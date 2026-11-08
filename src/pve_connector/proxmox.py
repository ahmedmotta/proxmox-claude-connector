"""Async client for the Proxmox VE REST API (API-token auth)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from .config import Settings


class ProxmoxError(Exception):
    """Raised when the Proxmox API returns an error. The message is safe to show to Claude."""

    def __init__(self, status: int, message: str, errors: Any = None):
        detail = f"Proxmox API error {status}: {message}"
        if errors:
            detail += f" | details: {errors}"
        super().__init__(detail)
        self.status = status
        self.errors = errors


@dataclass(frozen=True)
class Guest:
    vmid: int
    node: str
    type: str  # "qemu" or "lxc"
    name: str | None
    status: str | None

    @property
    def base(self) -> str:
        return f"/nodes/{self.node}/{self.type}/{self.vmid}"


def _encode(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Proxmox expects 1/0 for booleans and drops None values."""
    if not params:
        return None
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = 1 if value else 0
        elif isinstance(value, list | tuple):
            out[key] = [1 if v is True else 0 if v is False else v for v in value]
        else:
            out[key] = value
    return out


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_SNAPNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")
_UPID = re.compile(
    r"^UPID:[A-Za-z0-9._-]+:[0-9A-F]{8}:[0-9A-F]{8,}:[0-9A-F]{8}:[A-Za-z0-9_.-]*:[^:/?#]*:[^:/?#]+:$"
)
_SAFE_PATH = re.compile(r"^(/[A-Za-z0-9_.@!:=,+-]+)+/?$|^/$")


def check_name(value: str, what: str) -> str:
    """Validate a node, storage, bridge or similar name before it goes into a URL path."""
    if not isinstance(value, str) or not _NAME.match(value) or value in (".", ".."):
        raise ValueError(f"invalid {what} name: {value!r}")
    return value


def check_snapshot_name(value: str) -> str:
    if not isinstance(value, str) or not _SNAPNAME.match(value):
        raise ValueError(
            f"invalid snapshot name {value!r}: start with a letter, then letters, digits, _ or - (2-40 chars)"
        )
    return value


def check_upid(value: str) -> str:
    if not isinstance(value, str) or not _UPID.match(value):
        raise ValueError(f"invalid task id (UPID): {value!r}")
    return value


def normalize_path(path: str) -> str:
    """Canonical API path. Anything that could make the path mean something other than what it
    looks like (dot segments, encoding, queries, fragments, doubled slashes) is refused, so the
    permission policy always judges exactly the path that is sent."""
    path = (path or "").strip()
    for prefix in ("/api2/json", "api2/json"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
    if not path.startswith("/"):
        path = "/" + path
    bad = ("..", "%", "?", "#", "\\", "//")
    if any(token in path for token in bad) or any(ord(c) < 33 or ord(c) > 126 for c in path):
        raise ValueError(
            f"refused API path {path!r}: no '..', '%', '?', '#', '\\\\', '//', spaces or non-ASCII; "
            "put query values in params instead"
        )
    if not _SAFE_PATH.match(path):
        raise ValueError(f"refused API path {path!r}")
    return path.rstrip("/") or "/"


_normalize_path = normalize_path


class ProxmoxClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        verify: bool | str = settings.pve_ca_cert if settings.pve_ca_cert else settings.pve_verify_ssl
        self._client = httpx.AsyncClient(
            base_url=settings.pve_base_url,
            headers={"Authorization": f"PVEAPIToken={settings.pve_token_id}={settings.pve_token_secret}"},
            verify=verify,
            timeout=settings.pve_timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- raw ------------------------------------------------------------------------
    async def request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        path = _normalize_path(path)
        data = _encode(params)
        try:
            if method in ("GET", "DELETE"):
                response = await self._client.request(method, path, params=data)
            else:
                response = await self._client.request(method, path, data=data)
        except httpx.ConnectError as exc:
            raise ProxmoxError(0, f"cannot reach Proxmox at {self.settings.pve_base_url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise ProxmoxError(0, f"request to Proxmox timed out: {method} {path}") from exc

        body: Any
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_error:
            errors = body.get("errors") if isinstance(body, dict) else None
            message = response.reason_phrase or (body.get("message") if isinstance(body, dict) else "")
            raise ProxmoxError(response.status_code, message or response.text[:500], errors)
        return body.get("data") if isinstance(body, dict) else body

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params)

    async def post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, params)

    async def put(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("PUT", path, params)

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("DELETE", path, params)

    # ---- helpers --------------------------------------------------------------------
    async def guests(self) -> list[dict[str, Any]]:
        return await self.get("/cluster/resources", {"type": "vm"}) or []

    async def locate(self, vmid: int) -> Guest:
        for item in await self.guests():
            if int(item.get("vmid", -1)) == int(vmid):
                return Guest(
                    vmid=int(vmid),
                    node=item["node"],
                    type=item["type"],
                    name=item.get("name"),
                    status=item.get("status"),
                )
        raise ProxmoxError(404, f"no VM or container with VMID {vmid} exists in the cluster")

    async def next_vmid(self) -> int:
        return int(await self.get("/cluster/nextid"))

    async def nodes(self) -> list[str]:
        return [n["node"] for n in (await self.get("/nodes") or [])]

    async def default_node(self, node: str | None) -> str:
        if node:
            return check_name(node, "node")
        names = await self.nodes()
        if len(names) == 1:
            return names[0]
        raise ProxmoxError(400, f"several nodes exist ({', '.join(names)}); say which node to use")

    @staticmethod
    def is_upid(value: Any) -> bool:
        return isinstance(value, str) and value.startswith("UPID:")

    @staticmethod
    def node_of_upid(upid: str) -> str:
        # UPID:{node}:{pid}:{pstart}:{starttime}:{type}:{id}:{user}:
        return check_upid(upid).split(":")[1]

    async def task_status(self, upid: str) -> dict[str, Any]:
        node = self.node_of_upid(upid)
        return await self.get(f"/nodes/{node}/tasks/{upid}/status")

    async def task_log(self, upid: str, start: int = 0, limit: int = 500) -> list[str]:
        node = self.node_of_upid(upid)
        lines = await self.get(f"/nodes/{node}/tasks/{upid}/log", {"start": start, "limit": limit})
        return [line.get("t", "") for line in lines or []]

    async def wait_task(self, upid: str, timeout: float = 300, poll: float = 1.5) -> dict[str, Any]:
        """Wait until a task ends. Returns its status plus the tail of its log."""
        deadline = time.monotonic() + timeout
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = await self.task_status(upid)
            if status.get("status") == "stopped":
                break
            await anyio.sleep(poll)
        finished = status.get("status") == "stopped"
        log = await self.task_log(upid, limit=5000)
        return {
            "upid": upid,
            "finished": finished,
            "ok": finished and status.get("exitstatus") == "OK",
            "exitstatus": status.get("exitstatus"),
            "type": status.get("type"),
            "log_tail": log[-40:],
            "note": None
            if finished
            else f"still running after {int(timeout)}s; follow it with get_task (wait=true) using this upid",
        }

    async def maybe_wait(self, result: Any, wait: bool, timeout: float = 300) -> Any:
        """Wait for a task, but never longer than CONNECTOR_MAX_WAIT so the tool call returns before
        Claude's own time limit; a longer task is reported as still running with its UPID."""
        if wait and self.is_upid(result):
            return await self.wait_task(result, timeout=min(timeout, self.settings.connector_max_wait))
        if self.is_upid(result):
            return {"upid": result, "note": "task started in the background; follow it with get_task"}
        return result
