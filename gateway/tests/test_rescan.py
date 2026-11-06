"""rescan(): the whole root one folder at a time, each committed on its own.

On a 300,000-item all-shares root the old full pass took over half an hour,
committed nothing until the very end, and a single error anywhere in the walk
threw the whole pass away without a word (it was swallowed in maybe_rescan).
Folders that became visible never showed up.
"""
import dataclasses
import struct

import pytest

from loft_gateway.rootfs import Blocked
from loft_gateway.storage.base import StorageError
from loft_gateway.storage.smb import _EPOCH_FT, _dir_entries

from conftest import all_shares, child, rooted


class FlakySession:
    """Listing `bad` fails with a plain StorageError (a timeout, a dropped link);
    `on_list(path)` runs after each listing."""

    def __init__(self, inner, bad=(), on_list=None):
        self.inner, self.bad, self.on_list = inner, set(bad), on_list

    def scandir(self, share, path):
        if path in self.bad:
            raise StorageError(f"{share}/{path}: STATUS_IO_TIMEOUT")
        out = self.inner.scandir(share, path)
        if self.on_list:
            self.on_list(path)
        return out

    def __getattr__(self, name):
        return getattr(self.inner, name)


def setup(env):
    h, root = rooted(env)
    root_row = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    sess = env.gw.backend.authenticate("alice", "pw-alice")
    return h, root, root_row, sess


def changes(env, h, anchor):
    return env.public.get("/v1/changes", params={"anchor": anchor}, headers=h).json()


def latest(env, h):
    return env.public.get("/v1/changes/latest", headers=h).json()["anchor"]


def names(env, parent_id):
    return sorted(r["name"] for r in env.gw.db.all(
        "SELECT name FROM items WHERE parent_id=? AND deleted=0", (parent_id,)))


def test_rescan_finds_new_folders_at_every_depth(env):
    h, root, root_row, sess = setup(env)
    (env.nas / "home/alice/New/Deeper").mkdir(parents=True)
    (env.nas / "home/alice/New/Deeper/x.txt").write_bytes(b"x")
    st = env.gw.rescan(root_row, sess)
    assert st["skipped"] == 0 and st["folders"] >= 4
    new = child(env.public, h, root["root_item_id"], "New")
    deeper = child(env.public, h, new["id"], "Deeper")
    assert names(env, deeper["id"]) == ["x.txt"]
    assert env.gw.db.one("SELECT last_scan_at FROM roots WHERE id=?",
                         (root["id"],))["last_scan_at"]


def test_folder_that_fails_is_skipped_and_kept(env):
    """One unreadable folder used to end the whole pass; now it is logged, its rows
    stay as they were (not deleted), and every other folder is still done."""
    h, root, root_row, sess = setup(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    child(env.public, h, docs["id"], "a.txt")
    (env.nas / "home/alice/Other").mkdir()
    anchor = latest(env, h)
    (env.nas / "home/alice/Docs/a.txt").unlink()           # would read as a delete...
    st = env.gw.rescan(root_row, FlakySession(sess, bad={"alice/Docs"}))
    assert st["skipped"] == 1
    assert names(env, docs["id"]) == ["a.txt"]             # ...but Docs couldn't be read
    r = changes(env, h, anchor)
    assert not r["deleted"] and [i["name"] for i in r["updated"]] == ["Other"]


def test_unreadable_subfolder_in_full_reconcile_keeps_its_rows(env):
    h, root, root_row, sess = setup(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    child(env.public, h, docs["id"], "a.txt")
    env.gw.reconcile(root_row, FlakySession(sess, bad={"alice/Docs"}))
    assert names(env, docs["id"]) == ["a.txt"]


def test_root_that_fails_raises(env):
    h, root, root_row, sess = setup(env)
    with pytest.raises(StorageError):
        env.gw.rescan(root_row, FlakySession(sess, bad={"alice"}))


def test_each_folder_is_committed_as_the_pass_goes(env):
    """What the first folders turned up is in the change feed before the pass ends."""
    h, root, root_row, sess = setup(env)
    (env.nas / "home/alice/Early").mkdir()
    (env.nas / "home/alice/Docs/Sub").mkdir()
    seen_mid_pass = []

    def on_list(path):
        if path == "alice/Docs/Sub":
            seen_mid_pass.append(env.gw.db.one(
                "SELECT 1 FROM items WHERE name='Early' AND deleted=0") is not None)
    env.gw.rescan(root_row, FlakySession(sess, on_list=on_list))
    assert seen_mid_pass == [True]


@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_move_between_folders_keeps_the_id(env, order):
    """Folder by folder, the old place and the new one are listed in separate
    commits, in either order. The item must come out moved, never delete+create."""
    h, root, root_row, sess = setup(env)
    (env.nas / "home/alice/A" if order == "source_first" else env.nas / "home/alice/Z").mkdir()
    src = "A" if order == "source_first" else "Z"
    dst = "Z" if order == "source_first" else "A"
    (env.nas / f"home/alice/{dst}").mkdir()
    (env.nas / f"home/alice/{src}/f.txt").write_bytes(b"data")
    env.gw.rescan(root_row, sess)
    f = child(env.public, h, child(env.public, h, root["root_item_id"], src)["id"], "f.txt")
    anchor = latest(env, h)
    (env.nas / f"home/alice/{src}/f.txt").rename(env.nas / f"home/alice/{dst}/g.txt")
    env.gw.rescan(root_row, sess)
    r = changes(env, h, anchor)
    assert not r["deleted"]
    assert [(i["id"], i["name"]) for i in r["updated"]] == [(f["id"], "g.txt")]


def test_moved_folder_keeps_its_children(env):
    h, root, root_row, sess = setup(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    env.gw.rescan(root_row, sess)
    anchor = latest(env, h)
    (env.nas / "home/alice/Archive").mkdir()
    (env.nas / "home/alice/Docs").rename(env.nas / "home/alice/Archive/Docs2")
    env.gw.rescan(root_row, sess)
    r = changes(env, h, anchor)
    assert not r["deleted"]
    assert env.public.get(f"/v1/items/{a['id']}", headers=h).status_code == 200
    assert env.public.get(f"/v1/items/{docs['id']}", headers=h).json()["name"] == "Docs2"


def test_listing_the_target_folder_picks_up_a_move(env):
    """children() (one folder, what a Mac asks when it opens one) also sees a move
    in from elsewhere, if the old path is really gone."""
    h, root, root_row, sess = setup(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    (env.nas / "home/alice/Other").mkdir()
    other = child(env.public, h, root["root_item_id"], "Other")
    (env.nas / "home/alice/Docs/a.txt").rename(env.nas / "home/alice/Other/a.txt")
    got = env.public.get(f"/v1/items/{other['id']}/children", headers=h).json()["items"]
    assert [i["id"] for i in got] == [a["id"]]


def test_copy_is_not_a_move(env):
    """Same file id in two places can't happen on one volume, but a hard link or a
    reused id must not pull the item away from a path that still has it."""
    h, root, root_row, sess = setup(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    (env.nas / "home/alice/Other").mkdir()
    other = child(env.public, h, root["root_item_id"], "Other")
    (env.nas / "home/alice/Other/a.txt").hardlink_to(env.nas / "home/alice/Docs/a.txt")
    got = env.public.get(f"/v1/items/{other['id']}/children", headers=h).json()["items"]
    assert [i["id"] for i in got] != [a["id"]]
    assert env.public.get(f"/v1/items/{a['id']}", headers=h).json()["parent_id"] == docs["id"]


def test_deleted_outside_goes_at_the_end(env):
    h, root, root_row, sess = setup(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    anchor = latest(env, h)
    (env.nas / "home/alice/Docs/a.txt").unlink()
    st = env.gw.rescan(root_row, sess)
    assert changes(env, h, anchor)["deleted"] == [a["id"]] and st["changes"] == 1


def test_deleted_folder_takes_its_children(env):
    h, root, root_row, sess = setup(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    anchor = latest(env, h)
    (env.nas / "home/alice/Docs/a.txt").unlink()
    (env.nas / "home/alice/Docs").rmdir()
    env.gw.rescan(root_row, sess)
    assert sorted(changes(env, h, anchor)["deleted"]) == sorted([docs["id"], a["id"]])


def test_write_during_pass_is_not_deleted_at_the_end(env):
    """An item deferred as missing, then written by a Mac before the pass ends, is
    not deleted at the end: the pass's view of it is older than that write."""
    h, root, root_row, sess = setup(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    (env.nas / "home/alice/Docs/Sub").mkdir()
    away = env.tmp / "a.txt"
    (env.nas / "home/alice/Docs/a.txt").rename(away)
    st0 = env.gw.db.one("SELECT updated_seq FROM items WHERE id=?", (a["id"],))["updated_seq"]

    def on_list(path):
        if path == "alice/Docs/Sub":   # Docs is committed, a.txt deferred; a Mac saves it again
            row = env.gw.db.one("SELECT deleted, updated_seq FROM items WHERE id=?", (a["id"],))
            assert not row["deleted"] and row["updated_seq"] == st0   # deferred, not yet gone
            away.rename(env.nas / "home/alice/Docs/a.txt")
            with env.gw.db.tx() as c:
                env.gw._record(c, root["id"], a["id"], "upsert")
    env.gw.rescan(root_row, FlakySession(sess, on_list=on_list))
    assert env.public.get(f"/v1/items/{a['id']}", headers=h).status_code == 200


def test_rescan_stops_when_the_gateway_shuts_down(env):
    h, root, root_row, sess = setup(env)
    env.gw._closing = True
    st = env.gw.rescan(root_row, sess)
    assert st["folders"] == 0
    assert env.gw.db.one("SELECT last_scan_at FROM roots WHERE id=?",
                         (root["id"],))["last_scan_at"] is None


def test_background_rescan_logs_a_failure(env, caplog):
    h, root, root_row, sess = setup(env)
    env.gw.s.background_rescan = True
    env.gw.rescan = lambda *a: (_ for _ in ()).throw(StorageError("boom"))
    env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=h)
    for t in __import__("threading").enumerate():
        if t.name.startswith("rescan-"):
            t.join(5)
    assert any("failed" in r.message and r.levelname == "ERROR" for r in caplog.records)


def _raw_entry(name, attrs, size, ft, fid, last):
    n = name.encode("utf-16-le")
    head = struct.pack("<IIqqqqqqIIIIQ", 0, 0, ft, ft, ft, ft, size, 0, attrs, len(n), 0, 0, fid)
    body = head + n
    pad = (-len(body)) % 8
    body += b"\0" * pad
    if not last:
        body = struct.pack("<I", len(body)) + body[4:]
    return body


def test_smb_listing_keeps_100ns_mtime():
    """FILETIME is in 100 ns; smbprotocol's datetime drops the last digit, stat()
    keeps it, and commit compares the two. The listing must give stat()'s value."""
    ft = _EPOCH_FT + 17_620_000_000_123_456_7
    buf = (_raw_entry(".", 0x10, 0, ft, 1, False) + _raw_entry("ab c", 0x20, 42, ft, 99, False)
           + _raw_entry("dir", 0x10, 4096, ft, 7, True))
    got = list(_dir_entries(buf))
    assert got == [("ab c", 0x20, 42, (ft - _EPOCH_FT) * 100, 99),
                   ("dir", 0x10, 4096, (ft - _EPOCH_FT) * 100, 7)]
    assert got[0][3] % 1000 == 700


# ── file ids repeat across shares; listings that don't parse ──────────────────
# When every share is its own btrfs subvolume, Samba hands out the raw inode
# number, so items in different shares share file ids. FidSession reports one file under another file's id, as two
# shares would.

class FidSession:
    def __init__(self, inner, alias):
        self.inner, self.alias = inner, alias          # {real file id: reported file id}

    def _fix(self, e):
        return dataclasses.replace(e, file_id=self.alias.get(e.file_id, e.file_id))

    def scandir(self, share, path):
        return [self._fix(e) for e in self.inner.scandir(share, path)]

    def stat(self, share, path):
        return self._fix(self.inner.stat(share, path))

    def __getattr__(self, name):
        return getattr(self.inner, name)


def carol(env):
    h, root = all_shares(env)
    root_row = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    return h, root, root_row, env.gw.backend.authenticate("carol", "pw-carol")


def item_at(env, root_id, *names):
    cur = root_id
    for n in names:
        cur = env.gw.db.one("SELECT id FROM items WHERE parent_id=? AND name=? AND deleted=0",
                            (cur, n))["id"]
    return cur


def test_move_found_although_another_share_has_the_same_file_id(env):
    """clip.mov (media, indexed first = the older row) has f.txt's file id and is
    still there. A move of f.txt must still be found: one candidate per root
    (db.one) was clip.mov, found at its path, so f.txt became delete+create."""
    h, root, root_row, sess = carol(env)
    bs, clip = env.nas / "Family", env.nas / "media/Film/clip.mov"
    env.gw.rescan(root_row, sess)
    (bs / "A").mkdir(); (bs / "Z").mkdir(); (bs / "A/f.txt").write_bytes(b"data")
    s = FidSession(sess, {clip.stat().st_ino: (bs / "A/f.txt").stat().st_ino})
    env.gw.rescan(root_row, s)
    f = item_at(env, root["id"], "Family", "A", "f.txt")
    anchor = latest(env, h)
    (bs / "A/f.txt").rename(bs / "Z/g.txt")
    env.gw.rescan(root_row, s)
    r = changes(env, h, anchor)
    assert not r["deleted"] and [(i["id"], i["name"]) for i in r["updated"]] == [(f, "g.txt")]


@pytest.mark.parametrize("how", ["rescan", "full_reconcile"])
def test_new_file_never_takes_the_id_of_an_item_in_another_share(env, how):
    """clip.mov (media) is deleted; a new file in Family carries the same
    file id. It is a new item, and clip.mov's id goes with clip.mov."""
    h, root, root_row, sess = carol(env)
    clip = env.nas / "media/Film/clip.mov"
    env.gw.rescan(root_row, sess)
    old = item_at(env, root["id"], "media", "Film", "clip.mov")
    fid = clip.stat().st_ino
    clip.unlink()
    new = env.nas / "Family/notes/n.txt"
    new.write_bytes(b"something else entirely")
    s = FidSession(sess, {new.stat().st_ino: fid})
    anchor = latest(env, h)
    env.gw.rescan(root_row, s) if how == "rescan" else env.gw.reconcile(root_row, s)
    assert changes(env, h, anchor)["deleted"] == [old]
    assert item_at(env, root["id"], "Family", "notes", "n.txt") != old


@pytest.mark.parametrize("how", ["rescan", "full_reconcile"])
def test_file_id_twice_in_one_share_matches_nothing(env, how):
    """Two items in one share with the same file id (a stale index) both gone,
    a new file with that id: no way to tell which one it is, so it is new."""
    h, root, root_row, sess = setup(env)
    nas = env.nas / "home/alice"
    (nas / "X").mkdir(); (nas / "X/b.txt").write_bytes(b"b"); (nas / "N").mkdir()
    a_ino = (nas / "Docs/a.txt").stat().st_ino
    s = FidSession(sess, {(nas / "X/b.txt").stat().st_ino: a_ino})
    env.gw.rescan(root_row, s)
    a, b = item_at(env, root["id"], "Docs", "a.txt"), item_at(env, root["id"], "X", "b.txt")
    (nas / "Docs/a.txt").unlink(); (nas / "X/b.txt").unlink()
    (nas / "N/n.txt").write_bytes(b"n")
    s.alias[(nas / "N/n.txt").stat().st_ino] = a_ino
    anchor = latest(env, h)
    env.gw.rescan(root_row, s) if how == "rescan" else env.gw.reconcile(root_row, s)
    assert sorted(changes(env, h, anchor)["deleted"]) == sorted([a, b])
    assert item_at(env, root["id"], "N", "n.txt") not in (a, b)


def test_full_reconcile_moves_only_within_a_share(env):
    h, root, root_row, sess = carol(env)
    bs, clip = env.nas / "Family", env.nas / "media/Film/clip.mov"
    (bs / "A").mkdir(); (bs / "Z").mkdir(); (bs / "A/f.txt").write_bytes(b"data")
    s = FidSession(sess, {clip.stat().st_ino: (bs / "A/f.txt").stat().st_ino})
    env.gw.reconcile(root_row, s)
    f = item_at(env, root["id"], "Family", "A", "f.txt")
    c = item_at(env, root["id"], "media", "Film", "clip.mov")
    anchor = latest(env, h)
    (bs / "A/f.txt").rename(bs / "Z/g.txt")
    env.gw.reconcile(root_row, s)
    r = changes(env, h, anchor)
    assert not r["deleted"] and [(i["id"], i["name"]) for i in r["updated"]] == [(f, "g.txt")]
    assert item_at(env, root["id"], "media", "Film", "clip.mov") == c


def _bad(name_bytes, nxt=0):
    return struct.pack("<IIqqqqqqIIIIQ", nxt, 0, 0, 0, _EPOCH_FT, 0, 5, 8, 0x20,
                       len(name_bytes), 0, 0, 42) + name_bytes


def test_listing_that_does_not_parse_is_a_storage_error():
    """So reconcile skips that one folder; a UnicodeDecodeError or struct.error
    ended the whole pass, on every pass."""
    with pytest.raises(StorageError):
        list(_dir_entries(_bad(b"\x00\xd8")))                   # lone high surrogate
    with pytest.raises(StorageError):
        list(_dir_entries(_bad("a".encode("utf-16-le"), nxt=4096)))   # next entry past the end


def test_folder_blocked_mid_pass_is_skipped(env):
    h, root, root_row, sess = setup(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    (env.nas / "home/alice/Other").mkdir()
    real = env.gw.reconcile

    def reconcile(root_, session, start_id=None, **kw):
        if start_id == docs["id"]:
            raise Blocked("home/alice/Docs")
        return real(root_, session, start_id, **kw)
    env.gw.reconcile = reconcile
    st = env.gw.rescan(root_row, sess)
    assert st["skipped"] == 1 and child(env.public, h, root["root_item_id"], "Other")
