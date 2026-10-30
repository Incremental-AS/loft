"""Sign-in codes (e.g. for an app store reviewer): minted on the direct
route by a signed-in device of an allowed account, redeemed on either route."""
import time

from conftest import login


def allow(env, users="alice"):
    env.gw.s.code_users_re = users


def mint(env, h, **body):
    return env.direct.post("/v1/auth/codes", json=body, headers=h)


def test_off_by_default(env):
    h = login(env)
    r = mint(env, h)
    assert r.status_code == 403 and r.json()["error"] == "account_not_allowed"


def test_mint_and_redeem_over_public(env):
    allow(env)
    r = mint(env, login(env))
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert len(code) == 29 and code.count("-") == 5
    r = env.public.post("/v1/auth/code", json={"code": code.lower(), "device_name": "Review iPhone"})
    assert r.status_code == 200, r.text
    h2 = {"Authorization": f"Bearer {r.json()['token']}"}
    assert r.json()["nas_user"] == "alice"
    assert env.public.get("/v1/roots", headers=h2).status_code == 200
    # the new device is an ordinary device: refresh works, listed, revocable
    assert env.public.post("/v1/auth/refresh", headers=h2).status_code == 200


def test_mint_only_on_direct(env):
    allow(env)
    h = login(env)
    r = env.public.post("/v1/auth/codes", json={}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "codes_direct_only"
    r = env.direct.post("/v1/auth/codes", json={}, headers={**h, "cf-connecting-ip": "1.2.3.4"})
    assert r.status_code == 403


def test_other_account_cannot_mint(env):
    allow(env, "alice")
    r = mint(env, login(env, "bob"))
    assert r.status_code == 403


def test_denied_account_never(env):
    allow(env, ".*")
    h = login(env, "bob")
    env.gw.s.denied_users_re = "bob"          # e.g. a service account
    r = mint(env, h)
    assert r.status_code == 403 and r.json()["error"] == "account_not_allowed"


def test_uses_are_capped(env):
    allow(env)
    code = mint(env, login(env), max_uses=2).json()["code"]
    for _ in range(2):
        assert env.public.post("/v1/auth/code", json={"code": code}).status_code == 200
    r = env.public.post("/v1/auth/code", json={"code": code})
    assert r.status_code == 401 and r.json()["error"] == "code_expired"


def test_client_cannot_exceed_config(env):
    allow(env)
    env.gw.s.code_max_uses, env.gw.s.code_ttl_days = 3, 5
    j = mint(env, login(env), max_uses=1000, days=1000).json()
    assert j["max_uses"] == 3 and j["expires_at"] < time.time() + 5 * 86400 + 60


def test_expired_and_revoked(env):
    allow(env)
    h = login(env)
    j = mint(env, h).json()
    env.gw.revoke_code(j["code_id"])
    r = env.public.post("/v1/auth/code", json={"code": j["code"]})
    assert r.status_code == 401 and r.json()["error"] == "code_expired"
    row = env.gw.db.one("SELECT wrapped_cred FROM signin_codes WHERE id=?", (j["code_id"],))
    assert row["wrapped_cred"] == b""
    j = mint(env, h).json()
    env.gw.db.conn.execute("UPDATE signin_codes SET expires_at=? WHERE id=?", (time.time() - 1, j["code_id"]))
    assert env.public.post("/v1/auth/code", json={"code": j["code"]}).status_code == 401


def test_wrong_secret_and_rate_limit(env):
    allow(env)
    j = mint(env, login(env)).json()
    bad = j["code"][:-1] + ("A" if j["code"][-1] != "A" else "B")
    for _ in range(5):
        r = env.public.post("/v1/auth/code", json={"code": bad})
        assert r.status_code == 401 and r.json()["error"] == "invalid_code"
    # five misses on this code id: even the right code waits out the window
    r = env.public.post("/v1/auth/code", json={"code": j["code"]})
    assert r.status_code == 429
    assert env.public.post("/v1/auth/code", json={"code": "nonsense"}).status_code == 401


def test_config_narrowed_after_minting(env):
    allow(env)
    code = mint(env, login(env)).json()["code"]
    allow(env, "")
    r = env.public.post("/v1/auth/code", json={"code": code})
    assert r.status_code == 403


def test_password_change_kills_code(env):
    allow(env)
    code = mint(env, login(env)).json()["code"]
    env.gw.backend.users["alice"]["password"] = "changed"
    r = env.public.post("/v1/auth/code", json={"code": code})
    assert r.status_code == 401 and r.json()["error"] == "nas_credentials_invalid"


def test_db_never_holds_the_code(env):
    allow(env)
    j = mint(env, login(env)).json()
    raw = j["code"].replace("-", "")
    dump = "\n".join(env.gw.db.conn.iterdump())
    assert raw[8:] not in dump and j["code"] not in dump
