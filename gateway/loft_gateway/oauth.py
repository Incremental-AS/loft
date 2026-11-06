"""OAuth 2.1 sign-in for assistants (MCP authorization): the gateway is both the
protected resource (/mcp) and its authorization server.

The flow an MCP client (Jeevo) runs by itself:
  1. POST /mcp without a token: 401 with `resource_metadata` (RFC 9728).
  2. It reads the metadata, registers itself (RFC 7591, public client, PKCE S256 only)
     and opens /oauth/authorize in the user's browser.
  3. The page names the client and where it returns to, and asks for an APPROVAL CODE:
     a 10-minute, single-use code made in a signed-in Loft app (or on the gateway host,
     `loft-gateway mcp code`). The page never asks for the NAS password: it is served
     through the tunnel, where TLS ends at the edge (design.md §B.4).
  4. Approve: the code's wrapped password is re-wrapped under a one-time authorization
     code (5 minutes) and the browser goes back to the client. The client trades it with
     its PKCE verifier for an access token (2 hours) and a refresh token that doesn't
     rotate (service.refresh_oauth says why); a reused authorization code ends the
     connection it made.

The connection then shows in the user's device list like any device. It opens nothing
until the user chooses folders in the assistant (mcp.py, loft/access/set).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import crypto
from .service import ApiError, Gateway, rate_ip

REQUEST_TTL = 15 * 60          # the sign-in page
AUTH_CODE_TTL = 5 * 60         # the authorization code
CLIENTS_UNUSED_MAX = 500       # registered but never signed in
REGISTER_PER_IP_HOUR = 10
PAGES_OPEN_PER_IP = 20         # sign-in pages not yet answered (each is a stored row)
PAGES_OPEN_MAX = 2000
FORM_MAX = 8192
_CHALLENGE = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}


def base_url(gw: Gateway, route: str) -> str:
    """This listener's own address as clients know it; '' = no OAuth here."""
    return ((gw.s.assistant_url or gw.s.public_url) if route == "public"
            else gw.s.lan_url).rstrip("/")


def www_authenticate(gw: Gateway, route: str, error: str | None = None) -> str:
    base = base_url(gw, route)
    parts = ['Bearer realm="loft"']
    if base:
        parts.append(f'resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"')
    if error:
        parts.append(f'error="{error}"')
    return ", ".join(parts)


def _redirect_ok(uri, allowed_hosts=()) -> bool:
    """https (http only to loopback); with assistant_return_hosts set, only those hosts
    or loopback."""
    if not isinstance(uri, str) or len(uri) > 500 or "#" in uri:
        return False
    p = urlparse(uri)
    if p.username or p.password or not p.hostname:
        return False
    if p.hostname in _LOOPBACK:
        return p.scheme in ("http", "https")
    if allowed_hosts and p.hostname.lower() not in {h.lower() for h in allowed_hosts}:
        return False
    return p.scheme == "https"


def _clean_name(v) -> str:
    v = v if isinstance(v, str) else ""
    v = "".join(ch for ch in v if ch.isprintable()).strip()[:60]
    return v or "Assistant"


def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _token_error(error: str, status: int = 400, desc: str | None = None) -> Response:
    body = {"error": error, **({"error_description": desc} if desc else {})}
    return _no_store(JSONResponse(body, status_code=status))


def _back(uri: str, **params) -> Response:
    sep = "&" if urlparse(uri).query else "?"
    return _no_store(RedirectResponse(uri + sep + urlencode(
        {k: v for k, v in params.items() if v is not None}), status_code=302))


async def _form(req: Request) -> dict:
    raw = await req.body()
    if len(raw) > FORM_MAX:
        return {}
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}


# ── the page ──────────────────────────────────────────────────────────────

_CSS = ("body{font:16px/1.5 -apple-system,system-ui,sans-serif;background:#f6f5f2;color:#14213d;"
        "margin:0;display:flex;justify-content:center}main{max-width:30rem;margin:3rem 1rem;"
        "background:#fff;padding:2rem;border-radius:12px;box-shadow:0 1px 4px #0002}"
        "h1{font-size:1.4rem;margin:0 0 1rem}input{font:inherit;font-size:1.3rem;"
        "letter-spacing:.15em;width:100%;box-sizing:border-box;padding:.6rem;margin:.4rem 0 1rem;"
        "border:1px solid #bbb;border-radius:8px;text-transform:uppercase}"
        "button{font:inherit;padding:.6rem 1.2rem;border-radius:8px;border:0;margin-right:.5rem}"
        ".go{background:#14213d;color:#fff}.no{background:#e8e6e1}.err{color:#a3271f}"
        "small,details{color:#555}code{background:#f0eee9;padding:0 .2em;border-radius:3px}")


def _page(title: str, body: str, form_to: str | None = None, status: int = 200) -> Response:
    csp = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'"
    if form_to:
        p = urlparse(form_to)
        csp += f" {p.scheme}://{p.netloc}"      # the 302 after the form goes there
    doc = (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<meta name=referrer content=no-referrer><title>{html.escape(title)}</title>"
           f"<style>{_CSS}</style></head><body><main>{body}</main></body></html>")
    resp = HTMLResponse(doc, status_code=status)
    resp.headers.update({"Content-Security-Policy": csp, "X-Frame-Options": "DENY",
                         "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
    return _no_store(resp)


def _error_page(msg: str, status: int = 400) -> Response:
    return _page("Loft", f"<h1>Can't sign in</h1><p>{html.escape(msg)}</p>", status=status)


def _approve_page(rid: str, client: dict, redirect_uri: str, error: str = "") -> Response:
    name = html.escape(client["name"])
    host = html.escape(urlparse(redirect_uri).hostname or "")
    err = f"<p class=err>{html.escape(error)}</p>" if error else ""
    body = (
        f"<h1>Connect {name} to Loft<br><small>returns to {host}</small></h1>"
        f"<p><strong>{name}</strong> wants to read and save files in your Loft. After you "
        f"approve, you choose in {name} which folder it works in and what else it may read. "
        f"Until then it can open nothing.</p>"
        f"<p><small>Approving sends you back to <strong>{host}</strong>. If you didn't start "
        f"this from {name}, cancel.</small></p>{err}"
        f"<form method=post action='/oauth/authorize'>"
        f"<input type=hidden name=request_id value='{html.escape(rid)}'>"
        f"<label for=code>Approval code</label>"
        f"<input id=code name=code autocomplete=one-time-code autocapitalize=characters "
        f"spellcheck=false placeholder='XXXX-XXXX-XXXX' autofocus>"
        f"<button class=go name=action value=allow>Approve</button>"
        f"<button class=no name=action value=deny>Cancel</button></form>"
        f"<details><summary>Where do I get a code?</summary><p>In a Loft app where you're "
        f"signed in to this gateway: Settings › Connect an assistant. Or ask whoever runs "
        f"your Loft gateway to run <code>sudo loft-gateway mcp code --user &lt;your NAS "
        f"user name&gt;</code> and type your NAS password there. A code works once, within "
        f"10 minutes.</p></details>")
    return _page(f"Connect {client['name']} to Loft", body, form_to=redirect_uri)


# ── routes ────────────────────────────────────────────────────────────────

def install(app, gw: Gateway, route: str, ip_of) -> None:

    def need_base() -> str:
        base = base_url(gw, route)
        if not base:
            raise ApiError(404, "not_found")
        return base

    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    @app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    def protected_resource():
        base = need_base()
        return {"resource": f"{base}/mcp", "authorization_servers": [base],
                "bearer_methods_supported": ["header"], "resource_name": "Loft"}

    @app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    def authorization_server():
        base = need_base()
        return {"issuer": base,
                "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "registration_endpoint": f"{base}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "authorization_response_iss_parameter_supported": True}

    @app.post("/oauth/register", include_in_schema=False)
    async def register(req: Request):
        need_base()
        raw = await req.body()
        try:
            doc = json.loads(raw) if len(raw) <= FORM_MAX else None
        except ValueError:
            doc = None
        if not isinstance(doc, dict):
            return _token_error("invalid_client_metadata", desc="Send a JSON object.")
        uris = doc.get("redirect_uris")
        if (not isinstance(uris, list) or not 1 <= len(uris) <= 5
                or not all(_redirect_ok(u, gw.s.assistant_return_hosts) for u in uris)):
            return _token_error("invalid_redirect_uri",
                                desc="1 to 5 https redirect URIs (http only to loopback)"
                                     + (" on a host this gateway allows."
                                        if gw.s.assistant_return_hosts else "."))
        method = doc.get("token_endpoint_auth_method", "none")
        if method not in ("none", "client_secret_basic", "client_secret_post"):
            return _token_error("invalid_client_metadata", desc="Public clients only.")
        ip = ip_of(req)
        now = time.time()

        def store():
            with gw.db.tx() as c:
                c.execute("DELETE FROM oauth_clients WHERE used_at IS NULL AND created_at<?",
                          (now - 86400,))
                n_ip = c.execute("SELECT COUNT(*) FROM oauth_clients WHERE created_ip=? AND"
                                 " created_at>?", (rate_ip(ip), now - 3600)).fetchone()[0]
                n_unused = c.execute("SELECT COUNT(*) FROM oauth_clients WHERE used_at IS NULL"
                                     ).fetchone()[0]
                if n_ip >= REGISTER_PER_IP_HOUR or n_unused >= CLIENTS_UNUSED_MAX:
                    return None
                cid = "lc_" + secrets.token_urlsafe(18)
                c.execute("INSERT INTO oauth_clients (id, name, redirect_uris, created_at,"
                          " created_ip) VALUES (?,?,?,?,?)",
                          (cid, _clean_name(doc.get("client_name")), json.dumps(uris), now,
                           rate_ip(ip)))
                return cid
        cid = await run_in_threadpool(store)
        if cid is None:
            return _token_error("temporarily_unavailable", 429, "Too many registrations.")
        gw.db.audit("oauth_client_registered", ip=ip, route=route, client_id=cid,
                    client_name=_clean_name(doc.get("client_name")))
        # A secret was asked for and none is given: RFC 7591 lets the server decide, and
        # the client then uses "none" (PKCE is what protects the code).
        return _no_store(JSONResponse({
            "client_id": cid, "client_id_issued_at": int(now),
            "client_name": _clean_name(doc.get("client_name")), "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"},
            status_code=201))

    def client_row(cid):
        if not isinstance(cid, str) or len(cid) > 100:
            return None
        r = gw.db.one("SELECT * FROM oauth_clients WHERE id=?", (cid,))
        return dict(r, redirect_uris=json.loads(r["redirect_uris"])) if r else None

    @app.get("/oauth/authorize", include_in_schema=False)
    def authorize(req: Request):
        base = need_base()
        q = req.query_params
        client = client_row(q.get("client_id"))
        if client is None:
            return _error_page("This app isn't registered with this Loft gateway. Start "
                               "again from the app.")
        uri = q.get("redirect_uri") or (client["redirect_uris"][0]
                                        if len(client["redirect_uris"]) == 1 else "")
        if uri not in client["redirect_uris"]:
            return _error_page("The app sent a return address it didn't register.")
        if not _redirect_ok(uri, gw.s.assistant_return_hosts):    # setting narrowed since
            return _error_page("This Loft gateway doesn't let assistants return to "
                               f"{urlparse(uri).hostname}.", 403)
        state = q.get("state")
        if q.get("response_type") != "code":
            return _back(uri, error="unsupported_response_type", state=state, iss=base)
        if (q.get("code_challenge_method") != "S256"
                or not _CHALLENGE.fullmatch(q.get("code_challenge") or "")):
            return _back(uri, error="invalid_request", state=state, iss=base,
                         error_description="PKCE with S256 is required.")
        res = q.get("resource")
        if res and res.rstrip("/") not in (f"{base}/mcp", base):
            return _back(uri, error="invalid_target", state=state, iss=base)
        if state is not None and len(state) > 500:
            return _back(uri, error="invalid_request", iss=base)
        rid = secrets.token_urlsafe(24)
        now = time.time()
        ip = rate_ip(ip_of(req))
        with gw.db.tx() as c:
            c.execute("DELETE FROM oauth_requests WHERE expires_at<?", (now,))
            mine = c.execute("SELECT COUNT(*) FROM oauth_requests WHERE created_ip IS ?",
                             (ip,)).fetchone()[0]
            total = c.execute("SELECT COUNT(*) FROM oauth_requests").fetchone()[0]
            if mine < PAGES_OPEN_PER_IP and total < PAGES_OPEN_MAX:
                c.execute("INSERT INTO oauth_requests (id, client_id, redirect_uri, state,"
                          " code_challenge, created_at, expires_at, created_ip)"
                          " VALUES (?,?,?,?,?,?,?,?)",
                          (rid, client["id"], uri, state, q["code_challenge"], now,
                           now + REQUEST_TTL, ip))
                rid_ok = True
            else:
                rid_ok = False
        if not rid_ok:
            return _error_page("Too many sign-in pages are open. Wait 15 minutes and start "
                               "again from the app.", 429)
        return _approve_page(rid, client, uri)

    @app.post("/oauth/authorize", include_in_schema=False)
    async def approve(req: Request):
        base = need_base()
        f = await _form(req)
        r = gw.db.one("SELECT * FROM oauth_requests WHERE id=? AND expires_at>?",
                      (f.get("request_id", ""), time.time()))
        if r is None:
            return _error_page("This sign-in page has expired. Start again from the app.")
        client = client_row(r["client_id"])
        if client is None:
            return _error_page("This app isn't registered with this Loft gateway any more.")
        if f.get("action") != "allow":
            with gw.db.tx() as c:
                c.execute("DELETE FROM oauth_requests WHERE id=?", (r["id"],))
            return _back(r["redirect_uri"], error="access_denied", state=r["state"], iss=base)
        ip = ip_of(req)
        try:
            user, pw = await run_in_threadpool(gw.redeem_assistant_code, f.get("code", ""), ip,
                                               route)
        except ApiError as e:
            msg = {"invalid_code": "That code isn't right. Check it and try again.",
                   "code_expired": "That code has expired or was used already. Make a new one.",
                   "too_many_attempts": "Too many wrong codes. Wait 15 minutes and try again.",
                   "account_not_allowed": "This gateway doesn't let that account connect "
                                          "assistants."}.get(e.code, f"Refused ({e.code}).")
            return _approve_page(r["id"], client, r["redirect_uri"], msg)
        code_id = secrets.token_hex(8)
        secret = secrets.token_bytes(32)
        now = time.time()
        with gw.db.tx() as c:
            n = c.execute("DELETE FROM oauth_requests WHERE id=?", (r["id"],)).rowcount
            if n:
                c.execute("DELETE FROM oauth_codes WHERE expires_at<?", (now - 86400,))
                c.execute("INSERT INTO oauth_codes (id, code_hash, client_id, redirect_uri,"
                          " code_challenge, nas_user, wrapped_cred, created_at, expires_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?)",
                          (code_id, crypto.token_hash(secret), client["id"], r["redirect_uri"],
                           r["code_challenge"], user,
                           crypto.wrap_credential(gw.master, secret, "oauth:" + code_id, user, pw),
                           now, now + AUTH_CODE_TTL))
                c.execute("UPDATE oauth_clients SET used_at=? WHERE id=?", (now, client["id"]))
        pw = None  # noqa: F841
        if not n:
            return _error_page("This sign-in page was already used. Start again from the app.")
        gw.db.audit("assistant_approved", nas_user=user, ip=ip, route=route,
                    client_id=client["id"], client_name=client["name"])
        code = f"lac_{code_id}.{base64.urlsafe_b64encode(secret).decode().rstrip('=')}"
        return _back(r["redirect_uri"], code=code, state=r["state"], iss=base)

    @app.post("/oauth/token", include_in_schema=False)
    async def token(req: Request):
        need_base()
        f = await _form(req)
        client = client_row(f.get("client_id"))
        if client is None:
            return _token_error("invalid_client", 401)
        ip = ip_of(req)
        gt = f.get("grant_type")
        if gt == "refresh_token":
            try:
                out = await run_in_threadpool(gw.refresh_oauth, f.get("refresh_token", ""),
                                              client["id"], ip, route)
            except ApiError:
                return _token_error("invalid_grant")
            return _no_store(JSONResponse(out))
        if gt != "authorization_code":
            return _token_error("unsupported_grant_type")
        return await run_in_threadpool(_trade_code, gw, client, f, ip, route)


def _trade_code(gw: Gateway, client: dict, f: dict, ip, route) -> Response:
    code = f.get("code", "")
    m = re.fullmatch(r"lac_([0-9a-f]{16})\.([A-Za-z0-9_-]{43})", code)
    if not m:
        return _token_error("invalid_grant")
    secret = base64.urlsafe_b64decode(m[2] + "=")
    row = gw.db.one("SELECT * FROM oauth_codes WHERE id=?", (m[1],))
    if not row or not crypto.hash_matches(secret, row["code_hash"]):
        return _token_error("invalid_grant")
    with gw.db.tx() as c:
        n = c.execute("UPDATE oauth_codes SET used_at=? WHERE id=? AND used_at IS NULL",
                      (time.time(), row["id"])).rowcount
    if not n:
        # A second use: someone else has the code. End what the first use made.
        for d in gw.db.all("SELECT id FROM devices WHERE client_id=? AND nas_user=? AND"
                           " created_at>=? AND revoked_at IS NULL",
                           (row["client_id"], row["nas_user"], row["created_at"])):
            gw.revoke(d["id"])
        gw.db.audit("oauth_code_reused", nas_user=row["nas_user"], ip=ip, route=route,
                    client_id=row["client_id"])
        return _token_error("invalid_grant")
    verifier = f.get("code_verifier", "")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()
                                         ).decode().rstrip("=")
    ok = (row["expires_at"] > time.time() and row["client_id"] == client["id"]
          and f.get("redirect_uri", row["redirect_uri"]) == row["redirect_uri"]
          and _CHALLENGE.fullmatch(verifier) is not None
          and secrets.compare_digest(challenge, row["code_challenge"]))
    try:
        pw = crypto.unwrap_credential(gw.master, secret, "oauth:" + row["id"], row["nas_user"],
                                      row["wrapped_cred"]) if ok else None
    except crypto.CryptoError:
        pw = None
    with gw.db.tx() as c:
        c.execute("UPDATE oauth_codes SET wrapped_cred=x'' WHERE id=?", (row["id"],))
    if pw is None:
        return _token_error("invalid_grant")
    try:
        out = gw.create_oauth_connection(row["nas_user"], pw, client["id"], client["name"], ip,
                                         route)
    except ApiError as e:
        return _token_error("invalid_grant", desc=e.code)
    finally:
        pw = None  # noqa: F841
    return _no_store(JSONResponse(out))
