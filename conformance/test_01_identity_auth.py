"""Identity, pinning, sign-in per listener, refresh."""
import httpx
import pytest

from conftest import err, sign_in
from loftclient import Loft, PinMismatch


def test_healthz_says_direct(direct):
    r = direct.call("healthz", token=False)
    assert r.status_code == 200
    assert r.json()["route"] == "direct" and r.json()["service"] == "loft"


def test_identity_serves_the_pins(direct, cfg):
    pins = direct.call("identity", token=False).json()["direct_spki_sha256"]
    assert pins, "no pins served"
    # The key we are talking to is one of them (the transport checked it against cfg.pins).
    assert set(cfg.pins) & set(pins), "the pins we trust are not the ones the gateway serves"


def test_wrong_pin_never_sends_a_request(cfg, spec):
    bogus = Loft(cfg.direct, spec, ["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="])
    with pytest.raises((PinMismatch, httpx.ConnectError)):
        bogus.call("login", json_body={"user": cfg.user, "password": cfg.password},
                   token=False)
    bogus.close()


def test_login_on_direct(direct, cfg):
    r = direct.call("login", json_body={"user": cfg.user, "password": cfg.password,
                                        "device_name": "conformance login"}, token=False)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("nst_") and body["device_id"] in body["token"]
    c = direct.with_token(body["token"])
    me = c.call("get_me").json()
    assert me["device_id"] == body["device_id"] and me["route"] == "direct"
    assert me["device_name"] == "conformance login"
    c.call("revoke_device", path={"device_id": body["device_id"]})


def test_wrong_password_is_refused(direct, cfg):
    r = direct.call("login", json_body={"user": cfg.user, "password": cfg.password + "-wrong",
                                        "device_name": "conformance"}, token=False)
    # 429 once the per-account limit is hit (re-runs within the window); both conform.
    assert (r.status_code, err(r)) in ((401, "invalid_credentials"), (429, "too_many_attempts"))
    if r.status_code == 429:
        assert int(r.headers["Retry-After"]) > 0


def test_missing_and_bad_tokens(direct):
    r = direct.call("get_me", token=False)
    assert (r.status_code, err(r)) == (401, "missing_token")
    r = direct.call("get_me", token="nst_nope.nope")
    assert (r.status_code, err(r)) == (401, "invalid_token")


def test_edge_header_refused_on_direct(direct, cfg):
    r = direct.call("login", json_body={"user": cfg.user, "password": cfg.password},
                    headers={"CF-Connecting-IP": "203.0.113.9"}, token=False)
    assert r.status_code == 403
    assert err(r) in ("password_login_direct_only", "edge_traffic_on_direct_listener")


def test_public_route(public, cfg, direct):
    r = public.call("healthz", token=False)
    assert r.status_code == 200
    expect = "direct" if cfg.public_mode == "direct" else "public"
    assert r.json()["route"] == expect
    pins = public.call("identity", token=False).json()["direct_spki_sha256"]
    assert set(cfg.pins) & set(pins)
    # A device token works on both routes.
    c = sign_in(direct, cfg, "conformance public")
    assert c.call("get_me").status_code == 200
    me = public.with_token(c.token).call("get_me").json()
    assert me["route"] == expect
    c.call("revoke_device", path={"device_id": me["device_id"]})


def test_login_on_public_route(public, cfg):
    r = public.call("login", json_body={"user": cfg.user, "password": cfg.password,
                                        "device_name": "conformance public login"},
                    token=False)
    if cfg.public_mode == "tunnel":
        # The proxy can read the traffic: never a password there.
        assert (r.status_code, err(r)) == (403, "password_login_direct_only")
    else:
        # direct mode: the public URL is the pinned direct listener, sign-in works.
        assert r.status_code == 200, r.text
        public.with_token(r.json()["token"]).call(
            "revoke_device", path={"device_id": r.json()["device_id"]})


def test_refresh_rotates_the_token(direct, cfg):
    c = sign_in(direct, cfg, "conformance refresh")
    old = c.token
    r = c.call("refresh")
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["token"] != old and new["device_id"] in new["token"]
    c2 = direct.with_token(new["token"])
    assert c2.call("get_me").status_code == 200
    # The old token still works for a grace period but can't rotate again.
    assert c.call("get_me").status_code == 200
    r = c.call("refresh")
    assert (r.status_code, err(r)) == (401, "token_superseded")
    c2.call("revoke_device", path={"device_id": new["device_id"]})
