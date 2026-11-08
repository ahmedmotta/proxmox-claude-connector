"""Optional SSH access to Proxmox nodes: host shell and `pct exec` for containers.

The Proxmox REST API has no endpoint for running shell commands on a node or inside an
LXC container, so these features go over SSH. Host keys are pinned on first use
(trust-on-first-use) and a changed key is refused.
"""

from __future__ import annotations

import shlex
import threading
import time
from dataclasses import dataclass

import anyio
import paramiko

from .config import Settings


class SSHDisabled(Exception):
    pass


@dataclass
class CommandResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False

    def as_dict(self, limit: int) -> dict:
        def clip(text: str) -> str:
            if len(text) <= limit:
                return text
            return f"...[{len(text) - limit} chars cut]...\n" + text[-limit:]

        return {
            "exit_code": self.exit_code,
            "stdout": clip(self.stdout),
            "stderr": clip(self.stderr),
            "timed_out": self.timed_out,
        }


class SSHRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._client: paramiko.SSHClient | None = None
        self._local_node: str | None = None
        self._known_hosts = settings.data_path / "known_hosts"

    @property
    def enabled(self) -> bool:
        return self.settings.pve_ssh_enabled

    def _connect(self) -> paramiko.SSHClient:
        if self._client is not None:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                return self._client
        client = paramiko.SSHClient()
        self._known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self._known_hosts.touch(exist_ok=True)
        client.load_host_keys(str(self._known_hosts))
        # Trust on first use: unknown host keys are saved, changed ones are rejected by paramiko.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        s = self.settings
        client.connect(
            hostname=s.ssh_host,
            port=s.pve_ssh_port,
            username=s.pve_ssh_user,
            key_filename=s.pve_ssh_key_path or None,
            password=s.pve_ssh_password or None,
            timeout=15,
            allow_agent=False,
            look_for_keys=not (s.pve_ssh_key_path or s.pve_ssh_password),
        )
        client.save_host_keys(str(self._known_hosts))
        self._client = client
        return client

    def _run_sync(self, command: str, timeout: float, stdin_data: str | None) -> CommandResult:
        with self._lock:
            client = self._connect()
            transport = client.get_transport()
            assert transport is not None
            channel = transport.open_session()
        channel.exec_command(command)
        if stdin_data:
            channel.sendall(stdin_data.encode())
        channel.shutdown_write()
        out, err = bytearray(), bytearray()
        timed_out = False
        deadline = time.monotonic() + timeout
        while True:
            while channel.recv_ready():
                out += channel.recv(65536)
            while channel.recv_stderr_ready():
                err += channel.recv_stderr(65536)
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
            time.sleep(0.05)
        exit_code = channel.recv_exit_status() if channel.exit_status_ready() else None
        channel.close()
        return CommandResult(exit_code, out.decode(errors="replace"), err.decode(errors="replace"), timed_out)

    async def run(self, command: str, timeout: float = 120, stdin_data: str | None = None) -> CommandResult:
        if not self.enabled:
            raise SSHDisabled(
                "SSH is turned off in the connector settings (PVE_SSH_ENABLED=false), so host shell and "
                "container commands are unavailable. VM commands still work through the QEMU guest agent."
            )
        return await anyio.to_thread.run_sync(self._run_sync, command, timeout, stdin_data)

    async def local_node(self) -> str:
        if self._local_node is None:
            result = await self.run("hostname", timeout=15)
            self._local_node = result.stdout.strip()
        return self._local_node

    async def run_on_node(
        self, node: str, command: str, timeout: float = 120, stdin_data: str | None = None
    ) -> CommandResult:
        """Run on a specific cluster node, hopping through the SSH host if needed.

        Proxmox cluster nodes trust each other's root SSH keys, so `ssh <node>` from any
        node reaches the others.
        """
        if node and node != await self.local_node():
            command = (
                f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new root@{shlex.quote(node)} "
                f"{shlex.quote(command)}"
            )
        return await self.run(command, timeout=timeout, stdin_data=stdin_data)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
