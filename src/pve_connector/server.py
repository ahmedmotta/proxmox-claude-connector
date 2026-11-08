"""Builds the MCP server (remote HTTP with OAuth, or local stdio)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .admin import AdminAPI, load_runtime
from .auth import SCOPE, ConnectorOAuthProvider, _page
from .config import Settings
from .policy import AuditLog
from .tools import INSTRUCTIONS, register_tools

log = logging.getLogger("pve_connector")


def build_server(settings: Settings, *, remote: bool) -> MCPServer:
    load_runtime(settings)  # changes saved from the Proxmox GUI win over the env file
    audit = AuditLog(settings.data_path / "audit.log")
    provider: ConnectorOAuthProvider | None = None
    auth: AuthSettings | None = None
    if remote:
        provider = ConnectorOAuthProvider(settings)
        auth = AuthSettings(
            issuer_url=settings.connector_public_url,
            resource_server_url=f"{settings.connector_public_url}/mcp",
            service_documentation_url=None,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
            validate_token_resource=True,
        )

    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            pve = state.get("pve")
            ssh = state.get("ssh")
            if ssh is not None:
                ssh.close()
            if pve is not None:
                await pve.aclose()

    server = MCPServer(
        name="proxmox",
        title="Proxmox VE",
        description="Full control of Proxmox VE: VMs, containers, storage, backups, networking and the host.",
        instructions=INSTRUCTIONS,
        website_url="https://innithub.online",
        version=__version__,
        auth_server_provider=provider,
        auth=auth,
        lifespan=lifespan,
        log_level=settings.connector_log_level.upper(),  # type: ignore[arg-type]
    )
    state["pve"], state["ssh"] = register_tools(server, settings, audit)

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "version": __version__, "paused": settings.connector_paused},
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
        )

    @server.custom_route("/", methods=["GET"])
    async def home(_: Request) -> Response:
        return _page(
            "Proxmox Claude Connector",
            "This server is a Model Context Protocol connector. Add "
            f"<b>{settings.connector_public_url}/mcp</b> as a custom connector in Claude.",
        )

    if provider is not None:

        @server.custom_route("/approve", methods=["GET", "POST"])
        async def approve(request: Request) -> Response:
            return await provider.approve_page(request)

        admin = AdminAPI(settings, provider, audit)
        server.admin = admin  # type: ignore[attr-defined]

        @server.custom_route("/admin/api/{action}", methods=["GET", "PUT", "POST", "OPTIONS"])
        async def admin_api(request: Request) -> Response:
            return await admin.handle(request)

    return server


def transport_security(settings: Settings) -> TransportSecuritySettings:
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", settings.public_host]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(set(hosts)),
        allowed_origins=[
            "https://claude.ai",
            "https://claude.com",
            settings.connector_public_url,
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    )


def serve_http(settings: Settings) -> None:
    import anyio

    server = build_server(settings, remote=True)
    log.info(
        "Proxmox connector %s listening on %s:%s, public URL %s",
        __version__,
        settings.connector_bind_host,
        settings.connector_port,
        settings.connector_public_url,
    )
    anyio.run(
        lambda: server.run_streamable_http_async(
            host=settings.connector_bind_host,
            port=settings.connector_port,
            streamable_http_path="/mcp",
            transport_security=transport_security(settings),
        )
    )


def serve_stdio(settings: Settings) -> None:
    build_server(settings, remote=False).run("stdio")
