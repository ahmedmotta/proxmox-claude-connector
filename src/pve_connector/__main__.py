"""Command line: pve-connector [serve|stdio|check|hash-password|sessions|revoke-all]."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys

from . import __version__


def _settings():
    from pydantic import ValidationError

    from .config import load_settings

    try:
        return load_settings()
    except ValidationError as exc:
        print("Configuration error:\n", exc, file=sys.stderr)
        print("\nSee .env.example for every setting.", file=sys.stderr)
        sys.exit(2)


async def _check(settings) -> int:
    from .proxmox import ProxmoxClient, ProxmoxError
    from .ssh import SSHRunner

    ok = True
    pve = ProxmoxClient(settings)
    try:
        version = await pve.get("/version")
        nodes = await pve.nodes()
        guests = await pve.guests()
        print(f"[ok] Proxmox API {settings.pve_base_url} - version {version.get('version')}")
        print(f"[ok] nodes: {', '.join(nodes)} - guests visible: {len(guests)}")
        perms = await pve.get("/access/permissions")
        root = perms.get("/", {}) if isinstance(perms, dict) else {}
        if not root.get("Sys.Modify"):
            print(
                "[warn] the API token lacks Sys.Modify on '/'. For full control give the token the "
                "Administrator role on '/' and turn privilege separation off (see README)."
            )
    except ProxmoxError as exc:
        ok = False
        print(f"[fail] {exc}")
    finally:
        await pve.aclose()

    ssh = SSHRunner(settings)
    if ssh.enabled:
        try:
            result = await ssh.run("pveversion", timeout=20)
            print(f"[ok] SSH {settings.pve_ssh_user}@{settings.ssh_host}: {result.stdout.strip()}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[fail] SSH: {exc}")
        finally:
            ssh.close()
    else:
        print("[info] SSH is off: host_shell and container commands are disabled")

    print(f"[info] mode: {settings.connector_mode.value}; public URL: {settings.connector_public_url}/mcp")
    if not settings.connector_admin_password:
        ok = False
        print("[fail] CONNECTOR_ADMIN_PASSWORD is not set; nobody could approve Claude's connection")
    if not settings.connector_public_url.startswith("https://"):
        print("[warn] CONNECTOR_PUBLIC_URL is not https; Claude on the web needs an https URL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pve-connector", description="Proxmox Claude Connector")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the remote connector (HTTP + OAuth) - default")
    sub.add_parser("stdio", help="run locally over stdio for Claude Desktop / Claude Code")
    sub.add_parser("check", help="test the Proxmox API and SSH settings")
    sub.add_parser("hash-password", help="hash an admin password for CONNECTOR_ADMIN_PASSWORD")
    sub.add_parser("sessions", help="list connected Claude sessions")
    sub.add_parser("revoke-all", help="disconnect every Claude session")
    args = parser.parse_args(argv)
    os.umask(0o077)  # files the connector creates (database, audit log, keys) stay private
    command = args.command or "serve"

    if command == "hash-password":
        from .auth import hash_password

        first = getpass.getpass("New admin password: ")
        if len(first) < 12:
            sys.exit("Use at least 12 characters.")
        if getpass.getpass("Again: ") != first:
            sys.exit("Passwords do not match.")
        print(hash_password(first))
        return

    settings = _settings()
    logging.basicConfig(
        level=settings.connector_log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Per-request lines from the HTTP and SSH libraries are noise in the journal and in `check`.
    for noisy in ("httpx", "httpcore", "paramiko"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if command == "serve":
        from .server import serve_http

        serve_http(settings)
    elif command == "stdio":
        from .server import serve_stdio

        serve_stdio(settings)
    elif command == "check":
        sys.exit(asyncio.run(_check(settings)))
    elif command in ("sessions", "revoke-all"):
        from .auth import ConnectorOAuthProvider

        provider = ConnectorOAuthProvider(settings)
        if command == "sessions":
            print(json.dumps(provider.sessions(), indent=2, ensure_ascii=False))
        else:
            print(f"Revoked {provider.revoke_all()} tokens. Claude must reconnect.")


if __name__ == "__main__":
    main()
