import os
import threading
import time

import pytest

from loft_gateway.storage.base import AccessDenied, NotFound, StorageError
from loft_gateway.storage.local import LocalSession

from conftest import child, rooted, upload


def test_list_and_download_with_range(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    assert a["size"] == 5 and a["content_etag"] == "c1"
    r = env.public.get(f"/v1/items/{a['id']}/content", headers=h)
    assert r.content == b"hello" and r.headers["etag"] == '"c1"'
    r = env.public.get(f"/v1/items/{a['id']}/content", headers={**h, "Range": "bytes=1-3"})
    assert r.status_code == 206 and r.content == b"ell"
    assert r.headers["content-range"] == "bytes 1-3/5"
    r = env.public.get(f"/v1/items/{a['id']}/content", headers={**h, "Range": "bytes=-2"})
    assert r.content == b"lo"


def test_excluded_names_hidden_and_refused(env):
    d = env.nas / "home" / "alice"
    (d / ".DS_Store").write_bytes(b"x")
    (d / "~$report.docx").write_bytes(b"x")
    h, root = rooted(env)
    names = [i["name"] for i in env.public.get(f"/v1/items/{root['root_item_id']}/children",
                                                headers=h).json()["items"]]
    assert names == ["Docs"]
    uid = upload(env.public, h, b"lock")
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"parent_id": root["root_item_id"], "name": "~$x.docx"}, headers=h)
    assert r.status_code == 422


def test_chunked_upload_resume_and_create(env):
    h, root = rooted(env)
    data = os.urandom(1000)
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    u = env.public.post("/v1/uploads", json={"size": 1000, "sha256": sha}, headers=h).json()
    uid = u["upload_id"]
    assert env.public.put(f"/v1/uploads/{uid}", params={"offset": 0}, content=data[:400],
                          headers=h).status_code == 200
    r = env.public.put(f"/v1/uploads/{uid}", params={"offset": 0}, content=data[:400], headers=h)
    assert r.status_code == 409 and r.json()["received"] == 400       # resume point
    r = env.public.put(f"/v1/uploads/{uid}", params={"offset": 400}, content=data[400:],
                       headers={**h, "X-Chunk-SHA256": "0" * 64})
    assert r.status_code == 422                                       # chunk hash checked
    env.public.put(f"/v1/uploads/{uid}", params={"offset": 400}, content=data[400:], headers=h)
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"parent_id": root["root_item_id"], "name": "blob.bin"}, headers=h)
    assert r.status_code == 200 and r.json()["result"] == "created"
    assert (env.nas / "home" / "alice" / "blob.bin").read_bytes() == data


def test_whole_file_hash_verified(env):
    h, root = rooted(env)
    u = env.public.post("/v1/uploads", json={"size": 3, "sha256": "a" * 64}, headers=h).json()
    env.public.put(f"/v1/uploads/{u['upload_id']}", params={"offset": 0}, content=b"abc", headers=h)
    r = env.public.post(f"/v1/uploads/{u['upload_id']}/commit",
                        json={"parent_id": root["root_item_id"], "name": "x"}, headers=h)
    assert r.status_code == 422 and r.json()["error"] == "file_hash_mismatch"


def test_modify_cas_and_conflict_copy(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    uid = upload(env.public, h, b"v2")
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"item_id": a["id"], "if_match": "c1"}, headers=h).json()
    assert r["result"] == "modified" and r["item"]["content_etag"] == "c2"
    assert r["item"]["id"] == a["id"]
    # a second Mac still based on c1 -> keep both, never overwrite
    uid = upload(env.public, h, b"stale edit")
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"item_id": a["id"], "if_match": "c1"}, headers=h).json()
    assert r["result"] == "conflict" and r["item"]["id"] != a["id"]
    assert r["item"]["name"].startswith("a (conflict, Test Mac, ") and r["item"]["name"].endswith(".txt")
    assert (env.nas / "home/alice/Docs/a.txt").read_bytes() == b"v2"
    vs = env.public.get(f"/v1/items/{a['id']}/versions", headers=h).json()["versions"]
    assert len(vs) == 1 and vs[0]["content_version"] == 1


def test_two_conflicts_in_one_minute_get_distinct_names(env):
    """Same file, same device, same minute: the second conflict copy must not
    collide with the first (it used to fail with 409 name_exists_on_nas)."""
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    uid = upload(env.public, h, b"v2")
    env.public.post(f"/v1/uploads/{uid}/commit", json={"item_id": a["id"], "if_match": "c1"}, headers=h)
    names = []
    for body in (b"stale one", b"stale two", b"stale three"):
        uid = upload(env.public, h, body)
        r = env.public.post(f"/v1/uploads/{uid}/commit",
                            json={"item_id": a["id"], "if_match": "c1"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["result"] == "conflict"
        names.append(r.json()["item"]["name"])
    assert len(set(names)) == 3
    assert names[1].endswith(" 2).txt") and names[2].endswith(" 3).txt")
    assert (env.nas / "home/alice/Docs" / names[2]).read_bytes() == b"stale three"


def test_create_collision_without_replaces_keeps_both(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    uid = upload(env.public, h, b"other")
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"parent_id": docs["id"], "name": "A.TXT"}, headers=h).json()
    assert r["result"] == "conflict"                    # case-insensitive collision
    assert (env.nas / "home/alice/Docs/a.txt").read_bytes() == b"hello"


def test_safe_save_create_before_delete(env):
    """Office: temp -> rename over original. Client sends create(replaces=X),
    then deleteItem(X). One new version on the NAS, nothing in trash."""
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    uid = upload(env.public, h, b"saved by word")
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"parent_id": docs["id"], "name": "a.txt", "replaces": a["id"],
                              "if_match": "c1"}, headers=h).json()
    assert r["result"] == "replaced" and r["replaced_id"] == a["id"]
    new = r["item"]
    assert new["content_etag"] == "c2"
    d = env.public.delete(f"/v1/items/{a['id']}", headers=h).json()
    assert d["result"] == "superseded" and d["superseded_by"] == new["id"]
    assert (env.nas / "home/alice/Docs/a.txt").read_bytes() == b"saved by word"
    assert env.gw.db.one("SELECT COUNT(*) n FROM trash")["n"] == 0
    vs = env.public.get(f"/v1/items/{new['id']}/versions", headers=h).json()["versions"]
    assert len(vs) == 1


def test_safe_save_delete_before_create(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    assert env.public.delete(f"/v1/items/{a['id']}", params={"if_match": "c1"},
                             headers=h).json()["result"] == "trashed"
    uid = upload(env.public, h, b"new content")
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"parent_id": docs["id"], "name": "a.txt"}, headers=h).json()
    assert r["result"] == "replaced"
    assert env.gw.db.one("SELECT COUNT(*) n FROM trash")["n"] == 0
    vs = env.public.get(f"/v1/items/{r['item']['id']}/versions", headers=h).json()["versions"]
    assert len(vs) == 1 and vs[0]["size"] == 5


def test_delete_outside_window_stays_in_trash(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    env.public.delete(f"/v1/items/{a['id']}", headers=h)
    env.gw.db.conn.execute("UPDATE trash SET deleted_at=deleted_at-3600")
    uid = upload(env.public, h, b"unrelated")
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"parent_id": docs["id"], "name": "a.txt"}, headers=h).json()
    assert r["result"] == "created"
    assert env.gw.db.one("SELECT COUNT(*) n FROM trash")["n"] == 1


def test_delete_refuses_stale_etag(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    r = env.public.delete(f"/v1/items/{a['id']}", params={"if_match": "c9"}, headers=h)
    assert r.status_code == 412
    assert (env.nas / "home/alice/Docs/a.txt").exists()


def test_rename_move_mkdir(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    sub = env.public.post("/v1/folders", json={"parent_id": root["root_item_id"], "name": "Sub"},
                          headers=h).json()
    r = env.public.patch(f"/v1/items/{a['id']}", json={"name": "b.txt", "parent_id": sub["id"],
                                                       "if_match": "m1"}, headers=h)
    assert r.status_code == 200 and r.json()["meta_etag"] == "m2"
    assert (env.nas / "home/alice/Sub/b.txt").read_bytes() == b"hello"
    r = env.public.patch(f"/v1/items/{a['id']}", json={"name": "c.txt", "if_match": "m1"},
                         headers=h)
    assert r.status_code == 412
    r = env.public.patch(f"/v1/items/{sub['id']}", json={"parent_id": sub["id"]}, headers=h)
    assert r.status_code == 400


def test_change_feed_and_longpoll(env):
    h, root = rooted(env)
    anchor = root["anchor"]
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()
    assert r["anchor"] == anchor and not r["updated"]
    env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=h)
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()
    assert [i["name"] for i in r["updated"]] == ["Docs"]
    t0 = time.time()
    r2 = env.public.get("/v1/changes", params={"anchor": r["anchor"], "wait": 1}, headers=h).json()
    assert time.time() - t0 >= 0.9 and not r2["updated"]
    bad = env.public.get("/v1/changes", params={"anchor": "otherepoch.0"}, headers=h)
    assert bad.status_code == 410 and bad.json()["error"] == "anchor_expired"
    # shutting down: an open long-poll answers at once, unchanged anchor
    threading.Timer(0.3, env.gw.release_waiters).start()
    t0 = time.time()
    r3 = env.public.get("/v1/changes", params={"anchor": r["anchor"], "wait": 25}, headers=h).json()
    assert time.time() - t0 < 3 and r3["anchor"] == r["anchor"] and not r3["updated"]


def test_external_changes_detected(env):
    """Edits made over SMB by someone else: content change, rename (same inode
    = same item id), delete."""
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    anchor = env.public.get("/v1/changes/latest", headers=h).json()["anchor"]
    p = env.nas / "home/alice/Docs/a.txt"
    p.write_bytes(b"changed outside")
    root_row = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    sess = env.gw.backend.authenticate("alice", "pw-alice")
    env.gw.reconcile(root_row, sess)
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()
    assert r["updated"][0]["id"] == a["id"] and r["updated"][0]["content_etag"] == "c2"
    anchor = r["anchor"]
    (env.nas / "home/alice/Moved").mkdir()
    p.rename(env.nas / "home/alice/Moved/renamed.txt")
    env.gw.reconcile(root_row, sess)
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()
    moved = [i for i in r["updated"] if i["id"] == a["id"]]
    assert moved and moved[0]["name"] == "renamed.txt" and not r["deleted"]
    anchor = r["anchor"]
    (env.nas / "home/alice/Moved/renamed.txt").unlink()
    env.gw.reconcile(root_row, sess)
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()
    assert r["deleted"] == [a["id"]]


def test_external_folder_move_keeps_children(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    root_row = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    sess = env.gw.backend.authenticate("alice", "pw-alice")
    env.gw.reconcile(root_row, sess)
    anchor = env.public.get("/v1/changes/latest", headers=h).json()["anchor"]
    (env.nas / "home/alice/Archive").mkdir()
    (env.nas / "home/alice/Docs").rename(env.nas / "home/alice/Archive/Docs2")
    env.gw.reconcile(root_row, sess)
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()
    assert not r["deleted"]
    assert env.public.get(f"/v1/items/{a['id']}", headers=h).status_code == 200
    assert env.public.get(f"/v1/items/{docs['id']}", headers=h).json()["name"] == "Docs2"


def test_restore_version(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    uid = upload(env.public, h, b"v2")
    env.public.post(f"/v1/uploads/{uid}/commit", json={"item_id": a["id"]}, headers=h)
    v = env.public.get(f"/v1/items/{a['id']}/versions", headers=h).json()["versions"][0]
    r = env.public.post(f"/v1/items/{a['id']}/versions/{v['id']}/restore", headers=h)
    assert r.status_code == 200 and r.json()["content_etag"] == "c3"
    assert (env.nas / "home/alice/Docs/a.txt").read_bytes() == b"hello"
    vs = env.public.get(f"/v1/items/{a['id']}/versions", headers=h).json()["versions"]
    assert len(vs) == 1 and vs[0]["size"] == 2           # v2 kept as history


def test_version_pruning(env):
    env.gw.s.versions_keep = 3
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    for i in range(6):
        uid = upload(env.public, h, f"v{i}".encode())
        env.public.post(f"/v1/uploads/{uid}/commit", json={"item_id": a["id"]}, headers=h)
    vs = env.public.get(f"/v1/items/{a['id']}/versions", headers=h).json()["versions"]
    assert len(vs) == 3
    vdir = env.nas / "home/alice/.loft/versions"          # per-root meta area
    assert len(list(vdir.iterdir())) == 3


def test_meta_area_not_visible(env):
    h, root = rooted(env, path="")                       # root = whole share
    uid = upload(env.public, h, b"x")
    env.public.post(f"/v1/uploads/{uid}/commit",
                    json={"parent_id": root["root_item_id"], "name": "x.bin"}, headers=h)
    names = [i["name"] for i in env.public.get(f"/v1/items/{root['root_item_id']}/children",
                                                headers=h).json()["items"]]
    assert ".loft" not in names and "x.bin" in names
    assert (env.nas / "home/.loft").is_dir()         # whole-share root: area at share top


def test_meta_area_is_per_root(env):
    """docs/design.md §B.3: uploads/versions/trash live inside
    the chosen root, so a user never needs write access above it."""
    h, root = rooted(env)                                # root = home/alice
    uid = upload(env.public, h, b"one")
    a = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"parent_id": root["root_item_id"], "name": "a.txt"},
                        headers=h).json()["item"]
    uid = upload(env.public, h, b"two")
    env.public.post(f"/v1/uploads/{uid}/commit", json={"item_id": a["id"]}, headers=h)
    env.public.delete(f"/v1/items/{a['id']}", headers=h)
    meta = env.nas / "home/alice/.loft"
    assert {p.name for p in meta.iterdir()} == {"uploads", "versions", "trash"}
    assert len(list((meta / "versions").iterdir())) == 1
    assert len(list((meta / "trash").iterdir())) == 1
    assert not (env.nas / "home/.loft").exists()     # nothing written above the root


def test_background_rescan_runs_as_user(env):
    env.gw.s.background_rescan = True
    h, root = rooted(env)
    anchor = root["anchor"]
    env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=h)
    for _ in range(50):
        if env.gw.db.one("SELECT last_scan_at FROM roots WHERE id=?", (root["id"],))[0]:
            break
        time.sleep(0.1)
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()
    assert sorted(i["name"] for i in r["updated"]) == ["Docs", "a.txt"]


def test_cas_sees_smb_side_edit(env):
    """Someone edits the file over SMB after the Mac fetched it. The Mac's save
    based on c1 must become a conflict copy, not overwrite the SMB edit."""
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    p = env.nas / "home/alice/Docs/a.txt"
    p.write_bytes(b"edited over smb")
    uid = upload(env.public, h, b"mac edit")
    r = env.public.post(f"/v1/uploads/{uid}/commit",
                        json={"item_id": a["id"], "if_match": "c1"}, headers=h).json()
    assert r["result"] == "conflict"
    assert p.read_bytes() == b"edited over smb"


def test_lost_folder_access_is_403_not_a_mass_delete(env):
    """The Loft account loses its folder: the rescan must not read that as an
    empty folder (it used to tell every Mac to delete everything), and the
    long-poll says access_denied so the menu shows "No access"."""
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    child(env.public, h, docs["id"], "a.txt")
    anchor = env.public.get("/v1/changes/latest", headers=h).json()["anchor"]
    root_row = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    sess = env.gw.backend.authenticate("alice", "pw-alice")
    env.gw.backend.users["alice"]["shares"] = []
    with pytest.raises(AccessDenied):
        env.gw.reconcile(root_row, sess)
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h)
    assert r.status_code == 403 and r.json()["error"] == "access_denied"
    env.gw.backend.users["alice"]["shares"] = ["home"]
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()
    assert not r["deleted"]


def test_vanished_root_is_not_a_mass_delete(env):
    h, root = rooted(env)
    child(env.public, h, root["root_item_id"], "Docs")
    anchor = env.public.get("/v1/changes/latest", headers=h).json()["anchor"]
    root_row = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    (env.nas / "home/alice").rename(env.nas / "home/alice-old")
    with pytest.raises(NotFound):
        env.gw.reconcile(root_row, env.gw.backend.authenticate("alice", "pw-alice"))
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h)
    assert r.status_code == 404 and r.json()["error"] == "root_missing"
    assert env.gw.db.one("SELECT COUNT(*) n FROM items WHERE deleted=1")["n"] == 0


def test_nas_down_behind_gateway_is_nas_unavailable(env, monkeypatch):
    h, root = rooted(env)
    anchor = root["anchor"]

    def down(*_a, **_k):
        raise StorageError("SMB connect to nas failed: timed out")
    monkeypatch.setattr(LocalSession, "stat", down)
    r = env.public.get("/v1/changes", params={"anchor": anchor}, headers=h)
    assert r.status_code == 503 and r.json()["error"] == "nas_unavailable"
