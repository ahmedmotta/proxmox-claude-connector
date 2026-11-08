"""The admin API used by the Proxmox GUI tab."""

import json

import pytest
import respx
from starlette.testclient import TestClient

from conftest import BASE, make_settings

PUBLIC = "https://pve-mcp.example.com"
ADMIN = "PVE:root@pam:6730ABCD::c2lnbmF0dXJl+/=="
AUDITOR = "PVE:viewer@pve:6730ABCD::c2lnbmF0dXJl"


@pytest.fixture
def pve():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:

        def perms(request):
            cookie = request.headers.get("cookie", "")
            if "root%40pam" in cookie:
                return respx.MockResponse(200, json={"data": {"/": {"Sys.Modify": 1, "Sys.Audit": 1}}})
            if "viewer%40pve" in cookie:
                return respx.MockResponse(200, json={"data": {"/": {"Sys.Audit": 1}}})
            return respx.MockResponse(401)

        mock.get("/access/permissions").mock(side_effect=perms)
        yield mock


@pytest.fixture
def app(env, pve):
    from pve_connector.server import build_server

    settings = make_settings()
    server = build_server(settings, remote=True)
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as client:
        client.settings = settings
        yield client


def call(app, method, action, ticket=ADMIN, **kw):
    headers = {"X-PVE-Ticket": ticket, "Origin": "https://192.168.1.10:8006"} if ticket else {}
    return app.request(method, f"/admin/api/{action}", headers=headers, **kw)


def test_requires_proxmox_login(app):
    assert call(app, "GET", "status", ticket=None).status_code == 401
    assert call(app, "GET", "status", ticket="PVE:bad@pam:1::x").status_code == 401


def test_requires_administrator(app):
    r = call(app, "GET", "status", ticket=AUDITOR)
    assert r.status_code == 403 and "not a Proxmox administrator" in r.json()["error"]


def test_status_and_cors(app, pve):
    r = call(app, "GET", "status")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["user"] == "root@pam" and data["settings"]["mode"] == "full"
    assert r.headers["access-control-allow-origin"] == "https://192.168.1.10:8006"
    # the ticket is sent URL-encoded as Proxmox expects
    assert "PVEAuthCookie=PVE%3Aroot%40pam" in pve.calls.last.request.headers["cookie"]
    pre = app.options(
        "/admin/api/settings",
        headers={"Origin": "https://pve:8006", "Access-Control-Request-Method": "PUT"},
    )
    assert pre.status_code == 204 and "X-PVE-Ticket" in pre.headers["access-control-allow-headers"]


def test_settings_apply_live_and_persist(app, tmp_path):
    r = call(app, "PUT", "settings", json={"mode": "safe", "protected_vmids": "100, 105", "paused": True})
    assert r.status_code == 200, r.text
    assert app.settings.connector_mode == "safe"
    assert app.settings.connector_protected_vmids == [100, 105]
    saved = json.loads((tmp_path / "runtime.json").read_text())
    assert saved["connector_mode"] == "safe" and saved["connector_paused"] is True
    # only what the GUI changed is saved; the password stays in the env file
    assert "connector_admin_password" not in saved and "connector_max_wait" not in saved
    assert (tmp_path / "runtime.json").stat().st_mode & 0o777 == 0o600
    assert "admin:settings" in (tmp_path / "audit.log").read_text()

    # a fresh server picks the saved values up over the env file
    from pve_connector.server import build_server

    fresh = make_settings()
    build_server(fresh, remote=True)
    assert fresh.connector_mode == "safe" and fresh.connector_paused is True


def test_bad_settings_rejected(app):
    assert call(app, "PUT", "settings", json={"mode": "god"}).status_code == 400
    assert call(app, "PUT", "settings", json={"protected_vmids": [5]}).status_code == 400
    assert call(app, "PUT", "settings", json={"max_wait": 5}).status_code == 400


def test_pause_blocks_tools(env, pve):
    from mcp.server.mcpserver.exceptions import ToolError

    from pve_connector.server import build_server

    server = build_server(make_settings(connector_paused=True), remote=False)

    async def run():
        with pytest.raises(ToolError, match="paused"):
            await server.call_tool("list_guests", {})
        info = await server.call_tool("connector_info", {})
        assert "paused" in "".join(getattr(c, "text", "") for c in info.content)

    import anyio

    anyio.run(run)


def test_password_change(app):
    from pve_connector.auth import check_password

    assert call(app, "POST", "password", json={"password": "short"}).status_code == 400
    assert call(app, "POST", "password", json={"password": "a much longer secret"}).status_code == 200
    assert check_password("a much longer secret", app.settings.connector_admin_password)


def test_revoke_and_audit(app):
    r = call(app, "POST", "revoke", json={})
    assert r.status_code == 200 and "revoked_tokens" in r.json()["data"]
    entries = call(app, "GET", "audit?limit=5").json()["data"]
    assert entries[0]["tool"] == "admin:revoke" and entries[0]["client"] == "pve:root@pam"


def test_health_is_public_for_the_gui(app):
    r = app.get("/health")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
