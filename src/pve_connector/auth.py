"""Built-in OAuth 2.1 authorization server for the connector.

Claude (web, desktop or mobile) registers itself through dynamic client registration, then
sends the owner to an approval page served by this connector. The owner types the
connector's admin password once; Claude receives an access token plus a refresh token and
stays connected until the session is revoked.

Tokens are stored hashed (SHA-256) in a small SQLite database, so a leaked database does not
leak usable tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import Settings

SCOPE = "proxmox"
PENDING_TTL = 600
CODE_TTL = 300
MAX_FAILS_PER_IP = 5
MAX_FAILS_GLOBAL = 20  # above this, every attempt is slowed down (no hard lock, so no lock-out attack)
FAIL_DELAY = 1.0  # seconds added per recent failure from the same address
LOCK_SECONDS = 900


# ---- password hashing -----------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(candidate: str, configured: str) -> bool:
    if configured.startswith("scrypt$"):
        try:
            _, salt_hex, digest_hex = configured.split("$", 2)
        except ValueError:
            return False
        digest = hashlib.scrypt(candidate.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    return hmac.compare_digest(candidate.encode(), configured.encode())


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- storage --------------------------------------------------------------------------
class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL);
            CREATE TABLE IF NOT EXISTS pending (txn TEXT PRIMARY KEY, client_id TEXT, data TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS codes (code_hash TEXT PRIMARY KEY, data TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash TEXT PRIMARY KEY, kind TEXT, client_id TEXT, data TEXT,
                expires REAL, created REAL, family TEXT
            );
            """
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def execute(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def purge(self) -> None:
        now = time.time()
        self.execute("DELETE FROM pending WHERE expires < ?", (now,))
        self.execute("DELETE FROM codes WHERE expires < ?", (now,))
        self.execute("DELETE FROM tokens WHERE expires IS NOT NULL AND expires < ?", (now,))


class ConnectorOAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.data_path / "oauth.sqlite3")
        self._fails: dict[str, list[float]] = {}
        self._fails_lock = threading.Lock()

    # ---- clients ----------------------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self.store.execute("SELECT data FROM clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0][0]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        allowed = {h.lower() for h in self.settings.connector_allowed_redirect_hosts}
        for uri in client_info.redirect_uris or []:
            parsed = urlparse(str(uri))
            host = (parsed.hostname or "").lower()
            loopback = host in ("localhost", "127.0.0.1", "::1")
            if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description="redirect URIs must use https (http only for localhost)",
                )
            if allowed and host not in allowed:
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"redirect host '{host}' is not allowed by this connector",
                )
        self.store.execute(
            "INSERT OR REPLACE INTO clients VALUES (?, ?, ?)",
            (client_info.client_id, client_info.model_dump_json(), time.time()),
        )

    # ---- authorization ----------------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.settings.connector_admin_password:
            raise AuthorizeError(
                error="server_error",
                error_description="CONNECTOR_ADMIN_PASSWORD is not set on the connector server",
            )
        expected = f"{self.settings.connector_public_url}/mcp"
        if params.resource and params.resource.rstrip("/") != expected:
            raise AuthorizeError(
                error="invalid_target", error_description=f"this server only issues tokens for {expected}"
            )
        # Tokens are always bound to this connector, even when the client sends no resource indicator.
        params = params.model_copy(update={"resource": expected})
        self.store.purge()
        txn = secrets.token_urlsafe(32)
        self.store.execute(
            "INSERT INTO pending VALUES (?, ?, ?, ?)",
            (txn, client.client_id, params.model_dump_json(), time.time() + PENDING_TTL),
        )
        return f"{self.settings.connector_public_url}/approve?txn={txn}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        rows = self.store.execute(
            "SELECT data, expires FROM codes WHERE code_hash = ?", (_h(authorization_code),)
        )
        if not rows or rows[0][1] < time.time():
            return None
        code = AuthorizationCode.model_validate_json(rows[0][0])
        return code if code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        deleted = self.store.execute(
            "DELETE FROM codes WHERE code_hash = ? RETURNING code_hash", (_h(authorization_code.code),)
        )
        if not deleted:
            raise TokenError(error="invalid_grant", error_description="authorization code already used")
        return self._issue(
            client.client_id,
            authorization_code.scopes,
            authorization_code.resource,
            authorization_code.subject,
            family=secrets.token_hex(8),
        )

    # ---- tokens -----------------------------------------------------------------------
    def _issue(
        self, client_id: str, scopes: list[str], resource: str | None, subject: str | None, family: str
    ) -> OAuthToken:
        now = time.time()
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(48)
        a_exp = int(now + self.settings.connector_access_token_ttl)
        r_exp = int(now + self.settings.connector_refresh_token_ttl)
        meta = {"scopes": scopes, "resource": resource, "subject": subject}
        self.store.execute(
            "INSERT INTO tokens VALUES (?, 'access', ?, ?, ?, ?, ?)",
            (_h(access), client_id, json.dumps(meta), a_exp, now, family),
        )
        self.store.execute(
            "INSERT INTO tokens VALUES (?, 'refresh', ?, ?, ?, ?, ?)",
            (_h(refresh), client_id, json.dumps(meta), r_exp, now, family),
        )
        return OAuthToken(
            access_token=access,
            expires_in=self.settings.connector_access_token_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _load(self, token: str, kind: str) -> tuple[str, dict[str, Any], float, str] | None:
        rows = self.store.execute(
            "SELECT client_id, data, expires, family FROM tokens WHERE token_hash = ? AND kind = ?",
            (_h(token), kind),
        )
        if not rows:
            return None
        client_id, data, expires, family = rows[0]
        if expires is not None and expires < time.time():
            return None
        return client_id, json.loads(data), expires, family

    async def load_access_token(self, token: str) -> AccessToken | None:
        found = self._load(token, "access")
        if not found:
            return None
        client_id, meta, expires, _ = found
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=meta["scopes"],
            expires_at=int(expires),
            resource=meta.get("resource"),
            subject=meta.get("subject"),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        found = self._load(refresh_token, "refresh")
        if not found or found[0] != client.client_id:
            return None
        client_id, meta, expires, _ = found
        return RefreshToken(
            token=refresh_token,
            client_id=client_id,
            scopes=meta["scopes"],
            expires_at=int(expires),
            resource=meta.get("resource"),
            subject=meta.get("subject"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Atomic: only the request that actually deletes the refresh token may rotate it.
        rows = self.store.execute(
            "DELETE FROM tokens WHERE token_hash = ? AND kind = 'refresh' AND client_id = ? "
            "AND (expires IS NULL OR expires > ?) RETURNING family",
            (_h(refresh_token.token), client.client_id, time.time()),
        )
        if not rows:
            raise TokenError(error="invalid_grant", error_description="refresh token expired or revoked")
        # Rotate: the whole previous family (old access token too) is retired.
        self.store.execute("DELETE FROM tokens WHERE family = ?", (rows[0][0],))
        return self._issue(
            client.client_id,
            scopes or refresh_token.scopes,
            refresh_token.resource,
            refresh_token.subject,
            family=secrets.token_hex(8),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        rows = self.store.execute("SELECT family FROM tokens WHERE token_hash = ?", (_h(token.token),))
        if rows:
            self.store.execute("DELETE FROM tokens WHERE family = ?", (rows[0][0],))

    # ---- admin helpers (CLI) ----------------------------------------------------------
    def sessions(self) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT t.client_id, c.data, MIN(t.created), MAX(t.expires) FROM tokens t "
            "LEFT JOIN clients c ON c.client_id = t.client_id WHERE t.kind = 'refresh' "
            "AND (t.expires IS NULL OR t.expires > ?) GROUP BY t.family",
            (time.time(),),
        )
        out = []
        for client_id, data, created, expires in rows:
            name = json.loads(data).get("client_name") if data else None
            out.append(
                {
                    "client_id": client_id,
                    "client_name": name,
                    "connected": time.strftime("%Y-%m-%d %H:%M", time.localtime(created)),
                    "expires": time.strftime("%Y-%m-%d %H:%M", time.localtime(expires)),
                }
            )
        return out

    def revoke_client(self, client_id: str) -> int:
        count = len(self.store.execute("SELECT 1 FROM tokens WHERE client_id = ?", (client_id,)))
        self.store.execute("DELETE FROM tokens WHERE client_id = ?", (client_id,))
        return count

    def revoke_all(self) -> int:
        count = len(self.store.execute("SELECT 1 FROM tokens"))
        self.store.execute("DELETE FROM tokens")
        self.store.execute("DELETE FROM codes")
        return count

    # ---- brute-force protection -------------------------------------------------------
    def _locked(self, ip: str) -> bool:
        now = time.time()
        with self._fails_lock:
            for key in list(self._fails):
                self._fails[key] = [t for t in self._fails[key] if now - t < LOCK_SECONDS]
                if not self._fails[key]:
                    del self._fails[key]
            return len(self._fails.get(ip, [])) >= MAX_FAILS_PER_IP

    def _delay(self, ip: str) -> float:
        with self._fails_lock:
            own = len(self._fails.get(ip, []))
            total = sum(len(v) for v in self._fails.values())
        return FAIL_DELAY * (own + (5 if total >= MAX_FAILS_GLOBAL else 0))

    def _fail(self, ip: str) -> None:
        with self._fails_lock:
            self._fails.setdefault(ip, []).append(time.time())

    # ---- approval page ----------------------------------------------------------------
    def _pending(self, txn: str) -> tuple[OAuthClientInformationFull | None, AuthorizationParams | None]:
        rows = self.store.execute("SELECT client_id, data, expires FROM pending WHERE txn = ?", (txn,))
        if not rows or rows[0][2] < time.time():
            return None, None
        client_rows = self.store.execute("SELECT data FROM clients WHERE client_id = ?", (rows[0][0],))
        client = OAuthClientInformationFull.model_validate_json(client_rows[0][0]) if client_rows else None
        return client, AuthorizationParams.model_validate_json(rows[0][1])

    async def approve_page(self, request: Request) -> Response:
        txn = request.query_params.get("txn") if request.method == "GET" else None
        if request.method == "POST":
            form = await request.form()
            txn = str(form.get("txn", ""))
        client, params = self._pending(txn or "")
        if not client or not params:
            return _page(
                "Link expired",
                "This approval link has expired. Start the connection again from Claude.",
                status=400,
            )

        if request.method == "GET":
            return _approval_form(txn or "", client, params)

        ip = _client_ip(request)
        if form.get("decision") == "deny":
            self.store.execute("DELETE FROM pending WHERE txn = ?", (txn,))
            return RedirectResponse(
                construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state),
                status_code=302,
            )
        if self._locked(ip):
            return _approval_form(
                txn or "", client, params, error="Too many wrong attempts. Try again in 15 minutes."
            )
        password = str(form.get("password", ""))
        configured = self.settings.connector_admin_password or ""
        if not configured or not check_password(password, configured):
            self._fail(ip)
            await anyio.sleep(self._delay(ip))
            return _approval_form(txn or "", client, params, error="Wrong password.")

        self.store.execute("DELETE FROM pending WHERE txn = ?", (txn,))
        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="owner",
        )
        self.store.execute(
            "INSERT INTO codes VALUES (?, ?, ?)",
            (_h(code), auth_code.model_dump_json(), auth_code.expires_at),
        )
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), status_code=302
        )


def _client_ip(request: Request) -> str:
    """The visitor's address. Cloudflare's header is trusted only when the request comes from the
    local tunnel daemon (loopback or a private network), never straight from the internet."""
    peer = request.client.host if request.client else ""
    try:
        trusted = ipaddress.ip_address(peer).is_loopback or ipaddress.ip_address(peer).is_private
    except ValueError:
        trusted = False
    return (request.headers.get("cf-connecting-ip") if trusted else None) or peer or "unknown"


# ---- HTML -----------------------------------------------------------------------------
_CSS = """
:root{--bg:#f4f5f7;--card:#fff;--ink:#16181d;--muted:#5d6470;--line:#e2e5ea;--accent:#e57000;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--ink:#eceef2;--muted:#9aa2ae;
--line:#2a2f38;--accent:#ff8a1f;--bad:#ef5350}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}
.card{width:100%;max-width:440px;background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:28px}h1{font-size:20px;margin:0 0 6px}p{color:var(--muted);margin:0 0 18px}
.row{display:flex;justify-content:space-between;gap:12px;
padding:8px 0;border-top:1px solid var(--line);font-size:14px}.row b{font-weight:600;text-align:right;
word-break:break-all}label{display:block;font-size:14px;margin:18px 0 6px}input{width:100%;padding:11px 12px;
border:1px solid var(--line);border-radius:9px;background:transparent;color:var(--ink);font-size:16px}
.btns{display:flex;flex-direction:row-reverse;gap:10px;margin-top:18px}button{flex:1;padding:11px;border-radius:9px;border:1px solid
var(--line);font-size:15px;cursor:pointer;background:transparent;color:var(--ink)}
button.go{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.err{color:var(--bad);font-size:14px;margin-top:12px}.warn{font-size:13px;margin-top:16px}
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
        f"content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main class='card'>"
        f"<h1>{html.escape(title)}</h1><p>{body}</p></main></body></html>",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
        },
    )


def _approval_form(
    txn: str, client: OAuthClientInformationFull, params: AuthorizationParams, error: str | None = None
) -> HTMLResponse:
    name = html.escape(client.client_name or "An MCP client")
    redirect_host = html.escape(str(params.redirect_uri))
    err = f"<div class='err'>{html.escape(error)}</div>" if error else ""
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Approve Proxmox access</title><style>{_CSS}</style></head><body><main class="card">
<h1>Approve Proxmox access</h1>
<p>{name} is asking to manage this Proxmox server through the connector.</p>
<div class="row"><span>Application</span><b>{name}</b></div>
<div class="row"><span>Returns to</span><b>{redirect_host}</b></div>
<form method="post" action="approve" autocomplete="off">
<input type="hidden" name="txn" value="{html.escape(txn)}">
<label for="pw">Connector admin password</label>
<input id="pw" type="password" name="password" autofocus>
{err}
<div class="btns"><button class="go" type="submit" name="decision" value="approve">Approve</button>
<button type="submit" name="decision" value="deny" formnovalidate>Deny</button></div>
</form>
<p class="warn">Only approve if you started this connection yourself.</p>
</main></body></html>"""
    return HTMLResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
        },
    )
