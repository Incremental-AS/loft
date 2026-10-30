"""public_mode = "direct" (docs/design.md §A): the outside address is a port forward to the
direct listener. Sign-in works there because the connection is pinned end to end, and
the login limits are then the only brute-force wall in front of the NAS. The public
(tunnel) listener never takes a password, whatever the mode."""
import asyncio
import base64
import hashlib
import os
import socket
import ssl
import threading
import time

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from loft_gateway import initcmd
from loft_gateway.api import create_app
from loft_gateway.config import Listener, Settings
from loft_gateway.db import DB
from loft_gateway.main import serve
from loft_gateway.service import Gateway
from loft_gateway.storage.local import LocalBackend

USERS = {f"u{i}": {"password": f"pw-u{i}", "shares": ["home"]} for i in range(25)}


def _gw(tmp_path, **kw):
    (tmp_path / "nas" / "home").mkdir(parents=True, exist_ok=True)
    s = Settings(db_path=str(tmp_path / "index.db"), shares=["home"], background_rescan=False,
                 thumb_socket="", public_mode="direct", public_url="https://files.example.org:8443",
                 lan_url="https://10.0.0.5:8443", **kw)
    return Gateway(s, DB(s.db_path), LocalBackend(tmp_path / "nas", USERS), os.urandom(32))


def _login(c, user, pw):
    return c.post("/v1/auth/login", json={"user": user, "password": pw, "device_name": "t"})


def test_signin_only_on_the_direct_listener(tmp_path):
    gw = _gw(tmp_path)
    direct, public = TestClient(create_app(gw, "direct")), TestClient(create_app(gw, "public"))
    assert _login(direct, "u1", "pw-u1").status_code == 200
    r = _login(public, "u1", "pw-u1")
    assert r.status_code == 403 and r.json()["error"] == "password_login_direct_only"
    # A proxy's header on the direct listener: refused, so no one can pose as the tunnel
    # or rotate the address the limits count.
    r = direct.post("/v1/auth/login", json={"user": "u1", "password": "pw-u1"},
                    headers={"CF-Connecting-IP": "198.51.100.7"})
    assert r.status_code == 403


def test_per_account_limit_holds_across_addresses(tmp_path):
    gw = _gw(tmp_path)
    app = create_app(gw, "direct")
    for i in range(5):
        c = TestClient(app, client=(f"203.0.113.{i}", 40000))
        assert _login(c, "u1", "wrong").status_code == 401
    # Right password, from yet another address: the account is held for the window.
    r = _login(TestClient(app, client=("203.0.113.99", 40000)), "u1", "pw-u1")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) == gw.s.login_window_s
    # Other accounts are not affected.
    assert _login(TestClient(app, client=("203.0.113.99", 40000)), "u2", "pw-u2").status_code == 200


def test_per_address_limit_across_accounts(tmp_path):
    gw = _gw(tmp_path)
    app = create_app(gw, "direct")
    attacker = TestClient(app, client=("198.51.100.66", 51000))
    for i in range(gw.s.login_max_fail_ip):          # spread so no account hits its own cap
        assert _login(attacker, f"u{i}", "wrong").status_code == 401
    r = _login(attacker, "u24", "pw-u24")
    assert r.status_code == 429 and r.json()["error"] == "too_many_attempts"
    # Someone else, from their own address, still signs in.
    assert _login(TestClient(app, client=("198.51.100.1", 51000)), "u24", "pw-u24").status_code == 200


def test_limits_expire_with_the_window(tmp_path):
    gw = _gw(tmp_path, login_window_s=1)
    c = TestClient(create_app(gw, "direct"))
    for _ in range(5):
        _login(c, "u3", "wrong")
    assert _login(c, "u3", "pw-u3").status_code == 429
    time.sleep(1.2)
    assert _login(c, "u3", "pw-u3").status_code == 200


def test_failures_are_audited_with_address_and_route(tmp_path):
    gw = _gw(tmp_path)
    _login(TestClient(create_app(gw, "direct"), client=("203.0.113.5", 1)), "u4", "wrong")
    row = gw.db.one("SELECT * FROM audit WHERE event='login_failed'")
    assert (row["nas_user"], row["ip"], row["route"]) == ("u4", "203.0.113.5", "direct")


# ── the real listeners: TLS with the pinned key, as a port forward would reach them ──

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def live(tmp_path):
    key, crt = tmp_path / "direct.key", tmp_path / "direct.crt"
    pin = initcmd.make_tls_key(key, crt, ["127.0.0.1", "files.example.org"])
    dp, pp = _free_port(), _free_port()
    gw = _gw(tmp_path, direct_spki_pins=[pin],
             direct=Listener("127.0.0.1", dp, str(crt), str(key)),
             public=Listener("127.0.0.1", pp))
    threading.Thread(target=lambda: asyncio.run(serve(gw)), daemon=True).start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{pp}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield type("Live", (), dict(gw=gw, dp=dp, pp=pp, pin=pin))
    gw.release_waiters()


def _server_pin(port: int) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw, \
            ctx.wrap_socket(raw, server_hostname="files.example.org") as tls:
        cert = x509.load_der_x509_certificate(tls.getpeercert(True))
    spki = cert.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def test_live_direct_listener_is_pinned_tls_and_takes_signin(live):
    # The key a device meets on the forwarded port is the pinned one ...
    assert _server_pin(live.dp) == live.pin
    # ... which /v1/identity also serves, and the pairing code carries.
    from loft_gateway import admin
    assert admin.pairing_payload(live.gw.s)["pins"][0] == live.pin
    assert admin.pairing_payload(live.gw.s)["mode"] == "direct"
    with httpx.Client(verify=False, base_url=f"https://127.0.0.1:{live.dp}") as c:
        assert c.get("/v1/identity").json()["direct_spki_sha256"] == [live.pin]
        r = c.post("/v1/auth/login", json={"user": "u5", "password": "pw-u5"})
        assert r.status_code == 200 and r.json()["token"].startswith("nst_")
    # Plain HTTP on the direct port never works: no password without TLS.
    with pytest.raises(httpx.HTTPError):
        httpx.post(f"http://127.0.0.1:{live.dp}/v1/auth/login",
                   json={"user": "u5", "password": "pw-u5"}, timeout=3)
    # The public listener (a tunnel's target) refuses the password in every mode.
    r = httpx.post(f"http://127.0.0.1:{live.pp}/v1/auth/login",
                   json={"user": "u5", "password": "pw-u5"}, timeout=3)
    assert r.status_code == 403 and r.json()["error"] == "password_login_direct_only"
