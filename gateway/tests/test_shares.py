"""All-shares roots: any NAS account, every share it can open as a top-level
folder, blocked shares never reachable."""
import hashlib

import pytest

from loft_gateway.rootfs import ALL_SHARES, Blocked, CrossShare, FixedItem, RootFS

from conftest import all_shares, child, upload


def kids(env, h, iid):
    return env.public.get(f"/v1/items/{iid}/children", headers=h).json()["items"]


def test_shares_are_top_level_and_blocked_ones_hidden(env):
    h, root = all_shares(env)
    assert root["share"] == "*"
    top = kids(env, h, root["root_item_id"])
    names = sorted(i["name"] for i in top)
    assert names == ["Family", "home", "media"]              # backup blocked
    assert all(i["fixed"] and i["type"] == "dir" for i in top)
    assert env.public.get(f"/v1/items/{root['root_item_id']}", headers=h).json()["fixed"]
    assert env.public.get("/v1/roots", headers=h).json()["shares"] == ["Family", "home", "media"]
    film = child(env.public, h, child(env.public, h, root["root_item_id"], "media")["id"], "Film")
    assert not film["fixed"]
    clip = child(env.public, h, film["id"], "clip.mov")
    r = env.public.get(f"/v1/items/{clip['id']}/content", headers={**h, "Range": "bytes=2-4"})
    assert r.status_code == 206 and r.content == b"234"


def test_blocked_share_refused_as_root_and_in_paths(env):
    h, root = all_shares(env)
    r = env.public.put("/v1/me/root", json={"share": "backup"}, headers=h)
    assert r.status_code == 404
    fs = RootFS(dict(root, share=ALL_SHARES, path=""), None, ["backup"], [])
    for p in ("backup/dumps", "Backup/x", "BACKUP"):
        with pytest.raises(Blocked):
            fs.nas(p)


def test_shares_themselves_are_fixed(env):
    h, root = all_shares(env)
    rid = root["root_item_id"]
    media = child(env.public, h, rid, "media")
    assert env.public.post("/v1/folders", json={"parent_id": rid, "name": "new"},
                           headers=h).json()["error"] == "share_is_fixed"
    assert env.public.patch(f"/v1/items/{media['id']}", json={"name": "m2"},
                            headers=h).json()["error"] == "share_is_fixed"
    assert env.public.delete(f"/v1/items/{media['id']}", headers=h).json()["error"] == "share_is_fixed"
    uid = upload(env.public, h, b"x", near=media["id"])
    r = env.public.post(f"/v1/uploads/{uid}/commit", json={"parent_id": rid, "name": "x.txt"},
                        headers=h)
    assert r.status_code == 403
    assert (env.nas / "media").is_dir() and not (env.nas / "m2").exists()


def test_writes_land_in_the_right_share_with_their_own_meta_area(env):
    h, root = all_shares(env)
    media = child(env.public, h, root["root_item_id"], "media")
    film = child(env.public, h, media["id"], "Film")
    data = b"new film"
    r = env.public.post("/v1/uploads", json={"size": 1, "sha256": "0" * 64}, headers=h)
    assert r.json()["error"] == "near_id_required"
    uid = upload(env.public, h, data, near=film["id"])
    r = env.public.post(f"/v1/uploads/{uid}/commit", json={"parent_id": film["id"], "name": "n.mov"},
                        headers=h)
    assert r.status_code == 200, r.text
    assert (env.nas / "media" / "Film" / "n.mov").read_bytes() == data
    it = r.json()["item"]
    uid = upload(env.public, h, b"v2", near=it["id"])
    r = env.public.post(f"/v1/uploads/{uid}/commit", json={"item_id": it["id"], "if_match": "c1"},
                        headers=h)
    assert r.json()["result"] == "modified"
    assert len(list((env.nas / "media" / ".loft" / "versions").iterdir())) == 1
    assert env.public.delete(f"/v1/items/{it['id']}", headers=h).status_code == 200
    assert len(list((env.nas / "media" / ".loft" / "trash").iterdir())) == 1
    assert not (env.nas / ".loft").exists()


def test_cross_share_move_copies_and_resumed_upload_lands(env):
    h, root = all_shares(env)
    rid = root["root_item_id"]
    home, media = child(env.public, h, rid, "home"), child(env.public, h, rid, "media")
    clip = child(env.public, h, child(env.public, h, media["id"], "Film")["id"], "clip.mov")
    mtime = (env.nas / "media" / "Film" / "clip.mov").stat().st_mtime_ns
    r = env.public.patch(f"/v1/items/{clip['id']}", json={"parent_id": home["id"]}, headers=h)
    assert r.status_code == 200, r.text
    moved = r.json()
    assert moved["id"] == clip["id"] and moved["content_etag"] == clip["content_etag"]
    assert not (env.nas / "media" / "Film" / "clip.mov").exists()
    assert (env.nas / "home" / "clip.mov").read_bytes() == b"0123456789"
    assert (env.nas / "home" / "clip.mov").stat().st_mtime_ns == mtime
    # a whole folder, then a rescan: same ids, no content change
    film = child(env.public, h, media["id"], "Film")
    (env.nas / "media" / "Film" / "sub").mkdir()
    (env.nas / "media" / "Film" / "sub" / "x.txt").write_bytes(b"x")
    env.public.get(f"/v1/items/{film['id']}/children", headers=h)
    sub = child(env.public, h, film["id"], "sub")
    x = child(env.public, h, sub["id"], "x.txt")
    assert env.public.patch(f"/v1/items/{film['id']}", json={"parent_id": home["id"]},
                            headers=h).status_code == 200
    assert (env.nas / "home" / "Film" / "sub" / "x.txt").read_bytes() == b"x"
    assert not (env.nas / "media" / "Film").exists()
    env.gw.reconcile(env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],)),
                     env.gw.backend.authenticate("carol", "pw-carol"))
    x2 = env.public.get(f"/v1/items/{x['id']}", headers=h).json()
    assert x2["content_etag"] == x["content_etag"] and x2["parent_id"] == sub["id"]
    # an upload started for media but committed into home (resume across a move)
    uid = upload(env.public, h, b"travels", near=media["id"])
    r = env.public.post(f"/v1/uploads/{uid}/commit", json={"parent_id": home["id"], "name": "t.bin"},
                        headers=h)
    assert r.status_code == 200, r.text
    assert (env.nas / "home" / "t.bin").read_bytes() == b"travels"
    assert not any((env.nas / "media" / ".loft" / "uploads").iterdir())


def test_rescan_sees_external_changes_in_any_share(env):
    h, root = all_shares(env)
    anchor = env.public.get("/v1/changes/latest", headers=h).json()["anchor"]
    (env.nas / "media" / "Film" / "ext.mov").write_bytes(b"x")
    env.gw.reconcile(env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],)),
                     env.gw.backend.authenticate("carol", "pw-carol"))
    names = [i["name"] for i in env.public.get("/v1/changes", params={"anchor": anchor},
                                               headers=h).json()["updated"]]
    assert "ext.mov" in names and "backup" not in names and "dumps" not in names
