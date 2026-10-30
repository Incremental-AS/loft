import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from loft_gateway.api import create_app
from loft_gateway.config import Settings
from loft_gateway.db import DB
from loft_gateway.service import Gateway
from loft_gateway.storage.local import LocalBackend


@pytest.fixture
def env(tmp_path):
    nas = tmp_path / "nas"
    (nas / "home" / "alice" / "Docs").mkdir(parents=True)
    (nas / "home" / "alice" / "Docs" / "a.txt").write_bytes(b"hello")
    (nas / "home" / "bob").mkdir(parents=True)
    (nas / "secret").mkdir()
    (nas / "media" / "Film").mkdir(parents=True)
    (nas / "media" / "Film" / "clip.mov").write_bytes(b"0123456789")
    (nas / "Family" / "notes").mkdir(parents=True)
    (nas / "Family" / "Passwords" / "vault").mkdir(parents=True)
    (nas / "Family" / "Passwords" / "vault" / "master.kdbx").write_bytes(b"KEY")
    (nas / "Family" / "Admin" / "router-backups").mkdir(parents=True)
    (nas / "Family" / "Admin" / "router-backups" / "router.tar").write_bytes(b"CFG")
    (nas / "Family" / "Admin" / "Wifi-Settings").mkdir()       # case differs on purpose
    (nas / "Family" / "Admin" / "medical-records").mkdir()
    (nas / "Family" / "Admin" / "papers" / "Private").mkdir(parents=True)  # case differs on purpose
    (nas / "Family" / "Admin" / "papers" / "Private" / "accounts.md").write_bytes(b"KEYS")
    (nas / "Family" / "Admin" / "papers" / "moving.md").write_bytes(b"MAP")
    (nas / "Family" / "Admin" / "receipts").mkdir()
    (nas / "backup" / "dumps").mkdir(parents=True)
    backend = LocalBackend(nas, {
        "alice": {"password": "pw-alice", "shares": ["home"]},
        "bob": {"password": "pw-bob", "shares": ["home"]},
        # an admin's personal account: opens everything, backup included
        "carol": {"password": "pw-carol", "shares": ["home", "media", "Family", "backup"]},
    }, short_names={"PWD7QK~1": "Passwords", "NOTESO~1": "notes"})   # Samba's 8.3 aliases
    s = Settings(db_path=str(tmp_path / "index.db"), shares=["home", "secret"],
                 direct_spki_pins=["pin1", "pin2"], background_rescan=False,
                 allowed_users_re="alice|bob|carol", thumb_socket="",
                 # A site's blocks, set explicitly: the defaults block nothing (Loft 0.2).
                 blocked_shares=["git", "backup", "timemachine", "vm-images", "mail-archive",
                                 "scratch", "drive-backup-only"],
                 blocked_paths=["Family/Passwords", "Family/Admin/router-backups",
                                "Family/Admin/wifi-settings", "Family/Admin/medical-records",
                                "Family/Admin/papers/private"])
    gw = Gateway(s, DB(s.db_path), backend, os.urandom(32))
    direct = TestClient(create_app(gw, "direct"))
    public = TestClient(create_app(gw, "public"))
    return type("Env", (), dict(gw=gw, nas=nas, direct=direct, public=public, tmp=tmp_path))


def login(env, user="alice", pw=None, name="Test Mac"):
    r = env.direct.post("/v1/auth/login", json={"user": user, "password": pw or f"pw-{user}",
                                                 "device_name": name})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def rooted(env, user="alice", path="alice", name="Test Mac"):
    h = login(env, user, name=name)
    r = env.public.put("/v1/me/root", json={"share": "home", "path": path}, headers=h)
    assert r.status_code == 200, r.text
    return h, r.json()["root"]


def all_shares(env, user="carol", name="Test Mac"):
    """Signed in with an all-shares root, shares discovered from the NAS."""
    env.gw.s.shares = []
    h = login(env, user, name=name)
    r = env.public.put("/v1/me/root", json={"share": "*"}, headers=h)
    assert r.status_code == 200, r.text
    return h, r.json()["root"]


def upload(client, h, data: bytes, chunk=4, near=None):
    sha = hashlib.sha256(data).hexdigest()
    u = client.post("/v1/uploads", json={"size": len(data), "sha256": sha, "near_id": near},
                    headers=h).json()
    off = 0
    while off < len(data) or (off == 0 and not data):
        part = data[off:off + chunk]
        r = client.put(f"/v1/uploads/{u['upload_id']}", params={"offset": off}, content=part,
                       headers={**h, "X-Chunk-SHA256": hashlib.sha256(part).hexdigest()})
        assert r.status_code == 200, r.text
        off += len(part)
        if not data:
            break
    return u["upload_id"]


def child(client, h, parent_id, name):
    for it in client.get(f"/v1/items/{parent_id}/children", headers=h).json()["items"]:
        if it["name"] == name:
            return it
    return None
