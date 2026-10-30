from conftest import login, rooted

from loft_gateway import crypto


def test_healthz_says_which_route(env):
    assert env.direct.get("/healthz").json()["route"] == "direct"
    assert env.public.get("/healthz").json()["route"] == "public"


def test_password_login_refused_on_public(env):
    r = env.public.post("/v1/auth/login", json={"user": "alice", "password": "pw-alice"})
    assert r.status_code == 403 and r.json()["error"] == "password_login_direct_only"


def test_edge_header_on_direct_is_refused(env):
    r = env.direct.post("/v1/auth/login", json={"user": "alice", "password": "pw-alice"},
                        headers={"CF-Connecting-IP": "1.2.3.4"})
    assert r.status_code == 403


def test_token_works_on_public(env):
    h = login(env)
    assert env.public.get("/v1/me", headers=h).json()["nas_user"] == "alice"


def test_bad_password_and_lockout(env):
    for _ in range(5):
        r = env.direct.post("/v1/auth/login", json={"user": "alice", "password": "nope"})
        assert r.status_code == 401
    r = env.direct.post("/v1/auth/login", json={"user": "alice", "password": "pw-alice"})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_personal_account_refused_before_nas(env):
    """Only dedicated Loft accounts may sign in; others never reach the NAS."""
    calls = []
    real = env.gw.backend.authenticate
    env.gw.backend.authenticate = lambda u, p: calls.append(u) or real(u, p)
    r = env.direct.post("/v1/auth/login", json={"user": "dave", "password": "x"})
    assert r.status_code == 403 and r.json()["error"] == "account_not_allowed"
    assert calls == []
    assert env.direct.post("/v1/auth/login",
                           json={"user": "ALICE", "password": "pw-alice"}).status_code != 403


def test_default_account_pattern():
    import re
    from loft_gateway.config import Settings
    s = Settings()
    pat = s.allowed_users_re
    for ok in ("loft-alice", "alice", "bob.smith", "admin"):
        assert re.fullmatch(pat, ok, re.I) and not re.fullmatch(s.denied_users_re, ok, re.I)
    for bad in ("", "a b", "dom\\user", "u@x", "../x"):
        assert not re.fullmatch(pat, bad, re.I)
    assert re.fullmatch(s.denied_users_re, "Guest", re.I)
    # Neutral defaults (Loft 0.2): nothing blocked, nobody may use sign-in codes,
    # SMB encryption required. A site sets its own in gateway.toml.
    assert s.blocked_shares == [] and s.blocked_paths == [] and s.code_users_re == ""
    assert s.smb_server == "" and s.smb_encryption == "required" and s.public_mode == "none"



def test_db_never_holds_password_or_token(env):
    h = login(env)
    tok = h["Authorization"][7:]
    raw = b"".join(bytes(v) if isinstance(v, (bytes, memoryview)) else str(v).encode()
                   for row in env.gw.db.all("SELECT * FROM devices") for v in row if v is not None)
    assert b"pw-alice" not in raw
    assert tok.encode() not in raw
    _, secret = crypto.parse_token(tok)
    assert secret not in raw


def test_credential_needs_token_and_master_key(env):
    h = login(env)
    did, secret = crypto.parse_token(h["Authorization"][7:])
    blob = env.gw.db.one("SELECT wrapped_cred FROM devices WHERE id=?", (did,))[0]
    assert crypto.unwrap_credential(env.gw.master, secret, did, "alice", blob) == "pw-alice"
    import os
    import pytest
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_credential(os.urandom(32), secret, did, "alice", blob)
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_credential(env.gw.master, os.urandom(32), did, "alice", blob)


def test_session_rebuilt_from_wrapped_credential(env):
    h = login(env)
    env.gw._sessions.clear()                      # gateway restart: no cached sessions
    assert env.public.get("/v1/me", headers=h).status_code == 200


def test_refresh_rotates_with_grace(env):
    h = login(env)
    r = env.public.post("/v1/auth/refresh", headers=h)
    assert r.status_code == 200
    h2 = {"Authorization": f"Bearer {r.json()['token']}"}
    env.gw._sessions.clear()
    assert env.public.get("/v1/me", headers=h2).status_code == 200
    env.gw._sessions.clear()
    assert env.public.get("/v1/me", headers=h).status_code == 200      # grace
    assert env.public.post("/v1/auth/refresh", headers=h).status_code == 401  # old can't rotate
    env.gw.db.conn.execute("UPDATE devices SET prev_valid_until=0")
    assert env.public.get("/v1/me", headers=h).status_code == 401


def test_revoke_wipes_credential(env):
    h = login(env)
    did = env.public.get("/v1/me", headers=h).json()["device_id"]
    assert env.public.delete(f"/v1/auth/device/{did}", headers=h).status_code == 200
    r = env.public.get("/v1/me", headers=h)
    assert r.status_code == 401 and r.json()["error"] == "device_revoked"
    assert env.gw.db.one("SELECT length(wrapped_cred) n FROM devices WHERE id=?", (did,))["n"] == 0


def test_expired_token_says_so(env):
    h = login(env)
    env.gw._sessions.clear()
    env.gw.db.conn.execute("UPDATE devices SET expires_at=1")
    r = env.public.get("/v1/me", headers=h)
    assert r.status_code == 401 and r.json()["error"] == "token_expired"
    r = env.public.post("/v1/auth/refresh", headers=h)
    assert r.status_code == 401 and r.json()["error"] == "token_expired"


def test_wrong_secret_never_reveals_device_state(env):
    """A token with a real device id but the wrong secret is plain invalid_token,
    even when that device is revoked or expired."""
    h = login(env)
    did = env.public.get("/v1/me", headers=h).json()["device_id"]
    forged, _ = crypto.new_token(did)
    hf = {"Authorization": f"Bearer {forged}"}
    for sql in ("UPDATE devices SET expires_at=1", "UPDATE devices SET revoked_at=1"):
        env.gw.db.conn.execute(sql)
        r = env.public.get("/v1/me", headers=hf)
        assert r.status_code == 401 and r.json()["error"] == "invalid_token"


def test_cannot_revoke_other_users_device(env):
    ha = login(env, "alice")
    hb = login(env, "bob")
    did_b = env.public.get("/v1/me", headers=hb).json()["device_id"]
    assert env.public.delete(f"/v1/auth/device/{did_b}", headers=ha).status_code == 404


def test_nas_password_change_invalidates_device(env):
    h = login(env)
    env.gw._sessions.clear()
    env.gw.backend.users["alice"]["password"] = "changed"
    r = env.public.get("/v1/me", headers=h)
    assert r.status_code == 401 and r.json()["error"] == "nas_credentials_invalid"


def test_browse_only_accessible_shares(env):
    h = login(env)
    assert env.public.get("/v1/roots", headers=h).json()["shares"] == ["home"]
    r = env.public.get("/v1/roots", params={"share": "home"}, headers=h).json()
    assert r["folders"] == ["alice", "bob"]
    assert env.public.get("/v1/roots", params={"share": "secret"}, headers=h).status_code == 404


def test_items_scoped_to_device_root(env):
    ha, ra = rooted(env, "alice", "alice")
    hb, rb = rooted(env, "bob", "bob")
    assert env.public.get(f"/v1/items/{ra['root_item_id']}", headers=hb).status_code == 404
    assert env.public.get(f"/v1/items/{rb['root_item_id']}", headers=hb).status_code == 200
