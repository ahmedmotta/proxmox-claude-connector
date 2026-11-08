"""Permission policy and audit log.

Every tool declares what kind of action it is. The policy decides, from the configured
mode, the protected VMIDs and the VMID allowlist, whether the action may run.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import Mode, Settings


class Kind(StrEnum):
    READ = "read"  # look, never change
    WRITE = "write"  # create or change, recoverable
    DESTRUCTIVE = "destructive"  # delete, power off, roll back, overwrite
    SHELL = "shell"  # arbitrary commands on a node or inside a guest


class PolicyError(Exception):
    """Raised when the configured policy forbids an action. The message explains why."""


_ALLOWED: dict[Mode, set[Kind]] = {
    Mode.FULL: {Kind.READ, Kind.WRITE, Kind.DESTRUCTIVE, Kind.SHELL},
    Mode.SAFE: {Kind.READ, Kind.WRITE},
    Mode.READONLY: {Kind.READ},
}

_GUEST_IN_PATH = re.compile(r"/(?:qemu|lxc)/(\d+)(?=/|$)")
# Anything that reaches a shell, a console or files inside a guest or backup.
_SHELL_PATH = re.compile(r"(/agent(/|$))|(/file-restore/)|(shell$)|(proxy$)|(websocket$)|(/execute$)")
# The only state-changing calls `safe` mode allows through proxmox_api, with whether each may touch
# a protected guest. Every other POST/PUT/DELETE counts as destructive (default deny).
_SAFE_WRITES: list[tuple[str, re.Pattern[str], bool]] = [
    ("POST", re.compile(r"^/nodes/[^/]+/(qemu|lxc)/\d+/status/(start|resume)$"), True),
    ("POST", re.compile(r"^/nodes/[^/]+/(qemu|lxc)/\d+/status/(shutdown|reboot)$"), False),
    ("POST", re.compile(r"^/nodes/[^/]+/(qemu|lxc)/\d+/snapshot$"), True),
    ("PUT", re.compile(r"^/nodes/[^/]+/(qemu|lxc)/\d+/snapshot/[^/]+/config$"), True),
    ("POST", re.compile(r"^/nodes/[^/]+/vzdump$"), True),
    ("POST", re.compile(r"^/nodes/[^/]+/apt/update$"), True),
    ("POST", re.compile(r"^/nodes/[^/]+/storage/[^/]+/download-url$"), True),
    ("POST", re.compile(r"^/nodes/[^/]+/aplinfo$"), True),
]
# Body parameters that name guests.
_VMID_KEYS = re.compile(r"^(vmid|newid|vms|target-vmid|sid|vmids)$", re.I)
_SECRET_KEYS = re.compile(r"pass|secret|token|key|cipassword|sshkeys|stdin|content|input-data", re.I)


def vmids_in_params(params: dict[str, Any] | None) -> set[int]:
    found: set[int] = set()
    for key, value in (params or {}).items():
        if not _VMID_KEYS.match(str(key)):
            continue
        values = value if isinstance(value, list | tuple) else [value]
        for v in values:
            found.update(int(n) for n in re.findall(r"\d+", str(v)))
    return found


class Policy:
    """Reads the settings live, so changes made from the Proxmox GUI apply immediately."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def mode(self) -> Mode:
        return Mode(self.settings.connector_mode)

    @property
    def protected(self) -> set[int]:
        return set(self.settings.connector_protected_vmids)

    @property
    def allowed(self) -> set[int]:
        return set(self.settings.connector_allowed_vmids)

    def check_not_paused(self) -> None:
        if self.settings.connector_paused:
            raise PolicyError(
                "The connector is paused by the Proxmox administrator (Datacenter > Claude Connector). "
                "Nothing can be done through it until it is resumed."
            )

    # ---- checks ---------------------------------------------------------------------
    def check(self, kind: Kind, vmid: int | None = None, *, protected_ok: bool = False) -> None:
        if kind not in _ALLOWED[self.mode]:
            raise PolicyError(
                f"The connector runs in '{self.mode.value}' mode, which does not allow {kind.value} actions. "
                "The administrator can change it in Proxmox under Datacenter > Claude Connector."
            )
        if vmid is not None:
            self.check_vmid(vmid)
            if vmid in self.protected and kind != Kind.READ and not protected_ok:
                raise PolicyError(
                    f"VMID {vmid} is protected (CONNECTOR_PROTECTED_VMIDS); this action is blocked for it."
                )

    def check_vmid(self, vmid: int) -> None:
        if self.allowed and int(vmid) not in self.allowed:
            raise PolicyError(f"VMID {vmid} is outside the connector's allowlist (CONNECTOR_ALLOWED_VMIDS).")

    def visible(self, vmid: int) -> bool:
        return not self.allowed or int(vmid) in self.allowed

    def classify_api_call(self, method: str, path: str) -> tuple[Kind, set[int], bool]:
        """Return (kind, every VMID in the path, whether protected guests may be touched).
        `path` must already be normalized by proxmox.normalize_path."""
        method = method.upper()
        vmids = {int(v) for v in _GUEST_IN_PATH.findall(path)}
        if _SHELL_PATH.search(path):
            return Kind.SHELL, vmids, True
        if method == "GET":
            return Kind.READ, vmids, True
        for verb, pattern, protected_ok in _SAFE_WRITES:
            if verb == method and pattern.match(path):
                return Kind.WRITE, vmids, protected_ok
        return Kind.DESTRUCTIVE, vmids, False

    def check_api_call(self, method: str, path: str, params: dict[str, Any] | None) -> Kind:
        if self.allowed:
            raise PolicyError(
                "Raw API access is off while CONNECTOR_ALLOWED_VMIDS is set, because it cannot be limited "
                "to those guests. Use the dedicated tools instead."
            )
        kind, vmids, protected_ok = self.classify_api_call(method, path)
        self.check(kind)
        for vmid in vmids | vmids_in_params(params):
            self.check(kind, vmid, protected_ok=protected_ok)
        return kind

    def check_unscoped(self, what: str) -> None:
        """For tools that reach everything at once (host shell)."""
        if self.allowed:
            raise PolicyError(
                f"{what} is off while CONNECTOR_ALLOWED_VMIDS limits the connector to some guests."
            )

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "allowed_action_kinds": sorted(k.value for k in _ALLOWED[self.mode]),
            "protected_vmids": sorted(self.protected),
            "vmid_allowlist": sorted(self.allowed) or "all",
        }


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if _SECRET_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "...[cut]"
    return value


class AuditLog:
    """Append-only JSON-lines record of every tool call."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, **entry: Any) -> None:
        entry = {"time": datetime.now(UTC).isoformat(timespec="seconds"), **redact(entry)}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
