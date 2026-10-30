"""reconcile() lists the NAS first and opens its index transaction after. Any write
that lands in between (a commit, rename, delete from a Mac, or another reconcile)
must survive: the listing is older than that write and has no say over it.

Found on the first real Mac run (2026-10-29): three files saved at once into one
folder; each commit's own reconcile had listed the folder before the others
renamed their files in, so it deleted their fresh rows and Finder dropped the
files although they were on the NAS.
"""
from loft_gateway.storage.local import LocalSession

from conftest import child, rooted, upload


class HookedSession:
    """A session whose Nth scandir runs `hook` after it has read the listing:
    exactly the window between reconcile's NAS walk and its index transaction."""

    def __init__(self, inner, hook, on_call=1):
        self.inner, self.hook, self.on_call, self.calls = inner, hook, on_call, 0

    def scandir(self, share, path):
        out = self.inner.scandir(share, path)
        self.calls += 1
        if self.calls == self.on_call:
            self.hook()
        return out

    def __getattr__(self, name):
        return getattr(self.inner, name)


def setup(env):
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    root_row = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    sess = env.gw.backend.authenticate("alice", "pw-alice")
    return h, root, root_row, docs, a, sess


def live(env, parent_id):
    return env.gw.db.all("SELECT * FROM items WHERE parent_id=? AND deleted=0 ORDER BY name_key",
                         (parent_id,))


def test_commit_during_reconcile_is_not_deleted(env):
    h, root, root_row, docs, a, sess = setup(env)
    uid = upload(env.public, h, b"new file")
    made = {}

    def commit():
        r = env.public.post(f"/v1/uploads/{uid}/commit",
                            json={"parent_id": docs["id"], "name": "b.txt"}, headers=h)
        assert r.status_code == 200, r.text
        made.update(r.json()["item"])

    env.gw.reconcile(root_row, HookedSession(sess, commit), docs["id"], max_depth=1)
    assert [r["name"] for r in live(env, docs["id"])] == ["a.txt", "b.txt"]
    assert env.public.get(f"/v1/items/{made['id']}", headers=h).status_code == 200
    # and the next honest pass leaves it alone (same id, no new change)
    before = env.gw.latest_anchor(root_row)
    env.gw.reconcile(root_row, sess, docs["id"], max_depth=1)
    assert env.gw.latest_anchor(root_row) == before


def test_modify_during_reconcile_keeps_new_content_meta(env):
    h, root, root_row, docs, a, sess = setup(env)
    uid = upload(env.public, h, b"a much longer body")

    def modify():
        r = env.public.post(f"/v1/uploads/{uid}/commit",
                            json={"item_id": a["id"], "if_match": "c1"}, headers=h)
        assert r.json()["result"] == "modified", r.text

    env.gw.reconcile(root_row, HookedSession(sess, modify), docs["id"], max_depth=1)
    it = env.public.get(f"/v1/items/{a['id']}", headers=h).json()
    # The stale listing (5 bytes) must not roll the size back or bump the etag.
    assert it["size"] == len(b"a much longer body") and it["content_etag"] == "c2"


def test_delete_during_reconcile_is_not_resurrected(env):
    h, root, root_row, docs, a, sess = setup(env)

    def delete():
        r = env.public.delete(f"/v1/items/{a['id']}", headers=h)
        assert r.status_code == 200, r.text

    env.gw.reconcile(root_row, HookedSession(sess, delete), docs["id"], max_depth=1)
    assert live(env, docs["id"]) == []


def test_rename_during_reconcile_makes_no_phantom(env):
    h, root, root_row, docs, a, sess = setup(env)

    def rename():
        r = env.public.patch(f"/v1/items/{a['id']}", json={"name": "renamed.txt"}, headers=h)
        assert r.status_code == 200, r.text

    env.gw.reconcile(root_row, HookedSession(sess, rename), docs["id"], max_depth=1)
    rows = live(env, docs["id"])
    assert [(r["id"], r["name"]) for r in rows] == [(a["id"], "renamed.txt")]


def test_folder_rename_during_full_rescan_keeps_children(env):
    """Root listed (sees Docs), Docs renamed by a Mac, then the walk can't find
    Docs: its children must not be read as deleted."""
    h, root, root_row, docs, a, sess = setup(env)

    def rename():
        r = env.public.patch(f"/v1/items/{docs['id']}", json={"name": "Docs2"}, headers=h)
        assert r.status_code == 200, r.text

    env.gw.reconcile(root_row, HookedSession(sess, rename))
    assert env.public.get(f"/v1/items/{a['id']}", headers=h).status_code == 200
    assert [r["name"] for r in live(env, root["root_item_id"])] == ["Docs2"]
    # a later pass settles on the truth without churn
    env.gw.reconcile(root_row, sess)
    assert [r["id"] for r in live(env, docs["id"])] == [a["id"]]


def test_mkdir_adopts_folder_a_reconcile_indexed_first(env, monkeypatch):
    """mkdir creates the folder on the NAS, a reconcile indexes it before mkdir's
    own transaction: one item, not two folders with one name."""
    h, root, root_row, docs, a, sess = setup(env)
    real = LocalSession.makedirs

    def makedirs(self, share, path):
        real(self, share, path)
        if path.endswith("/New"):
            env.gw.reconcile(root_row, sess, docs["id"], max_depth=1)
    monkeypatch.setattr(LocalSession, "makedirs", makedirs)
    r = env.public.post("/v1/folders", json={"parent_id": docs["id"], "name": "New"}, headers=h)
    assert r.status_code == 200, r.text
    rows = [x for x in live(env, docs["id"]) if x["name"] == "New"]
    assert len(rows) == 1 and rows[0]["id"] == r.json()["id"]
