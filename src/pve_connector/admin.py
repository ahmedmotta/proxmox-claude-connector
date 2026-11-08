"""Admin API behind the "Claude Connector" tab in the Proxmox web interface.

There are no separate admin accounts. The Proxmox GUI tab sends the logged-in user's Proxmox
ticket; the connector asks Proxmox whether that ticket is valid and whether its user holds
`Sys.Modify` on `/` (an administrator). Only then may settings be read or changed.

Changes are applied immediately and saved to `<data dir>/runtime.json`, which takes precedence
over the env file on the next start.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .auth import ConnectorOAuthProvider, hash_password
from .config import Mode, Settings
from .policy import AuditLog

TICKET_HEADER = "x-pve-ticket"
CACHE_SECONDS = 60
RUNTIME_KEYS = (
    "connector_mode",
    "connector_protected_vmids",
    "connector_allowed_vmids",
    "connector_max_wait",
    "connector_paused",
    "connector_admin_password",
)


class SettingsUpdate(BaseModel):
    mode: Mode | None = None
    protected_vmids: list[int] | None = None
    allowed_vmids: list[int] | None = None
    max_wait: int | None = Field(default=None, ge=10, le=600)
    paused: bool | None = None

    @field_validator("protected_vmids", "allowed_vmids", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [int(v) for v in value.replace(" ", "").split(",") if v]
        return value

    @field_validator("protected_vmids", "allowed_vmids")
    @classmethod
    def _range(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(not 100 <= v <= 999_999_999 for v in value):
            raise ValueError("VMIDs are numbers from 100 upwards")
        return sorted(set(value)) if value is not None else None


class AdminError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---- runtime overrides ----------------------------------------------------------------
def runtime_path(settings: Settings) -> Path:
    return settings.data_path / "runtime.json"


def load_runtime(settings: Settings) -> None:
    path = runtime_path(settings)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    for key in RUNTIME_KEYS:
        if key in data:
            setattr(settings, key, Mode(data[key]) if key == "connector_mode" else data[key])


def save_runtime(settings: Settings, keys: list[str]) -> None:
    """Persist only the settings changed from the GUI, merged into what was saved before."""
    path = runtime_path(settings)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    for key in keys:
        value = getattr(settings, key)
        data[key] = Mode(value).value if key == "connector_mode" else value
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# ---- the API --------------------------------------------------------------------------
class AdminAPI:
    def __init__(
        self,
        settings: Settings,
        provider: ConnectorOAuthProvider,
        audit: AuditLog,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.provider = provider
        self.audit = audit
        self._transport = transport
        self._cache: dict[str, tuple[float, str]] = {}

    # ---- authentication ---------------------------------------------------------------
    async def _verify(self, request: Request) -> str:
        ticket = request.headers.get(TICKET_HEADER, "")
        if not ticket.startswith("PVE:") or len(ticket) > 4096:
            raise AdminError(401, "Missing Proxmox login. Open this tab from the Proxmox web interface.")
        key = hashlib.sha256(ticket.encode()).hexdigest()
        cached = self._cache.get(key)
        if cached and cached[0] > time.time():
            return cached[1]

        s = self.settings
        verify: bool | str = s.pve_ca_cert if s.pve_ca_cert else s.pve_verify_ssl
        async with httpx.AsyncClient(
            base_url=s.pve_base_url, verify=verify, timeout=15, transport=self._transport
        ) as client:
            try:
                response = await client.get(
                    "/access/permissions",
                    params={"path": "/"},
                    headers={"Cookie": f"PVEAuthCookie={quote(ticket, safe='')}"},
                )
            except httpx.HTTPError as exc:
                raise AdminError(502, f"Cannot reach Proxmox to check the login: {exc}") from exc
        if response.status_code == 401:
            raise AdminError(401, "Your Proxmox session has expired. Log in again.")
        if response.is_error:
            raise AdminError(502, f"Proxmox refused the login check ({response.status_code}).")
        perms = (response.json().get("data") or {}).get("/", {})
        user = ticket.split(":")[1] if ticket.count(":") >= 2 else "unknown"
        if not perms.get("Sys.Modify"):
            raise AdminError(403, f"{user} is not a Proxmox administrator (Sys.Modify on / is required).")
        if len(self._cache) > 256:
            self._cache.clear()
        self._cache[key] = (time.time() + CACHE_SECONDS, user)
        return user

    # ---- views ------------------------------------------------------------------------
    def _settings_view(self) -> dict[str, Any]:
        s = self.settings
        return {
            "mode": Mode(s.connector_mode).value,
            "protected_vmids": list(s.connector_protected_vmids),
            "allowed_vmids": list(s.connector_allowed_vmids),
            "max_wait": s.connector_max_wait,
            "paused": s.connector_paused,
        }

    async def status(self, user: str) -> dict[str, Any]:
        s = self.settings
        return {
            "version": __version__,
            "user": user,
            "public_url": s.connector_public_url,
            "mcp_url": f"{s.connector_public_url}/mcp",
            "ssh_enabled": s.pve_ssh_enabled,
            "admin_password_set": bool(s.connector_admin_password),
            "settings": self._settings_view(),
            "sessions": self.provider.sessions(),
        }

    async def update_settings(self, user: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            update = SettingsUpdate.model_validate(body)
        except ValidationError as exc:
            raise AdminError(400, "; ".join(e["msg"] for e in exc.errors())) from exc
        s = self.settings
        changes = update.model_dump(exclude_none=True)
        mapping = {
            "mode": "connector_mode",
            "protected_vmids": "connector_protected_vmids",
            "allowed_vmids": "connector_allowed_vmids",
            "max_wait": "connector_max_wait",
            "paused": "connector_paused",
        }
        changed = [attr for field, attr in mapping.items() if field in changes]
        for field, attr in mapping.items():
            if field in changes:
                setattr(s, attr, changes[field])
        save_runtime(s, changed)
        self.audit.write(tool="admin:settings", client=f"pve:{user}", args=changes, ok=True)
        return self._settings_view()

    async def audit_tail(self, limit: int) -> list[dict[str, Any]]:
        return list(reversed(self.audit.tail(max(1, min(limit, 1000)))))

    async def revoke(self, user: str, body: dict[str, Any]) -> dict[str, Any]:
        client_id = body.get("client_id")
        if client_id:
            count = self.provider.revoke_client(str(client_id))
        else:
            count = self.provider.revoke_all()
        self.audit.write(tool="admin:revoke", client=f"pve:{user}", args={"client_id": client_id}, ok=True)
        return {"revoked_tokens": count}

    async def set_password(self, user: str, body: dict[str, Any]) -> dict[str, Any]:
        password = str(body.get("password", ""))
        if len(password) < 12:
            raise AdminError(400, "Use at least 12 characters.")
        self.settings.connector_admin_password = hash_password(password)
        save_runtime(self.settings, ["connector_admin_password"])
        self.audit.write(tool="admin:password", client=f"pve:{user}", args={}, ok=True)
        return {"ok": True}

    # ---- HTTP -------------------------------------------------------------------------
    @staticmethod
    def _cors(request: Request, response: Response) -> Response:
        origin = request.headers.get("origin")
        if origin:
            # Authentication is the Proxmox ticket in a header, never a cookie, so allowing the
            # origin of whichever Proxmox node served the GUI is safe.
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-PVE-Ticket"
            response.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, OPTIONS"
            response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Cache-Control"] = "no-store"
        return response

    async def handle(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return self._cors(request, Response(status_code=204))
        action = request.path_params.get("action", "")
        try:
            user = await self._verify(request)
            body: dict[str, Any] = {}
            if request.method in ("PUT", "POST"):
                try:
                    body = await request.json()
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    raise AdminError(400, "Expected a JSON object.")
            route = (request.method, action)
            if route == ("GET", "status"):
                result: Any = await self.status(user)
            elif route == ("PUT", "settings"):
                result = await self.update_settings(user, body)
            elif route == ("GET", "audit"):
                result = await self.audit_tail(int(request.query_params.get("limit", 200)))
            elif route == ("POST", "revoke"):
                result = await self.revoke(user, body)
            elif route == ("POST", "password"):
                result = await self.set_password(user, body)
            else:
                raise AdminError(404, "Unknown admin action.")
            response: Response = JSONResponse({"data": result})
        except AdminError as exc:
            response = JSONResponse({"error": str(exc)}, status_code=exc.status)
        except ValueError as exc:
            response = JSONResponse({"error": str(exc)}, status_code=400)
        return self._cors(request, response)
