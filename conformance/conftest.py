"""Loft gateway conformance suite: configuration and shared fixtures.

Point it at a running gateway with environment variables (see README.md):

    LOFT_PAIRING      pairing link or payload from `loft-gateway pair` (gives LAN URL + pins)
    LOFT_DIRECT_URL   https://host:port of the direct listener   } instead of LOFT_PAIRING
    LOFT_PINS         comma-separated SPKI pins (base64)           }
    LOFT_PUBLIC_URL   the public route (optional; tests that need it are skipped without it)
    LOFT_PUBLIC_MODE  "tunnel" (default) or "direct"
    LOFT_USER, LOFT_PASSWORD   an SMB account the gateway may sign in
    LOFT_SHARE        share to work in (default: the first one the account sees)
    LOFT_ROOT_PATH    folder inside the share used as the root (default: the share itself)

Every test works inside its own fresh folder `loft-conformance-<random>` under the root
and the session removes it (to the root's trash) at the end.
"""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from loftclient import Loft, Spec, decode_pairing


class Cfg:
    def __init__(self):
        e = os.environ
        pair = e.get("LOFT_PAIRING")
        p = decode_pairing(pair) if pair else {}
        self.direct = e.get("LOFT_DIRECT_URL") or p.get("lan")
        self.pins = [x.strip() for x in e.get("LOFT_PINS", "").split(",") if x.strip()] or p.get(
            "pins", [])
        self.public = e.get("LOFT_PUBLIC_URL") or p.get("pub")
        self.public_mode = e.get("LOFT_PUBLIC_MODE") or p.get("mode") or "tunnel"
        self.user = e.get("LOFT_USER")
        self.password = e.get("LOFT_PASSWORD")
        self.share = e.get("LOFT_SHARE")
        self.root_path = e.get("LOFT_ROOT_PATH", "").strip("/")
        missing = [n for n, v in (("LOFT_DIRECT_URL or LOFT_PAIRING", self.direct),
                                  ("LOFT_PINS or LOFT_PAIRING", self.pins),
                                  ("LOFT_USER", self.user), ("LOFT_PASSWORD", self.password))
                   if not v]
        if missing:
            pytest.exit("conformance: set " + ", ".join(missing), returncode=4)
        if not self.direct.startswith("https://"):
            pytest.exit("conformance: the direct URL must be https://", returncode=4)
        if self.public_mode not in ("tunnel", "direct"):
            pytest.exit("conformance: LOFT_PUBLIC_MODE is tunnel or direct", returncode=4)


@pytest.fixture(scope="session")
def cfg() -> Cfg:
    return Cfg()


@pytest.fixture(scope="session")
def spec() -> Spec:
    return Spec()


@pytest.fixture(scope="session")
def direct(cfg, spec):
    c = Loft(cfg.direct, spec, cfg.pins)
    yield c
    c.close()


@pytest.fixture(scope="session")
def public(cfg, spec):
    if not cfg.public:
        pytest.skip("LOFT_PUBLIC_URL not set")
    # tunnel: web PKI (or plain http on loopback in CI). direct: the same pins.
    c = Loft(cfg.public, spec, cfg.pins if cfg.public_mode == "direct" else None)
    yield c
    c.close()


def sign_in(direct: Loft, cfg: Cfg, name: str) -> Loft:
    r = direct.call("login", json_body={"user": cfg.user, "password": cfg.password,
                                         "device_name": name}, token=False)
    assert r.status_code == 200, r.text
    return direct.with_token(r.json()["token"])


@pytest.fixture(scope="session")
def share(direct, cfg) -> str:
    c = sign_in(direct, cfg, "conformance probe")
    shares = c.call("browse_roots").json()["shares"]
    c.call("revoke_device", path={"device_id": c.call("get_me").json()["device_id"]})
    if cfg.share:
        assert cfg.share in shares, f"{cfg.share!r} not offered; the account sees {shares}"
        return cfg.share
    assert shares, "the account sees no shares"
    return shares[0]


@pytest.fixture(scope="session")
def session_dev(direct, cfg, share):
    """The suite's main device, rooted at LOFT_SHARE/LOFT_ROOT_PATH, plus a scratch folder
    for the whole session."""
    c = sign_in(direct, cfg, "conformance")
    r = c.call("set_root", json_body={"share": share, "path": cfg.root_path})
    assert r.status_code == 200, r.text
    root = r.json()["root"]
    r = c.call("create_folder", json_body={"parent_id": root["root_item_id"],
                                           "name": f"loft-conformance-{uuid.uuid4().hex[:10]}"})
    assert r.status_code == 200, r.text
    scratch = r.json()
    yield type("Dev", (), dict(c=c, root=root, scratch=scratch))
    c.call("delete_item", path={"item_id": scratch["id"]})
    c.call("revoke_device", path={"device_id": c.call("get_me").json()["device_id"]})


@pytest.fixture
def dev(session_dev):
    return session_dev.c


@pytest.fixture
def folder(session_dev):
    """A fresh, empty folder for one test."""
    c = session_dev.c
    r = c.call("create_folder", json_body={"parent_id": session_dev.scratch["id"],
                                           "name": f"t-{uuid.uuid4().hex[:8]}"})
    assert r.status_code == 200, r.text
    return r.json()


# ── helpers ────────────────────────────────────────────────────────────────

def put_file(c: Loft, parent_id: str | None, name: str | None, data: bytes, *,
             chunk: int | None = None, item_id: str | None = None, if_match: str | None = None,
             replaces: str | None = None, near_id: str | None = None) -> dict:
    """create → PUT chunks → commit. Returns the commit answer."""
    sha = hashlib.sha256(data).hexdigest()
    r = c.call("upload_create", json_body={"size": len(data), "sha256": sha,
                                           "near_id": near_id or parent_id or item_id})
    assert r.status_code == 200, r.text
    u = r.json()
    step = chunk or u.get("chunk_max") or len(data) or 1
    off = 0
    while off < len(data):
        part = data[off:off + step]
        r = c.call("upload_chunk", path={"upload_id": u["upload_id"]}, params={"offset": off},
                   content=part, headers={"X-Chunk-SHA256": hashlib.sha256(part).hexdigest()})
        assert r.status_code == 200, r.text
        off += len(part)
    body = {"parent_id": parent_id, "name": name, "item_id": item_id, "if_match": if_match,
            "replaces": replaces}
    r = c.call("upload_commit", path={"upload_id": u["upload_id"]},
               json_body={k: v for k, v in body.items() if v is not None})
    assert r.status_code == 200, r.text
    return r.json()


def children(c: Loft, parent_id: str) -> dict[str, dict]:
    out, cursor = {}, None
    while True:
        r = c.call("list_children", path={"item_id": parent_id},
                   params={"cursor": cursor} if cursor else None)
        assert r.status_code == 200, r.text
        page = r.json()
        out.update({it["name"]: it for it in page["items"]})
        cursor = page["next_cursor"]
        if not cursor:
            return out


def err(r) -> str:
    return r.json().get("error")
