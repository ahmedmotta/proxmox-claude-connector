import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import UPID, make_settings, tool_payload


def server(**overrides):
    from pve_connector.server import build_server

    return build_server(make_settings(**overrides), remote=False)


async def test_all_tools_registered(env):
    names = {t.name for t in await server().list_tools()}
    assert {"get_overview", "list_guests", "power_guest", "host_shell", "proxmox_api", "guest_exec"} <= names
    assert len(names) >= 28


async def test_token_header_sent(env, pve_api):
    srv = server()
    await srv.call_tool("list_guests", {})
    request = pve_api.calls.last.request
    assert request.headers["Authorization"] == (
        "PVEAPIToken=root@pam!claude=00000000-0000-0000-0000-000000000000"
    )


async def test_list_guests_filters(env, pve_api):
    data = tool_payload(await server().call_tool("list_guests", {"type": "lxc"}))
    assert [g["vmid"] for g in data] == [101]
    data = tool_payload(await server().call_tool("list_guests", {"search": "pbx"}))
    assert data[0]["name"] == "pbx" and data[0]["memory_total_gib"] == 4.0


async def test_power_waits_for_task(env, pve_api):
    route = pve_api.post("/nodes/pve/qemu/100/status/shutdown").respond(json={"data": UPID})
    data = tool_payload(await server().call_tool("power_guest", {"vmid": 100, "action": "shutdown"}))
    assert route.called
    assert data["ok"] is True and data["exitstatus"] == "OK"
    assert b"timeout=" in route.calls.last.request.content


async def test_protected_vmid_blocks_stop_but_allows_start(env, pve_api):
    srv = server(connector_protected_vmids=[100])
    with pytest.raises(ToolError, match="protected"):
        await srv.call_tool("power_guest", {"vmid": 100, "action": "stop"})
    pve_api.post("/nodes/pve/qemu/100/status/start").respond(json={"data": UPID})
    data = tool_payload(await srv.call_tool("power_guest", {"vmid": 100, "action": "start"}))
    assert data["ok"] is True


async def test_readonly_mode_blocks_writes(env, pve_api):
    srv = server(connector_mode="readonly")
    with pytest.raises(ToolError, match="readonly"):
        await srv.call_tool("update_guest_config", {"vmid": 100, "settings": {"memory": 8192}})
    with pytest.raises(ToolError, match="readonly"):
        await srv.call_tool("proxmox_api", {"method": "POST", "path": "/nodes/pve/apt/update"})
    # reads still work
    assert tool_payload(await srv.call_tool("list_guests", {}))


async def test_safe_mode_blocks_delete_and_shell(env, pve_api):
    srv = server(connector_mode="safe")
    with pytest.raises(ToolError, match="destructive"):
        await srv.call_tool("delete_guest", {"vmid": 101})
    with pytest.raises(ToolError, match="shell"):
        await srv.call_tool("host_shell", {"command": "uptime"})
    with pytest.raises(ToolError, match="destructive"):
        await srv.call_tool("proxmox_api", {"method": "POST", "path": "/nodes/pve/qemu/100/status/stop"})


async def test_allowlist_hides_and_blocks(env, pve_api):
    srv = server(connector_allowed_vmids=[101])
    data = tool_payload(await srv.call_tool("list_guests", {}))
    assert [g["vmid"] for g in data] == [101]
    with pytest.raises(ToolError, match="allowlist"):
        await srv.call_tool("get_guest", {"vmid": 100})


async def test_generic_api_and_booleans(env, pve_api):
    pve_api.get("/nodes/pve/qemu/100/config").respond(json={"data": {"memory": 4096, "ide2": "none"}})
    route = pve_api.put("/nodes/pve/qemu/100/config").respond(json={"data": None})
    pve_api.get("/nodes/pve/qemu/100/pending").respond(json={"data": []})
    await server().call_tool(
        "update_guest_config", {"vmid": 100, "settings": {"onboot": True, "memory": 8192}, "delete": ["ide2"]}
    )
    body = route.calls.last.request.content.decode()
    assert "onboot=1" in body and "memory=8192" in body and "delete=ide2" in body


async def test_proxmox_error_surfaces_message(env, pve_api):
    pve_api.get("/nodes/pve/network").respond(
        status_code=500, json={"data": None}, extensions={"reason_phrase": b"no such node"}
    )
    with pytest.raises(ToolError, match="Proxmox API error 500"):
        await server().call_tool("proxmox_api", {"method": "GET", "path": "/nodes/pve/network"})


async def test_qemu_guest_exec(env, pve_api):
    pve_api.get("/nodes/pve/qemu/100/config").respond(json={"data": {"ostype": "l26"}})
    exec_route = pve_api.post("/nodes/pve/qemu/100/agent/exec").respond(json={"data": {"pid": 42}})
    pve_api.get("/nodes/pve/qemu/100/agent/exec-status", params={"pid": "42"}).respond(
        json={"data": {"exited": 1, "exitcode": 0, "out-data": "hello\n"}}
    )
    data = tool_payload(await server().call_tool("guest_exec", {"vmid": 100, "command": "echo hello"}))
    assert data["exit_code"] == 0 and data["stdout"] == "hello\n"
    body = exec_route.calls.last.request.content.decode()
    assert body.count("command=") == 3  # /bin/sh, -c, the command


async def test_container_exec_needs_ssh(env, pve_api):
    with pytest.raises(ToolError, match="(stopped|SSH)"):
        await server().call_tool("guest_exec", {"vmid": 101, "command": "uptime"})


async def test_create_vm_picks_next_id(env, pve_api):
    route = pve_api.post("/nodes/pve/qemu").respond(json={"data": UPID})
    data = tool_payload(await server().call_tool("create_vm", {"settings": {"name": "new", "memory": 2048}}))
    assert data["vmid"] == 102 and data["node"] == "pve"
    assert "vmid=102" in route.calls.last.request.content.decode()


async def test_audit_log_records_calls_and_redacts(env, pve_api, tmp_path):
    srv = server()
    pve_api.post("/access/users").respond(json={"data": None})
    await srv.call_tool(
        "proxmox_api",
        {"method": "POST", "path": "/access/users", "params": {"userid": "a@pve", "password": "x1"}},
    )
    log = (tmp_path / "audit.log").read_text()
    assert "proxmox_api" in log and '"password": "***"' in log and "x1" not in log


def test_classify_api_calls(env):
    from pve_connector.policy import Kind, Policy

    p = Policy(make_settings())
    assert p.classify_api_call("GET", "/nodes/pve/qemu/100/config") == (Kind.READ, {100}, True)
    assert p.classify_api_call("POST", "/nodes/pve/qemu/100/status/start") == (Kind.WRITE, {100}, True)
    assert p.classify_api_call("POST", "/nodes/pve/qemu/100/status/shutdown") == (Kind.WRITE, {100}, False)
    assert p.classify_api_call("POST", "/nodes/pve/lxc/105/status/stop")[0] == Kind.DESTRUCTIVE
    assert p.classify_api_call("POST", "/nodes/pve/status")[0] == Kind.DESTRUCTIVE
    assert p.classify_api_call("DELETE", "/pools/test")[0] == Kind.DESTRUCTIVE
    assert p.classify_api_call("POST", "/access/acl")[0] == Kind.DESTRUCTIVE  # default deny
    assert p.classify_api_call("PUT", "/nodes/pve/network")[0] == Kind.DESTRUCTIVE
    assert p.classify_api_call("POST", "/nodes/pve/qemu/100/agent/exec")[0] == Kind.SHELL
    assert p.classify_api_call("GET", "/nodes/pve/qemu/100/agent/file-read")[0] == Kind.SHELL
    assert p.classify_api_call("POST", "/nodes/pve/vncshell")[0] == Kind.SHELL
    assert p.classify_api_call("POST", "/nodes/pve/spiceshell")[0] == Kind.SHELL
    assert p.classify_api_call("GET", "/nodes/pve/storage/pbs/file-restore/list")[0] == Kind.SHELL
    assert p.classify_api_call("POST", "/nodes/pve/qemu/100/x/lxc/101/y")[1] == {100, 101}


@pytest.mark.parametrize(
    "path",
    [
        "/nodes/pve/qemu/101/../100/status/stop",
        "/nodes/pve/qemu/100/status/stop?x=1",
        "/nodes/pve/qemu/100/status/stop#",
        "/nodes/pve/qemu/100%2Fstatus%2Fstop",
        "/nodes/pve//qemu/100/status/stop",
        "/nodes/pve/qemu/100/status/stop\\",
        "/nodes/pve/qemu/100 /status",
    ],
)
async def test_path_tricks_refused(env, pve_api, path):
    with pytest.raises(ToolError, match="refused API path"):
        await server().call_tool("proxmox_api", {"method": "POST", "path": path})


async def test_safe_mode_api_is_default_deny(env, pve_api):
    srv = server(connector_mode="safe")
    for method, path in [
        ("POST", "/access/acl"),
        ("PUT", "/nodes/pve/network"),
        ("POST", "/nodes/pve/startall"),
        ("POST", "/nodes/pve/qemu/100/move_disk"),
    ]:
        with pytest.raises(ToolError, match="destructive"):
            await srv.call_tool("proxmox_api", {"method": method, "path": path})
    with pytest.raises(ToolError, match="shell"):
        await srv.call_tool("proxmox_api", {"method": "POST", "path": "/nodes/pve/vncshell"})
    pve_api.post("/nodes/pve/qemu/100/status/start").respond(json={"data": UPID})
    assert await srv.call_tool("proxmox_api", {"method": "POST", "path": "/nodes/pve/qemu/100/status/start"})


async def test_readonly_cannot_read_guest_files(env, pve_api):
    srv = server(connector_mode="readonly")
    with pytest.raises(ToolError, match="shell"):
        await srv.call_tool(
            "proxmox_api",
            {
                "method": "GET",
                "path": "/nodes/pve/qemu/100/agent/file-read",
                "params": {"file": "/etc/shadow"},
            },
        )


async def test_protected_vmid_in_body_is_checked(env, pve_api):
    srv = server(connector_protected_vmids=[100])
    with pytest.raises(ToolError, match="protected"):
        await srv.call_tool(
            "proxmox_api",
            {
                "method": "POST",
                "path": "/nodes/pve/qemu",
                "params": {"vmid": 100, "archive": "x", "force": 1},
            },
        )
    # backing up a protected guest is fine
    pve_api.post("/nodes/pve/vzdump").respond(json={"data": UPID})
    assert await srv.call_tool(
        "proxmox_api", {"method": "POST", "path": "/nodes/pve/vzdump", "params": {"vmid": "100"}}
    )


async def test_settings_cannot_override_vmid_or_force(env, pve_api):
    for bad in ({"vmid": 100}, {"force": 1, "archive": "local:backup/vzdump-qemu-100.vma.zst"}):
        with pytest.raises(ToolError, match="not allowed"):
            await server().call_tool("create_vm", {"settings": {"name": "x", **bad}})
    with pytest.raises(ToolError, match="not allowed"):
        await server().call_tool("create_container", {"settings": {"restore": 1, "ostemplate": "x"}})


async def test_deleting_disks_is_destructive(env, pve_api):
    pve_api.get("/nodes/pve/qemu/100/config").respond(json={"data": {"unused0": "local-lvm:vm-100-disk-1"}})
    srv = server(connector_mode="safe")
    with pytest.raises(ToolError, match="destructive"):
        await srv.call_tool("update_guest_config", {"vmid": 100, "delete": ["unused0"]})
    with pytest.raises(ToolError, match="shell"):
        await srv.call_tool(
            "update_guest_config", {"vmid": 100, "settings": {"hookscript": "local:snippets/x"}}
        )


async def test_bad_names_refused(env, pve_api):
    with pytest.raises(ToolError, match="snapshot name"):
        await server().call_tool("manage_snapshots", {"vmid": 100, "action": "delete", "name": "../../x"})
    with pytest.raises(ToolError, match="invalid storage"):
        await server().call_tool("storage_content", {"storage": "local/../x"})
    with pytest.raises(ToolError, match="UPID"):
        await server().call_tool("get_task", {"upid": "UPID:../x"})


async def test_allowlist_blocks_raw_access(env, pve_api):
    srv = server(connector_allowed_vmids=[101])
    with pytest.raises(ToolError, match="Raw API access is off"):
        await srv.call_tool("proxmox_api", {"method": "GET", "path": "/cluster/resources"})
    with pytest.raises(ToolError, match="host shell"):
        await srv.call_tool("host_shell", {"command": "qm list"})


async def test_long_tasks_return_early(env, pve_api):
    pve_api.routes.clear()
    pve_api.get("/cluster/resources", params={"type": "vm"}).respond(
        json={"data": [{"vmid": 100, "node": "pve", "type": "qemu", "status": "stopped"}]}
    )
    pve_api.get(url__regex=r".*/tasks/UPID.*/status").respond(json={"data": {"status": "running"}})
    pve_api.get(url__regex=r".*/tasks/UPID.*/log.*").respond(json={"data": []})
    pve_api.post("/nodes/pve/qemu/100/status/start").respond(json={"data": UPID})
    data = tool_payload(
        await server(connector_max_wait=1).call_tool("power_guest", {"vmid": 100, "action": "start"})
    )
    assert data["finished"] is False and "get_task" in data["note"]


async def test_audit_log_is_private(env, pve_api, tmp_path):
    await server().call_tool("list_guests", {})
    assert (tmp_path / "audit.log").stat().st_mode & 0o777 == 0o600


def test_ipv6_host(env):
    assert make_settings(pve_host="fd00::5").pve_base_url == "https://[fd00::5]:8006/api2/json"


def test_bad_token_id(env):
    with pytest.raises(ValueError, match="user@realm!tokenname"):
        make_settings(pve_token_id="root@pam")


async def test_connect_error_is_friendly(env):
    from pve_connector.proxmox import ProxmoxClient, ProxmoxError

    def fail(request):
        raise httpx.ConnectError("refused")

    client = ProxmoxClient(make_settings(), transport=httpx.MockTransport(fail))
    with pytest.raises(ProxmoxError, match="cannot reach Proxmox"):
        await client.get("/version")
