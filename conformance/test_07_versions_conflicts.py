"""Modify, versions + restore, safe-save and conflict copies."""
import re

from conftest import children, put_file

CONFLICT = re.compile(r"^report \(conflict, .+, \d{4}-\d\d-\d\d \d{4}\)\.txt$")


def test_modify_keeps_a_version_and_restore(dev, folder):
    v1 = put_file(dev, folder["id"], "doc.txt", b"version one")["item"]
    r = put_file(dev, None, None, b"version two", item_id=v1["id"], if_match=v1["content_etag"])
    assert r["result"] == "modified" and r["item"]["id"] == v1["id"]
    v2 = r["item"]
    assert v2["content_etag"] != v1["content_etag"] and v2["meta_etag"] == v1["meta_etag"]
    assert dev.call("get_content", path={"item_id": v1["id"]}).content == b"version two"
    vs = dev.call("list_versions", path={"item_id": v1["id"]}).json()["versions"]
    assert len(vs) == 1 and vs[0]["size"] == len(b"version one")
    r = dev.call("restore_version", path={"item_id": v1["id"], "version_id": vs[0]["id"]})
    assert r.status_code == 200, r.text
    v3 = r.json()
    assert v3["id"] == v1["id"] and v3["content_etag"] not in (v1["content_etag"],
                                                               v2["content_etag"])
    assert dev.call("get_content", path={"item_id": v1["id"]}).content == b"version one"
    vs = dev.call("list_versions", path={"item_id": v1["id"]}).json()["versions"]
    assert [v["size"] for v in vs] == [len(b"version two")]       # nothing lost
    r = dev.call("restore_version", path={"item_id": v1["id"], "version_id": "0" * 32})
    assert (r.status_code, r.json()["error"]) == (404, "no_such_version")


def test_stale_modify_becomes_a_conflict_copy(dev, folder):
    a = put_file(dev, folder["id"], "report.txt", b"base")["item"]
    b = put_file(dev, None, None, b"edit from device B", item_id=a["id"],
                 if_match=a["content_etag"])["item"]
    # Device A still thinks the file is at `a`'s etag.
    r = put_file(dev, None, None, b"edit from device A", item_id=a["id"],
                 if_match=a["content_etag"])
    assert r["result"] == "conflict"
    assert r["item"]["id"] != a["id"] and CONFLICT.match(r["item"]["name"]), r["item"]["name"]
    # The original is untouched.
    assert dev.call("get_content", path={"item_id": a["id"]}).content == b"edit from device B"
    assert dev.call("get_item", path={"item_id": a["id"]}).json()["content_etag"] == \
        b["content_etag"]
    assert dev.call("get_content", path={"item_id": r["item"]["id"]}).content == \
        b"edit from device A"


def test_create_over_existing_name_is_a_conflict(dev, folder):
    put_file(dev, folder["id"], "report.txt", b"first")
    r = put_file(dev, folder["id"], "report.txt", b"second")
    assert r["result"] == "conflict" and CONFLICT.match(r["item"]["name"])
    kids = children(dev, folder["id"])
    assert len(kids) == 2
    assert dev.call("get_content", path={"item_id": kids["report.txt"]["id"]}).content == b"first"


def test_safe_save_replaces(dev, folder):
    """Apps that save by writing a new file and swapping it in: one item, one version."""
    old = put_file(dev, folder["id"], "safe.txt", b"old")["item"]
    r = put_file(dev, folder["id"], "safe.txt", b"new", replaces=old["id"])
    assert r["result"] == "replaced" and r["replaced_id"] == old["id"]
    new = r["item"]
    assert dev.call("get_content", path={"item_id": new["id"]}).content == b"new"
    vs = dev.call("list_versions", path={"item_id": new["id"]}).json()["versions"]
    assert len(vs) == 1 and vs[0]["size"] == 3
    d = dev.call("delete_item", path={"item_id": old["id"]}).json()
    assert d["result"] == "superseded" and d["superseded_by"] == new["id"]
