"""End-to-end OAuth flow as claude.ai performs it, against the real HTTP app."""

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from conftest import make_settings

PUBLIC = "https://pve-mcp.example.com"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.setattr("pve_connector.auth.FAIL_DELAY", 0)
    from pve_connector.server import build_server, transport_security

    settings = make_settings()
    app = build_server(settings, remote=True).streamable_http_app(
        transport_security=transport_security(settings)
    )
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as c:
        yield c


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(client, redirect=CALLBACK):
    return client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


def authorize_and_approve(client, password="correct horse battery"):
    reg = register(client).json()
    verifier, challenge = pkce()
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "proxmox",
            "resource": f"{PUBLIC}/mcp",
        },
    )
    assert r.status_code == 302, r.text
    approve_url = r.headers["location"]
    assert approve_url.startswith(f"{PUBLIC}/approve?txn=")
    txn = parse_qs(urlparse(approve_url).query)["txn"][0]
    page = client.get(f"/approve?txn={txn}")
    assert page.status_code == 200 and "Approve Proxmox access" in page.text
    r = client.post("/approve", data={"txn": txn, "password": password, "decision": "approve"})
    return reg, verifier, txn, r


def exchange(client, reg, verifier, code):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": reg["client_id"],
            "code_verifier": verifier,
            "resource": f"{PUBLIC}/mcp",
        },
    )


def mcp_initialize(client, token=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    return client.post("/mcp", json=body, headers=headers)


def test_metadata_is_published(client):
    prm = client.get("/.well-known/oauth-protected-resource/mcp")
    assert prm.status_code == 200
    assert prm.json()["authorization_servers"][0].rstrip("/") == PUBLIC
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"] == f"{PUBLIC}/authorize"
    assert meta["registration_endpoint"] == f"{PUBLIC}/register"
    assert "S256" in meta["code_challenge_methods_supported"]


def test_mcp_requires_token(client):
    r = mcp_initialize(client)
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_foreign_redirect_hosts_are_refused(client):
    r = register(client, "https://evil.example.net/callback")
    assert r.status_code == 400
    assert "not allowed" in r.text


def test_plain_http_redirect_refused(client):
    r = register(client, "http://claude.ai/api/mcp/auth_callback")
    assert r.status_code == 400 and "https" in r.text


def test_foreign_resource_refused(client):
    reg = register(client).json()
    _, challenge = pkce()
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "https://other.example.com/mcp",
        },
    )
    assert r.status_code == 302 and "invalid_target" in r.headers["location"]


def test_wrong_password_then_lockout(client):
    reg, verifier, txn, r = authorize_and_approve(client, password="nope")
    assert r.status_code == 200 and "Wrong password" in r.text
    for _ in range(5):
        r = client.post("/approve", data={"txn": txn, "password": "nope", "decision": "approve"})
    assert "Too many wrong attempts" in r.text
    # even the right password is refused while locked
    r = client.post("/approve", data={"txn": txn, "password": "correct horse battery", "decision": "approve"})
    assert r.status_code == 200 and "Too many" in r.text


def test_deny_returns_access_denied(client):
    reg = register(client).json()
    _, challenge = pkce()
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s1",
        },
    )
    txn = parse_qs(urlparse(r.headers["location"]).query)["txn"][0]
    r = client.post("/approve", data={"txn": txn, "decision": "deny"})
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["s1"]


def test_full_flow_initialize_and_refresh(client, tmp_path):
    reg, verifier, txn, r = authorize_and_approve(client)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith(CALLBACK)
    q = parse_qs(urlparse(location).query)
    assert q["state"] == ["xyz"]

    tokens = exchange(client, reg, verifier, q["code"][0])
    assert tokens.status_code == 200, tokens.text
    tokens = tokens.json()
    assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]

    # the code is single-use
    assert exchange(client, reg, verifier, q["code"][0]).status_code == 400
    # the approval link is single-use
    assert "expired" in client.get(f"/approve?txn={txn}").text

    init = mcp_initialize(client, tokens["access_token"])
    assert init.status_code == 200, init.text
    assert "proxmox" in init.text and "serverInfo" in init.text

    # tokens are stored hashed, never in clear text
    db = (tmp_path / "oauth.sqlite3").read_bytes()
    assert tokens["access_token"].encode() not in db

    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": reg["client_id"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    new = refreshed.json()
    assert new["access_token"] != tokens["access_token"]
    # rotation retires the old pair
    assert mcp_initialize(client, tokens["access_token"]).status_code == 401
    assert mcp_initialize(client, new["access_token"]).status_code == 200


def test_unknown_host_header_rejected(client):
    r = client.post("/mcp", json={}, headers={"Host": "attacker.example"})
    assert r.status_code in (400, 401, 421)


def test_password_hashing():
    from pve_connector.auth import check_password, hash_password

    hashed = hash_password("a long password!")
    assert re.match(r"^scrypt\$[0-9a-f]{32}\$[0-9a-f]+$", hashed)
    assert check_password("a long password!", hashed)
    assert not check_password("wrong", hashed)
    assert check_password("plain", "plain") and not check_password("plain", "other")
