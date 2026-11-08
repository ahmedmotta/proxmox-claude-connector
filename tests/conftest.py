import json
from typing import Any

import pytest
import respx

BASE = "https://pve.test:8006/api2/json"


@pytest.fixture
def env(tmp_path, monkeypatch):
    values = {
        "PVE_HOST": "pve.test",
        "PVE_TOKEN_ID": "root@pam!claude",
        "PVE_TOKEN_SECRET": "00000000-0000-0000-0000-000000000000",
        "CONNECTOR_DATA_DIR": str(tmp_path),
        "CONNECTOR_PUBLIC_URL": "https://pve-mcp.example.com",
        "CONNECTOR_ADMIN_PASSWORD": "correct horse battery",
        "PVE_CONNECTOR_ENV": str(tmp_path / "none.env"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def make_settings(**overrides: Any):
    from pve_connector.config import Settings

    return Settings(**overrides)  # type: ignore[call-arg]


def tool_payload(result) -> Any:
    """Turn a CallToolResult into Python data."""
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except ValueError:
        return text


GUESTS = [
    {
        "vmid": 100,
        "name": "pbx",
        "type": "qemu",
        "node": "pve",
        "status": "running",
        "maxmem": 4 * 1024**3,
        "mem": 1024**3,
        "maxcpu": 2,
        "cpu": 0.05,
        "maxdisk": 32 * 1024**3,
        "uptime": 7200,
    },
    {
        "vmid": 101,
        "name": "web",
        "type": "lxc",
        "node": "pve",
        "status": "stopped",
        "maxmem": 2 * 1024**3,
        "maxcpu": 1,
        "maxdisk": 8 * 1024**3,
    },
]
UPID = "UPID:pve:0000ABCD:00112233:65000000:qmstart:100:root@pam!claude:"


@pytest.fixture
def pve_api():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/cluster/resources", params={"type": "vm"}).respond(json={"data": GUESTS})
        mock.get("/nodes").respond(json={"data": [{"node": "pve"}]})
        mock.get("/cluster/nextid").respond(json={"data": "102"})
        mock.get(url__regex=r".*/tasks/UPID.*/status").respond(
            json={"data": {"status": "stopped", "exitstatus": "OK", "type": "qmstart"}}
        )
        mock.get(url__regex=r".*/tasks/UPID.*/log.*").respond(json={"data": [{"n": 1, "t": "TASK OK"}]})
        yield mock
