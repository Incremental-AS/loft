"""The change feed: latest anchor, changes since, long-poll, expired anchors."""
import threading
import time

from conftest import err, put_file


def _drain(dev, anchor):
    """Follow the feed to its end; return (updated by id, deleted ids, final anchor)."""
    updated, deleted = {}, set()
    while True:
        r = dev.call("list_changes", params={"anchor": anchor})
        assert r.status_code == 200, r.text
        page = r.json()
        for it in page["updated"]:
            updated[it["id"]] = it
            deleted.discard(it["id"])
        for i in page["deleted"]:
            deleted.add(i)
            updated.pop(i, None)
        anchor = page["anchor"]
        if not page["more"]:
            return updated, deleted, anchor


def test_changes_since_anchor(dev, folder):
    a0 = dev.call("latest_anchor").json()["anchor"]
    f = put_file(dev, folder["id"], "feed.txt", b"1")["item"]
    d = dev.call("create_folder", json_body={"parent_id": folder["id"], "name": "feed-dir"}).json()
    up, gone, a1 = _drain(dev, a0)
    assert f["id"] in up and d["id"] in up and up[f["id"]]["name"] == "feed.txt"
    assert a1 != a0
    dev.call("update_item", path={"item_id": f["id"]}, json_body={"name": "feed2.txt"})
    dev.call("delete_item", path={"item_id": d["id"]})
    up, gone, a2 = _drain(dev, a1)
    assert up[f["id"]]["name"] == "feed2.txt" and d["id"] in gone
    # Nothing new: same anchor back, empty page.
    page = dev.call("list_changes", params={"anchor": a2}).json()
    assert page == {"updated": [], "deleted": [], "anchor": a2, "more": False}
    assert dev.call("latest_anchor").json()["anchor"] == a2


def test_paging_with_limit(dev, folder):
    a0 = dev.call("latest_anchor").json()["anchor"]
    for i in range(3):
        put_file(dev, folder["id"], f"p{i}.txt", b"p")
    page = dev.call("list_changes", params={"anchor": a0, "limit": 1}).json()
    assert page["more"] is True and len(page["updated"]) + len(page["deleted"]) == 1


def test_long_poll_wakes_on_change(dev, folder):
    a0 = dev.call("latest_anchor").json()["anchor"]
    out = {}

    def poll():
        t = time.monotonic()
        out["r"] = dev.call("list_changes", params={"anchor": a0, "wait": 20})
        out["t"] = time.monotonic() - t

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(1.0)
    dev.call("create_folder", json_body={"parent_id": folder["id"], "name": "wake"})
    th.join(30)
    assert out["r"].status_code == 200
    assert any(i["name"] == "wake" for i in out["r"].json()["updated"])
    assert out["t"] < 15, f"long-poll took {out['t']:.1f}s to see the change"


def test_long_poll_times_out_empty(dev):
    a0 = dev.call("latest_anchor").json()["anchor"]
    t = time.monotonic()
    page = dev.call("list_changes", params={"anchor": a0, "wait": 2}).json()
    assert time.monotonic() - t >= 1.5
    assert page["updated"] == [] and page["anchor"] == a0


def test_bad_and_expired_anchors(dev):
    r = dev.call("list_changes", params={"anchor": "nonsense"})
    assert (r.status_code, err(r)) == (400, "bad_anchor")
    r = dev.call("list_changes", params={"anchor": "000000000000.0"})
    assert (r.status_code, err(r)) == (410, "anchor_expired")
