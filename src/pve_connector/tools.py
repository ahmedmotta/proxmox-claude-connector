"""The MCP tools Claude uses to operate Proxmox VE."""

from __future__ import annotations

import base64
import functools
import logging
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import quote

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .config import Settings
from .policy import AuditLog, Kind, Policy, PolicyError
from .proxmox import ProxmoxClient, ProxmoxError, check_name, check_snapshot_name, check_upid, normalize_path
from .ssh import SSHDisabled, SSHRunner

log = logging.getLogger("pve_connector.tools")

INSTRUCTIONS = """\
You are connected to a Proxmox VE environment with full administrative tools. Work like a careful,
experienced Proxmox administrator and carry out the user's requests directly.

- Start with get_overview to learn the nodes, guests, storage and the connector's permission mode.
- Find guests with list_guests; never guess a VMID. A VMID is unique across the cluster, and every
  guest tool works out the node and type (qemu VM or lxc container) by itself.
- Long operations (create, clone, backup, restore, migrate, download) run as Proxmox tasks. Most tools
  wait for them and report the log tail; if a task is still running, follow it with get_task.
- Prefer a clean shutdown over stop. Before risky changes to an important guest, take a snapshot
  (manage_snapshots) or a backup (backup_guests) and tell the user you did.
- Commands inside VMs need the QEMU guest agent installed and enabled in the VM (agent: enabled=1).
  Commands inside containers and on the host itself need SSH enabled on the connector.
- proxmox_api reaches every endpoint of the Proxmox REST API (/api2/json) for anything the dedicated
  tools do not cover: firewall, networking, users and permissions, HA, Ceph, replication, apt updates,
  certificates and more. api_help shows an endpoint's parameters when SSH is enabled.
- Sizes: memory is in MiB in VM/container settings; disk sizes look like 32G. Bridges are usually vmbr0.
- Report what you changed in plain words, including VMIDs, names and IP addresses where known.
"""


_DISK_KEY = re.compile(r"^(scsi|sata|ide|virtio|efidisk|tpmstate|unused|mp)\d+$|^rootfs$")


def _gib(value: Any) -> float | None:
    try:
        return round(float(value) / 1024**3, 2)
    except (TypeError, ValueError):
        return None


def _pct(used: Any, total: Any) -> float | None:
    try:
        return round(100 * float(used) / float(total), 1) if float(total) else None
    except (TypeError, ValueError):
        return None


def _client_id() -> str | None:
    try:
        token = get_access_token()
    except Exception:  # pragma: no cover - outside an HTTP request
        return None
    return token.client_id if token else None


def register_tools(
    server: MCPServer, settings: Settings, audit: AuditLog | None = None
) -> tuple[ProxmoxClient, SSHRunner]:
    pve = ProxmoxClient(settings)
    ssh = SSHRunner(settings)
    policy = Policy(settings)
    audit = audit or AuditLog(settings.data_path / "audit.log")
    limit = settings.connector_max_output_chars

    def check_settings(settings_: dict[str, Any], forbidden: set[str]) -> None:
        """Keys that would bypass the policy are refused; disks of other guests must be visible."""
        bad = forbidden & {str(k).lower() for k in settings_}
        if bad:
            raise ValueError(f"these keys are not allowed here: {', '.join(sorted(bad))}")
        if "hookscript" in {str(k).lower() for k in settings_}:
            policy.check(Kind.SHELL)  # a hookscript runs as root on the node
        for value in settings_.values():
            for other in re.findall(r"(?:vm|base|subvol)-(\d+)-disk", str(value)):
                policy.check(Kind.READ, int(other))

    def tool(
        kind: Kind, title: str, *, idempotent: bool = False
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        annotations = ToolAnnotations(
            title=title,
            read_only_hint=kind == Kind.READ,
            destructive_hint=kind in (Kind.DESTRUCTIVE, Kind.SHELL),
            idempotent_hint=idempotent or kind == Kind.READ,
            open_world_hint=False,
        )

        def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            @functools.wraps(fn)
            async def wrapper(**kwargs: Any) -> Any:
                started = time.monotonic()
                client = _client_id()
                try:
                    if fn.__name__ != "connector_info":
                        policy.check_not_paused()
                    result = await fn(**kwargs)
                except (PolicyError, ProxmoxError, SSHDisabled, ToolError, ValueError) as exc:
                    audit.write(tool=fn.__name__, client=client, args=kwargs, ok=False, error=str(exc))
                    raise ToolError(str(exc)) from exc
                except Exception as exc:
                    log.exception("tool %s failed", fn.__name__)
                    audit.write(tool=fn.__name__, client=client, args=kwargs, ok=False, error=repr(exc))
                    raise ToolError(f"unexpected error in {fn.__name__}: {exc}") from exc
                audit.write(
                    tool=fn.__name__,
                    client=client,
                    args=kwargs,
                    ok=True,
                    seconds=round(time.monotonic() - started, 2),
                )
                return result

            server.tool(name=fn.__name__, title=title, annotations=annotations)(wrapper)
            return fn

        return decorator

    # =================================================================================
    # Overview and discovery
    # =================================================================================
    @tool(Kind.READ, "Cluster overview")
    async def get_overview() -> dict[str, Any]:
        """Summary of the whole Proxmox environment: version, cluster and quorum, every node's CPU,
        memory and uptime, guest counts, storage usage, and the connector's own permission mode.
        Call this first in a new conversation."""
        version = await pve.get("/version")
        cluster = await pve.get("/cluster/status") or []
        resources = await pve.get("/cluster/resources") or []
        nodes, storage, guests = [], [], {"qemu": 0, "lxc": 0, "running": 0, "templates": 0}
        for r in resources:
            kind = r.get("type")
            if kind == "node":
                nodes.append(
                    {
                        "node": r.get("node"),
                        "status": r.get("status"),
                        "cpu_percent": round(100 * float(r.get("cpu") or 0), 1),
                        "cpus": r.get("maxcpu"),
                        "memory_used_gib": _gib(r.get("mem")),
                        "memory_total_gib": _gib(r.get("maxmem")),
                        "uptime_hours": round((r.get("uptime") or 0) / 3600, 1),
                    }
                )
            elif kind in ("qemu", "lxc") and policy.visible(r.get("vmid", 0)):
                guests[kind] += 1
                guests["running"] += r.get("status") == "running"
                guests["templates"] += bool(r.get("template"))
            elif kind == "storage":
                storage.append(
                    {
                        "storage": r.get("storage"),
                        "node": r.get("node"),
                        "type": r.get("plugintype"),
                        "content": r.get("content"),
                        "used_gib": _gib(r.get("disk")),
                        "total_gib": _gib(r.get("maxdisk")),
                        "used_percent": _pct(r.get("disk"), r.get("maxdisk")),
                        "status": r.get("status"),
                    }
                )
        cluster_info = next((c for c in cluster if c.get("type") == "cluster"), None)
        return {
            "proxmox_version": version.get("version") if isinstance(version, dict) else version,
            "cluster": {
                "name": cluster_info.get("name"),
                "quorate": bool(cluster_info.get("quorate")),
                "nodes": cluster_info.get("nodes"),
            }
            if cluster_info
            else "standalone node (no cluster)",
            "nodes": nodes,
            "guests": guests,
            "storage": storage,
            "connector": {"version": __version__, "ssh_enabled": ssh.enabled, **policy.describe()},
        }

    @tool(Kind.READ, "List VMs and containers")
    async def list_guests(
        node: str | None = None,
        type: Literal["all", "qemu", "lxc"] = "all",
        status: Literal["any", "running", "stopped", "paused"] = "any",
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """List virtual machines (qemu) and containers (lxc) across the cluster with their VMID,
        name, node, status, CPU, memory, disk, uptime, tags and whether they are templates.
        `search` matches part of the name, a tag or the VMID."""
        out = []
        for g in await pve.guests():
            if not policy.visible(g.get("vmid", 0)):
                continue
            if node and g.get("node") != node:
                continue
            if type != "all" and g.get("type") != type:
                continue
            if status != "any" and g.get("status") != status:
                continue
            if search:
                hay = f"{g.get('vmid')} {g.get('name', '')} {g.get('tags', '')}".lower()
                if search.lower() not in hay:
                    continue
            out.append(
                {
                    "vmid": g.get("vmid"),
                    "name": g.get("name"),
                    "type": g.get("type"),
                    "node": g.get("node"),
                    "status": g.get("status"),
                    "template": bool(g.get("template")),
                    "cpus": g.get("maxcpu"),
                    "cpu_percent": round(100 * float(g.get("cpu") or 0), 1),
                    "memory_used_gib": _gib(g.get("mem")),
                    "memory_total_gib": _gib(g.get("maxmem")),
                    "disk_total_gib": _gib(g.get("maxdisk")),
                    "uptime_hours": round((g.get("uptime") or 0) / 3600, 1),
                    "tags": g.get("tags"),
                    "lock": g.get("lock"),
                }
            )
        return sorted(out, key=lambda x: x["vmid"])

    @tool(Kind.READ, "Guest details")
    async def get_guest(vmid: int, include_config: bool = True) -> dict[str, Any]:
        """Full details of one VM or container: live status, configuration (disks, network, CPU,
        memory, boot order, cloud-init), snapshots count, and IP addresses reported by the guest
        agent (VMs) or the container interfaces (LXC)."""
        policy.check(Kind.READ, vmid)
        g = await pve.locate(vmid)
        result: dict[str, Any] = {"vmid": vmid, "node": g.node, "type": g.type}
        result["status"] = await pve.get(f"{g.base}/status/current")
        if include_config:
            result["config"] = await pve.get(f"{g.base}/config")
        try:
            snaps = await pve.get(f"{g.base}/snapshot") or []
            result["snapshots"] = [s.get("name") for s in snaps if s.get("name") != "current"]
        except ProxmoxError:
            pass
        if result["status"].get("status") == "running":
            try:
                if g.type == "qemu":
                    nics = await pve.get(f"{g.base}/agent/network-get-interfaces")
                    nics = nics.get("result", nics) if isinstance(nics, dict) else nics
                    result["ip_addresses"] = {
                        n.get("name"): [a.get("ip-address") for a in n.get("ip-addresses", [])]
                        for n in nics or []
                        if n.get("name") != "lo"
                    }
                else:
                    nics = await pve.get(f"{g.base}/interfaces")
                    result["ip_addresses"] = {
                        n.get("name"): [n.get("inet"), n.get("inet6")]
                        for n in nics or []
                        if n.get("name") != "lo"
                    }
            except ProxmoxError as exc:
                result["ip_addresses"] = f"not available ({exc})"
        return result

    @tool(Kind.READ, "Node status")
    async def node_status(node: str | None = None) -> dict[str, Any]:
        """Hardware and health of a node: CPU model and load, memory, swap, root disk, kernel,
        Proxmox version, boot mode, plus its network interfaces and physical disks."""
        node = await pve.default_node(node)
        result: dict[str, Any] = {"node": node, "status": await pve.get(f"/nodes/{node}/status")}
        for key, path in (
            ("network", f"/nodes/{node}/network"),
            ("disks", f"/nodes/{node}/disks/list"),
            ("services", f"/nodes/{node}/services"),
        ):
            try:
                result[key] = await pve.get(path)
            except ProxmoxError as exc:
                result[key] = f"not available ({exc})"
        return result

    # =================================================================================
    # Guest lifecycle
    # =================================================================================
    @tool(Kind.DESTRUCTIVE, "Power a VM or container")
    async def power_guest(
        vmid: int,
        action: Literal["start", "shutdown", "reboot", "stop", "reset", "suspend", "resume"],
        wait: bool = True,
        timeout_seconds: int = 180,
    ) -> Any:
        """Start, cleanly shut down, reboot, hard-stop, reset, suspend or resume a VM or container.
        `shutdown` asks the guest OS to power off; `stop` pulls the plug (use only if shutdown
        fails). `reset` is VM-only."""
        harmless = action in ("start", "resume")
        clean = action in ("shutdown", "reboot")
        policy.check(Kind.WRITE if harmless or clean else Kind.DESTRUCTIVE, vmid, protected_ok=harmless)
        g = await pve.locate(vmid)
        if action == "reset" and g.type != "qemu":
            raise ValueError("reset only exists for VMs; use reboot or stop for containers")
        params: dict[str, Any] = {}
        if action == "shutdown":
            params["timeout"] = max(10, timeout_seconds - 10)
        upid = await pve.post(f"{g.base}/status/{action}", params)
        return await pve.maybe_wait(upid, wait, timeout_seconds)

    @tool(Kind.WRITE, "Change VM or container settings")
    async def update_guest_config(
        vmid: int,
        settings: dict[str, Any] | None = None,
        delete: list[str] | None = None,
    ) -> dict[str, Any]:
        """Change configuration keys of a VM or container, exactly as in its config file. Examples:
        {"memory": 8192, "cores": 4}, {"net0": "virtio,bridge=vmbr0,tag=20"}, {"onboot": 1},
        {"name": "web01"}, {"tags": "prod;web"}, {"description": "notes"}, cloud-init
        {"ciuser": "admin", "ipconfig0": "ip=dhcp", "sshkeys": "ssh-ed25519 ..."}.
        `delete` removes keys, e.g. ["ide2"]. Some changes apply only after a reboot; the
        result lists pending changes."""
        policy.check(Kind.WRITE, vmid)
        if not settings and not delete:
            raise ValueError("give settings to change and/or keys to delete")
        check_settings(settings or {}, {"vmid", "digest"})
        g = await pve.locate(vmid)
        current = await pve.get(f"{g.base}/config") or {}
        touched = set(delete or []) | {k for k in (settings or {}) if k in current}
        if any(_DISK_KEY.match(k) for k in touched):
            # removing, detaching or redefining an existing disk can destroy data
            policy.check(Kind.DESTRUCTIVE, vmid)
        params = dict(settings or {})
        if delete:
            params["delete"] = ",".join(delete)
        if g.type == "qemu" and "sshkeys" in params:
            # Proxmox wants the keys URL-encoded inside the form value.
            params["sshkeys"] = quote(str(params["sshkeys"]), safe="")
        result = await pve.put(f"{g.base}/config", params)
        out: dict[str, Any] = {"updated": sorted(params), "result": result}
        try:
            pending = await pve.get(f"{g.base}/pending")
            out["pending_until_reboot"] = [p for p in pending or [] if "pending" in p or "delete" in p]
        except ProxmoxError:
            pass
        return out

    @tool(Kind.WRITE, "Grow a disk")
    async def resize_disk(vmid: int, disk: str, size: str) -> Any:
        """Grow a VM disk (e.g. disk="scsi0") or container volume (e.g. disk="rootfs" or "mp0").
        `size` is absolute ("64G") or relative ("+10G"). Disks can only grow. The filesystem
        inside the guest may still need extending afterwards."""
        policy.check(Kind.WRITE, vmid)
        if not _DISK_KEY.match(disk):
            raise ValueError(f"invalid disk name {disk!r}; examples: scsi0, virtio1, rootfs, mp0")
        g = await pve.locate(vmid)
        result = await pve.put(f"{g.base}/resize", {"disk": disk, "size": size})
        return await pve.maybe_wait(result, True, 300)

    @tool(Kind.WRITE, "Create a virtual machine")
    async def create_vm(
        settings: dict[str, Any],
        node: str | None = None,
        vmid: int | None = None,
        start: bool = False,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Create a QEMU virtual machine. `settings` are Proxmox VM options, for example:
        {"name": "web01", "memory": 4096, "cores": 2, "cpu": "host", "ostype": "l26",
         "scsihw": "virtio-scsi-single", "scsi0": "local-lvm:32,iothread=1,discard=on",
         "net0": "virtio,bridge=vmbr0", "ide2": "local:iso/debian-12.iso,media=cdrom",
         "boot": "order=scsi0;ide2", "agent": "enabled=1", "onboot": 1}
        For Windows use ostype "win11", add "efidisk0" and "tpmstate0", and a VirtIO drivers ISO.
        For a cloud image: "scsi0": "local-lvm:0,import-from=local:import/image.qcow2" plus
        "ide2": "local-lvm:cloudinit". The VMID is picked automatically when not given."""
        node = await pve.default_node(node)
        vmid = vmid or await pve.next_vmid()
        policy.check(Kind.WRITE, vmid)
        check_settings(settings, {"vmid", "force", "archive", "unique", "live-restore"})
        upid = await pve.post(f"/nodes/{node}/qemu", {**settings, "vmid": vmid})
        result = await pve.maybe_wait(upid, wait, 600)
        if start and wait and isinstance(result, dict) and result.get("ok"):
            await pve.post(f"/nodes/{node}/qemu/{vmid}/status/start")
        return {"vmid": vmid, "node": node, "task": result, "started": start}

    @tool(Kind.WRITE, "Create a container")
    async def create_container(
        settings: dict[str, Any],
        node: str | None = None,
        vmid: int | None = None,
        start: bool = False,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Create an LXC container. `settings` are Proxmox container options, for example:
        {"ostemplate": "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
         "hostname": "app01", "memory": 2048, "swap": 512, "cores": 2,
         "rootfs": "local-lvm:16", "net0": "name=eth0,bridge=vmbr0,ip=dhcp",
         "password": "...", "ssh-public-keys": "ssh-ed25519 ...", "unprivileged": 1,
         "features": "nesting=1", "onboot": 1}
        Find templates with storage_content(content="vztmpl") or list_lxc_templates and get one
        with download_to_storage. The VMID is picked automatically when not given."""
        node = await pve.default_node(node)
        vmid = vmid or await pve.next_vmid()
        policy.check(Kind.WRITE, vmid)
        check_settings(settings, {"vmid", "force", "restore", "unique"})
        if "vzdump-" in str(settings.get("ostemplate", "")):
            raise ValueError("restoring a backup goes through restore_backup")
        upid = await pve.post(f"/nodes/{node}/lxc", {**settings, "vmid": vmid})
        result = await pve.maybe_wait(upid, wait, 600)
        if start and wait and isinstance(result, dict) and result.get("ok"):
            await pve.post(f"/nodes/{node}/lxc/{vmid}/status/start")
        return {"vmid": vmid, "node": node, "task": result, "started": start}

    @tool(Kind.WRITE, "Clone a VM or container")
    async def clone_guest(
        vmid: int,
        name: str | None = None,
        newid: int | None = None,
        full: bool = True,
        target_node: str | None = None,
        storage: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Clone a VM, container or template into a new guest. `full=False` makes a linked clone
        (templates only, faster, shares the base disk)."""
        policy.check(Kind.READ, vmid)
        g = await pve.locate(vmid)
        newid = newid or await pve.next_vmid()
        policy.check(Kind.WRITE, newid)
        target_node = check_name(target_node, "node") if target_node else None
        storage = check_name(storage, "storage") if storage else None
        params: dict[str, Any] = {"newid": newid, "full": full, "target": target_node, "storage": storage}
        params["name" if g.type == "qemu" else "hostname"] = name
        upid = await pve.post(f"{g.base}/clone", params)
        return {"newid": newid, "task": await pve.maybe_wait(upid, wait, 1800)}

    @tool(Kind.WRITE, "Migrate to another node")
    async def migrate_guest(vmid: int, target_node: str, online: bool = True, wait: bool = True) -> Any:
        """Move a VM or container to another cluster node. Running VMs migrate live when
        `online` is true; running containers are restarted on the target."""
        policy.check(Kind.WRITE, vmid)
        g = await pve.locate(vmid)
        params: dict[str, Any] = {"target": check_name(target_node, "node")}
        if g.type == "qemu":
            params["online"] = online and g.status == "running"
            params["with-local-disks"] = True  # needed for local-lvm / ZFS; harmless on shared storage
        else:
            params["restart"] = g.status == "running"
        upid = await pve.post(f"{g.base}/migrate", params)
        return await pve.maybe_wait(upid, wait, 3600)

    @tool(Kind.DESTRUCTIVE, "Convert to template")
    async def convert_to_template(vmid: int) -> Any:
        """Turn a stopped VM or container into a template for fast cloning. This cannot be undone."""
        policy.check(Kind.DESTRUCTIVE, vmid)
        g = await pve.locate(vmid)
        return await pve.maybe_wait(await pve.post(f"{g.base}/template"), True, 600)

    @tool(Kind.DESTRUCTIVE, "Delete a VM or container")
    async def delete_guest(vmid: int, purge: bool = True, wait: bool = True) -> Any:
        """Permanently delete a VM or container and its disks. It must be stopped first.
        `purge` also removes it from backup jobs, replication and HA. Only call this after the
        user has clearly asked to delete this specific guest."""
        policy.check(Kind.DESTRUCTIVE, vmid)
        g = await pve.locate(vmid)
        if g.status == "running":
            raise ValueError(f"VMID {vmid} is running; shut it down first")
        params = {"purge": purge, "destroy-unreferenced-disks": True}
        return await pve.maybe_wait(await pve.delete(g.base, params), wait, 600)

    # =================================================================================
    # Snapshots and backups
    # =================================================================================
    @tool(Kind.DESTRUCTIVE, "Manage snapshots")
    async def manage_snapshots(
        vmid: int,
        action: Literal["list", "create", "rollback", "delete"],
        name: str | None = None,
        description: str | None = None,
        include_ram: bool = False,
        wait: bool = True,
    ) -> Any:
        """List, create, roll back to, or delete snapshots of a VM or container. Snapshot names
        must start with a letter (letters, digits, _ and -). `include_ram` saves VM memory too.
        Rollback throws away everything since the snapshot."""
        if action == "list":
            policy.check(Kind.READ, vmid)
        elif action == "create":
            policy.check(Kind.WRITE, vmid, protected_ok=True)
        else:
            policy.check(Kind.DESTRUCTIVE, vmid)
        g = await pve.locate(vmid)
        if action == "list":
            return await pve.get(f"{g.base}/snapshot")
        if not name:
            raise ValueError("a snapshot name is required")
        check_snapshot_name(name)
        if action == "create":
            params: dict[str, Any] = {"snapname": name, "description": description}
            if g.type == "qemu":
                params["vmstate"] = include_ram
            return await pve.maybe_wait(await pve.post(f"{g.base}/snapshot", params), wait, 900)
        if action == "rollback":
            return await pve.maybe_wait(await pve.post(f"{g.base}/snapshot/{name}/rollback"), wait, 900)
        return await pve.maybe_wait(await pve.delete(f"{g.base}/snapshot/{name}"), wait, 900)

    @tool(Kind.WRITE, "Back up now")
    async def backup_guests(
        vmids: list[int],
        storage: str | None = None,
        mode: Literal["snapshot", "suspend", "stop"] = "snapshot",
        compress: Literal["zstd", "gzip", "lzo", "0"] = "zstd",
        notes: str | None = None,
        protected: bool = False,
        wait: bool = True,
        timeout_seconds: int = 3600,
    ) -> list[Any]:
        """Run an immediate backup (vzdump) of one or more guests to a backup storage (local,
        NFS, Proxmox Backup Server...). `snapshot` mode keeps the guest running. Without a
        storage, the node's default backup storage is used. `protected` keeps the backup from
        automatic pruning."""
        results = []
        by_node: dict[str, list[int]] = {}
        for vmid in vmids:
            policy.check(Kind.WRITE, vmid, protected_ok=True)
            g = await pve.locate(vmid)
            by_node.setdefault(g.node, []).append(vmid)
        if storage:
            check_name(storage, "storage")
        for node, ids in by_node.items():
            params = {
                "vmid": ",".join(str(i) for i in ids),
                "storage": storage,
                "mode": mode,
                "compress": compress,
                "notes-template": notes,
                "protected": protected or None,
            }
            upid = await pve.post(f"/nodes/{node}/vzdump", params)
            results.append(
                {"node": node, "vmids": ids, "task": await pve.maybe_wait(upid, wait, timeout_seconds)}
            )
        return results

    @tool(Kind.READ, "List backups")
    async def list_backups(
        storage: str | None = None, vmid: int | None = None, node: str | None = None
    ) -> list[dict[str, Any]]:
        """List backup files (volid, guest, date, size, notes, protected). Without a storage, every
        storage that holds backups on the node is searched."""
        node = await pve.default_node(node)
        stores = (
            [check_name(storage, "storage")]
            if storage
            else [s["storage"] for s in await pve.get(f"/nodes/{node}/storage", {"content": "backup"}) or []]
        )
        out = []
        for store in stores:
            items = await pve.get(
                f"/nodes/{node}/storage/{store}/content", {"content": "backup", "vmid": vmid}
            )
            for item in items or []:
                if not policy.visible(item.get("vmid", 0) or 0):
                    continue
                out.append(
                    {
                        "volid": item.get("volid"),
                        "vmid": item.get("vmid"),
                        "storage": store,
                        "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("ctime", 0))),
                        "size_gib": _gib(item.get("size")),
                        "notes": item.get("notes"),
                        "protected": bool(item.get("protected")),
                        "format": item.get("format"),
                    }
                )
        return sorted(out, key=lambda x: x["created"], reverse=True)

    @tool(Kind.DESTRUCTIVE, "Restore a backup")
    async def restore_backup(
        archive: str,
        vmid: int | None = None,
        node: str | None = None,
        storage: str | None = None,
        overwrite: bool = False,
        start: bool = False,
        unique: bool = True,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Restore a backup (`archive` is the volid from list_backups) as a new guest, or over an
        existing one with `overwrite=true` (destroys the current guest's data). The archive name
        decides VM (vzdump-qemu-...) or container (vzdump-lxc-...). `unique` gives a restored VM
        new MAC addresses."""
        node = await pve.default_node(node)
        vmid = vmid or await pve.next_vmid()
        policy.check(Kind.DESTRUCTIVE if overwrite else Kind.WRITE, vmid)
        source = re.search(r"vzdump-(?:qemu|lxc)-(\d+)-", archive) or re.search(r"/(?:vm|ct)/(\d+)/", archive)
        if source:
            policy.check(Kind.READ, int(source.group(1)))
        if storage:
            check_name(storage, "storage")
        is_lxc = "vzdump-lxc-" in archive or "/ct/" in archive
        params: dict[str, Any] = {
            "vmid": vmid,
            "storage": storage,
            "force": overwrite or None,
            "unique": unique,
        }
        if is_lxc:
            params.update({"ostemplate": archive, "restore": 1})
            upid = await pve.post(f"/nodes/{node}/lxc", params)
        else:
            params["archive"] = archive
            upid = await pve.post(f"/nodes/{node}/qemu", params)
        result = await pve.maybe_wait(upid, wait, 7200)
        if start and wait and isinstance(result, dict) and result.get("ok"):
            await pve.post(f"/nodes/{node}/{'lxc' if is_lxc else 'qemu'}/{vmid}/status/start")
        return {"vmid": vmid, "node": node, "type": "lxc" if is_lxc else "qemu", "task": result}

    # =================================================================================
    # Storage, ISOs and templates
    # =================================================================================
    @tool(Kind.READ, "List storage")
    async def list_storage(node: str | None = None) -> list[dict[str, Any]]:
        """Storage on a node with type, allowed content (images, iso, vztmpl, backup...), usage
        and whether it is shared."""
        node = await pve.default_node(node)
        out = []
        for s in await pve.get(f"/nodes/{node}/storage") or []:
            out.append(
                {
                    "storage": s.get("storage"),
                    "type": s.get("type"),
                    "content": s.get("content"),
                    "shared": bool(s.get("shared")),
                    "active": bool(s.get("active")),
                    "used_gib": _gib(s.get("used")),
                    "total_gib": _gib(s.get("total")),
                    "used_percent": _pct(s.get("used"), s.get("total")),
                }
            )
        return out

    @tool(Kind.READ, "Storage contents")
    async def storage_content(
        storage: str,
        node: str | None = None,
        content: Literal["any", "images", "rootdir", "iso", "vztmpl", "backup", "snippets", "import"] = "any",
    ) -> list[dict[str, Any]]:
        """Files and volumes on one storage: disk images, ISO files, container templates, backups,
        snippets or import images. Use the `volid` values in other tools."""
        node = await pve.default_node(node)
        params = {"content": None if content == "any" else content}
        items = await pve.get(f"/nodes/{node}/storage/{check_name(storage, 'storage')}/content", params) or []
        items = [i for i in items if not i.get("vmid") or policy.visible(i["vmid"])]
        return [
            {
                "volid": i.get("volid"),
                "content": i.get("content"),
                "format": i.get("format"),
                "size_gib": _gib(i.get("size")),
                "vmid": i.get("vmid"),
                "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(i["ctime"]))
                if i.get("ctime")
                else None,
            }
            for i in items
        ]

    @tool(Kind.WRITE, "Download an ISO or template")
    async def download_to_storage(
        url: str,
        storage: str,
        filename: str,
        content: Literal["iso", "vztmpl", "import"] = "iso",
        node: str | None = None,
        checksum: str | None = None,
        checksum_algorithm: Literal["md5", "sha1", "sha224", "sha256", "sha384", "sha512"] | None = None,
        wait: bool = True,
    ) -> Any:
        """Have Proxmox download a file straight from the internet into a storage: an installer ISO
        (content="iso"), a container template (content="vztmpl") or a cloud/disk image for
        importing (content="import", e.g. .qcow2). Give the checksum when the vendor publishes one."""
        policy.check(Kind.WRITE)
        node = await pve.default_node(node)
        params = {
            "url": url,
            "content": content,
            "filename": filename,
            "checksum": checksum,
            "checksum-algorithm": checksum_algorithm,
        }
        upid = await pve.post(f"/nodes/{node}/storage/{check_name(storage, 'storage')}/download-url", params)
        return await pve.maybe_wait(upid, wait, 3600)

    @tool(Kind.READ, "Container template catalog")
    async def list_lxc_templates(node: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
        """The official container template catalog (Debian, Ubuntu, Alpine, Rocky, TurnKey apps...)
        that can be downloaded onto a node with download_lxc_template."""
        node = await pve.default_node(node)
        items = await pve.get(f"/nodes/{node}/aplinfo") or []
        out = [
            {
                "template": i.get("template"),
                "section": i.get("section"),
                "os": i.get("os"),
                "version": i.get("version"),
                "description": i.get("headline"),
            }
            for i in items
        ]
        if search:
            needle = search.lower()
            out = [i for i in out if needle in f"{i['template']} {i['description']} {i['os']}".lower()]
        return out

    @tool(Kind.WRITE, "Download a container template")
    async def download_lxc_template(template: str, storage: str = "local", node: str | None = None) -> Any:
        """Download a template from the official catalog (name from list_lxc_templates) into a
        storage that holds container templates."""
        policy.check(Kind.WRITE)
        node = await pve.default_node(node)
        upid = await pve.post(
            f"/nodes/{node}/aplinfo", {"storage": check_name(storage, "storage"), "template": template}
        )
        return await pve.maybe_wait(upid, True, 1800)

    # =================================================================================
    # Tasks
    # =================================================================================
    @tool(Kind.READ, "Recent tasks")
    async def list_tasks(
        node: str | None = None,
        vmid: int | None = None,
        running_only: bool = False,
        errors_only: bool = False,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Recent Proxmox tasks (backups, starts, migrations, clones...) with result, user and
        duration, newest first. Use get_task with a UPID for the full log."""
        nodes = [check_name(node, "node")] if node else await pve.nodes()
        out = []
        for n in nodes:
            params = {
                "vmid": vmid,
                "source": "active" if running_only else "all",
                "errors": errors_only,
                "limit": limit,
            }
            for t in await pve.get(f"/nodes/{n}/tasks", params) or []:
                if policy.allowed and not (str(t.get("id", "")).isdigit() and policy.visible(int(t["id"]))):
                    continue
                start, end = t.get("starttime"), t.get("endtime")
                out.append(
                    {
                        "upid": t.get("upid"),
                        "node": n,
                        "type": t.get("type"),
                        "id": t.get("id"),
                        "user": t.get("user"),
                        "status": t.get("status") or "running",
                        "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start))
                        if start
                        else None,
                        "seconds": (end - start) if start and end else None,
                    }
                )
        out.sort(key=lambda x: x["started"] or "", reverse=True)
        return out[:limit]

    @tool(Kind.READ, "Task status and log")
    async def get_task(upid: str, wait: bool = False, timeout_seconds: int = 300) -> dict[str, Any]:
        """Status and log of a Proxmox task by its UPID. With `wait`, block until it ends."""
        check_upid(upid)
        if wait:
            return await pve.wait_task(upid, timeout=min(timeout_seconds, settings.connector_max_wait))
        status = await pve.task_status(upid)
        log_lines = await pve.task_log(upid, limit=5000)
        return {"status": status, "log_tail": log_lines[-80:]}

    # =================================================================================
    # Running commands
    # =================================================================================
    async def _qemu_exec(base: str, argv: list[str], stdin: str | None, timeout: float) -> dict[str, Any]:
        params: dict[str, Any] = {"command": argv}
        if stdin:
            params["input-data"] = stdin
        started = await pve.post(f"{base}/agent/exec", params)
        pid = started["pid"] if isinstance(started, dict) else started
        deadline = time.monotonic() + timeout
        while True:
            status = await pve.get(f"{base}/agent/exec-status", {"pid": pid})
            if status.get("exited"):
                break
            if time.monotonic() > deadline:
                return {
                    "exit_code": None,
                    "timed_out": True,
                    "pid": pid,
                    "note": "command still running inside the VM",
                }
            await anyio.sleep(0.7)

        def clip(text: str | None) -> str:
            text = text or ""
            return text if len(text) <= limit else f"...[{len(text) - limit} chars cut]...\n" + text[-limit:]

        return {
            "exit_code": status.get("exitcode"),
            "stdout": clip(status.get("out-data")),
            "stderr": clip(status.get("err-data")),
            "truncated": bool(status.get("out-truncated") or status.get("err-truncated")),
            "timed_out": False,
        }

    @tool(Kind.SHELL, "Run a command inside a VM or container")
    async def guest_exec(
        vmid: int, command: str, timeout_seconds: int = 120, stdin: str | None = None
    ) -> Any:
        """Run a shell command inside a guest and return its exit code and output.
        VMs: goes through the QEMU guest agent (Linux runs it with /bin/sh -c, Windows with
        PowerShell). Containers: runs `pct exec` on the node over SSH."""
        policy.check(Kind.SHELL, vmid, protected_ok=True)
        g = await pve.locate(vmid)
        if g.status != "running":
            raise ValueError(f"VMID {vmid} is {g.status}; start it first")
        if g.type == "qemu":
            config = await pve.get(f"{g.base}/config")
            if str(config.get("ostype", "")).startswith("win"):
                argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            else:
                argv = ["/bin/sh", "-c", command]
            return await _qemu_exec(g.base, argv, stdin, timeout_seconds)
        cmd = f"pct exec {vmid} -- /bin/sh -c {shlex.quote(command)}"
        result = await ssh.run_on_node(g.node, cmd, timeout=timeout_seconds, stdin_data=stdin)
        return result.as_dict(limit)

    @tool(Kind.SHELL, "Read or write a file inside a guest")
    async def guest_file(
        vmid: int, action: Literal["read", "write"], path: str, content: str | None = None
    ) -> Any:
        """Read a text file from, or write a text file into, a running VM (guest agent) or
        container (SSH). Writing replaces the whole file. Best for config files and scripts;
        VM file transfer is limited to about 60 KB per call."""
        policy.check(Kind.SHELL, vmid, protected_ok=True)
        g = await pve.locate(vmid)
        if g.status != "running":
            raise ValueError(f"VMID {vmid} is {g.status}; start it first")
        if action == "write" and content is None:
            raise ValueError("content is required for write")
        if g.type == "qemu":
            if action == "read":
                data = await pve.get(f"{g.base}/agent/file-read", {"file": path})
                return {
                    "path": path,
                    "content": data.get("content"),
                    "truncated": bool(data.get("truncated")),
                }
            await pve.post(f"{g.base}/agent/file-write", {"file": path, "content": content})
            return {"path": path, "written_bytes": len((content or "").encode())}
        if action == "read":
            result = await ssh.run_on_node(
                g.node, f"pct exec {vmid} -- cat -- {shlex.quote(path)}", timeout=60
            )
            if result.exit_code != 0:
                raise ValueError(result.stderr.strip() or f"cannot read {path}")
            return {"path": path, "content": result.stdout[:limit], "truncated": len(result.stdout) > limit}
        encoded = base64.b64encode((content or "").encode()).decode()
        inner = f"base64 -d > {shlex.quote(path)}"
        result = await ssh.run_on_node(
            g.node, f"pct exec {vmid} -- /bin/sh -c {shlex.quote(inner)}", timeout=60, stdin_data=encoded
        )
        if result.exit_code != 0:
            raise ValueError(result.stderr.strip() or f"cannot write {path}")
        return {"path": path, "written_bytes": len((content or "").encode())}

    @tool(Kind.SHELL, "Run a command on the Proxmox host")
    async def host_shell(command: str, node: str | None = None, timeout_seconds: int = 120) -> Any:
        """Run a shell command as root on a Proxmox node over SSH: pvesh, qm, pct, pvesm, zfs,
        apt, journalctl, systemctl, ip, smartctl and so on. This is the most powerful tool;
        explain risky commands to the user before running them."""
        policy.check(Kind.SHELL)
        policy.check_unscoped("The host shell")
        if node:
            result = await ssh.run_on_node(check_name(node, "node"), command, timeout=timeout_seconds)
        else:
            result = await ssh.run(command, timeout=timeout_seconds)
        return result.as_dict(limit)

    # =================================================================================
    # Everything else
    # =================================================================================
    @tool(Kind.DESTRUCTIVE, "Call any Proxmox API endpoint")
    async def proxmox_api(
        method: Literal["GET", "POST", "PUT", "DELETE"],
        path: str,
        params: dict[str, Any] | None = None,
        wait_for_task: bool = True,
    ) -> Any:
        """Call any Proxmox VE REST API endpoint, e.g. GET /nodes/pve/network,
        POST /nodes/pve/network (then PUT /nodes/pve/network to apply), GET /cluster/firewall/rules,
        POST /access/users, GET /cluster/ha/resources, POST /nodes/pve/apt/update,
        GET /nodes/pve/apt/update, GET /cluster/backup (scheduled backup jobs), GET /pools.
        `path` is relative to /api2/json; put query values in `params`, never in the path.
        When the call starts a task, the result is waited for. In `safe` mode only a short list of
        harmless changes is allowed here (start, snapshot, backup, downloads, apt update)."""
        path = normalize_path(path)
        policy.check_api_call(method, path, params)
        result = await pve.request(method, path, params)
        return await pve.maybe_wait(result, wait_for_task, 1800)

    @tool(Kind.READ, "Look up API parameters")
    async def api_help(path: str) -> Any:
        """Show the methods and parameters of a Proxmox API path (from `pvesh usage`), e.g.
        "/nodes/{node}/qemu" or "/cluster/firewall/rules". Needs SSH enabled."""
        policy.check(Kind.READ)
        result = await ssh.run(f"pvesh usage {shlex.quote(path)} --verbose", timeout=30)
        return result.as_dict(limit)

    @tool(Kind.READ, "Connector status and audit log")
    async def connector_info(audit_entries: int = 20) -> dict[str, Any]:
        """The connector's own version, permission mode, protected VMIDs, whether SSH is enabled,
        and the latest entries of its audit log (every action taken through it)."""
        return {
            "version": __version__,
            "proxmox_api": settings.pve_base_url,
            "ssh_enabled": ssh.enabled,
            "paused": settings.connector_paused,
            **policy.describe(),
            "recent_actions": audit.tail(max(1, min(audit_entries, 200))),
        }

    return pve, ssh
