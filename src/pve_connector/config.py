"""Configuration, read from environment variables or an env file.

Every setting can be given as an environment variable with the same (upper-case) name,
or in the env file pointed to by ``PVE_CONNECTOR_ENV`` (default: ``/etc/pve-connector/env``,
falling back to ``./.env``).
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Mode(StrEnum):
    """How much the connector is allowed to do."""

    FULL = "full"  # everything, including deletes, host shell and guest commands
    SAFE = "safe"  # everything except destructive actions, host shell and raw DELETE calls
    READONLY = "readonly"  # read-only: listing, status, logs


def _default_env_file() -> str:
    explicit = os.environ.get("PVE_CONNECTOR_ENV")
    if explicit:
        return explicit
    system = Path("/etc/pve-connector/env")
    return str(system) if system.exists() else ".env"


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


IntList = Annotated[list[int], NoDecode]
StrList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_default_env_file(), env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- Proxmox API -----------------------------------------------------------------
    pve_host: str = Field(description="Proxmox host name or IP (any cluster node).")
    pve_port: int = 8006
    pve_token_id: str = Field(description="API token id, e.g. root@pam!claude")
    pve_token_secret: str = Field(description="API token secret (UUID).")
    pve_verify_ssl: bool = True
    pve_ca_cert: str | None = Field(default=None, description="Path to a CA bundle, e.g. pve-root-ca.pem")
    pve_timeout: float = 60.0

    # ---- Optional SSH (host shell + LXC exec) ----------------------------------------
    pve_ssh_enabled: bool = False
    pve_ssh_host: str | None = None  # defaults to pve_host
    pve_ssh_port: int = 22
    pve_ssh_user: str = "root"
    pve_ssh_key_path: str | None = None
    pve_ssh_password: str | None = None

    # ---- Connector -------------------------------------------------------------------
    connector_public_url: str = Field(
        default="http://127.0.0.1:8765",
        description="Public HTTPS URL Claude uses to reach the connector (your Cloudflare hostname).",
    )
    connector_bind_host: str = "127.0.0.1"
    connector_port: int = 8765
    connector_admin_password: str | None = Field(
        default=None, description="Password typed on the approval page when Claude connects."
    )
    connector_data_dir: str = "/var/lib/pve-connector"
    connector_mode: Mode = Mode.FULL
    connector_protected_vmids: IntList = Field(
        default_factory=list,
        description="VMIDs that can never be stopped, deleted, restored over or rolled back.",
    )
    connector_allowed_vmids: IntList = Field(
        default_factory=list, description="If set, the connector may only touch these VMIDs."
    )
    connector_allowed_redirect_hosts: StrList = Field(
        default_factory=lambda: ["claude.ai", "claude.com", "localhost", "127.0.0.1"],
        description="Hosts OAuth clients may redirect to. Blocks other apps from registering.",
    )
    connector_access_token_ttl: int = 3600
    connector_refresh_token_ttl: int = 60 * 60 * 24 * 30
    connector_max_output_chars: int = 60_000
    connector_max_wait: int = Field(
        default=90, description="Longest a single tool call waits for a Proxmox task, in seconds."
    )
    connector_log_level: str = "INFO"
    connector_paused: bool = Field(default=False, description="Kill switch: refuse every tool call.")

    @field_validator(
        "connector_protected_vmids",
        "connector_allowed_vmids",
        "connector_allowed_redirect_hosts",
        mode="before",
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("connector_public_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if "!" not in self.pve_token_id:
            raise ValueError("PVE_TOKEN_ID must look like user@realm!tokenname, e.g. root@pam!claude")
        return self

    # ---- Derived ---------------------------------------------------------------------
    @property
    def pve_base_url(self) -> str:
        host = self.pve_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # bare IPv6
        return f"https://{host}:{self.pve_port}/api2/json"

    @property
    def ssh_host(self) -> str:
        return self.pve_ssh_host or self.pve_host

    @cached_property
    def public_host(self) -> str:
        return urlparse(self.connector_public_url).netloc

    @property
    def data_path(self) -> Path:
        path = Path(self.connector_data_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
