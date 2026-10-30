"""Folders, listing, rename/move, delete + trash."""
from conftest import children, err, put_file


def test_children_and_paging(dev, folder):
    for n in ("b.txt", "a.txt", "c.txt"):
        put_file(dev, folder["id"], n, n.encode())
    r = dev.call("create_folder", json_body={"parent_id": folder["id"], "name": "sub"})
    assert r.status_code == 200
    seen, cursor = [], None
    while True:
        page = dev.call("list_children", path={"item_id": folder["id"]},
                        params={"limit": 1, **({"cursor": cursor} if cursor else {})}).json()
        assert len(page["items"]) <= 1
        seen += [i["name"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == ["a.txt", "b.txt", "c.txt", "sub"] and len(seen) == 4
    kids = children(dev, folder["id"])
    assert kids["a.txt"]["type"] == "file" and kids["a.txt"]["size"] == 5
    assert kids["a.txt"]["parent_id"] == folder["id"] and kids["sub"]["type"] == "dir"
    r = dev.call("list_children", path={"item_id": kids["a.txt"]["id"]})
    assert (r.status_code, err(r)) == (400, "not_a_folder")
    r = dev.call("get_item", path={"item_id": "0" * 32})
    assert (r.status_code, err(r)) == (404, "not_found")


def test_create_folder(dev, folder):
    r = dev.call("create_folder", json_body={"parent_id": folder["id"], "name": "Ny mappe ÆØÅ"})
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["type"] == "dir" and f["name"] == "Ny mappe ÆØÅ" and f["parent_id"] == folder["id"]
    assert dev.call("get_item", path={"item_id": f["id"]}).json()["id"] == f["id"]
    r = dev.call("create_folder", json_body={"parent_id": folder["id"], "name": "ny mappe æøå"})
    assert (r.status_code, err(r)) == (409, "name_exists")
    assert r.json()["existing_id"] == f["id"]
    for bad in (".DS_Store", "a/b", "..", "~$lock.docx"):
        r = dev.call("create_folder", json_body={"parent_id": folder["id"], "name": bad})
        assert (r.status_code, err(r)) == (422, "invalid_or_excluded_name"), bad


def test_rename_and_move(dev, folder):
    a = put_file(dev, folder["id"], "old.txt", b"x")["item"]
    dst = dev.call("create_folder", json_body={"parent_id": folder["id"], "name": "dst"}).json()
    r = dev.call("update_item", path={"item_id": a["id"]},
                 json_body={"name": "new.txt", "if_match": a["meta_etag"]})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["id"] == a["id"] and b["name"] == "new.txt" and b["meta_etag"] != a["meta_etag"]
    assert b["content_etag"] == a["content_etag"]
    # A stale meta etag: 412 with the current item.
    r = dev.call("update_item", path={"item_id": a["id"]},
                 json_body={"name": "other.txt", "if_match": a["meta_etag"]})
    assert (r.status_code, err(r)) == (412, "meta_etag_mismatch")
    assert r.json()["current"]["name"] == "new.txt"
    # Move into another folder (same id).
    r = dev.call("update_item", path={"item_id": a["id"]}, json_body={"parent_id": dst["id"]})
    assert r.status_code == 200 and r.json()["parent_id"] == dst["id"]
    assert "new.txt" in children(dev, dst["id"]) and "new.txt" not in children(dev, folder["id"])
    # Name taken in the destination.
    put_file(dev, folder["id"], "taken.txt", b"t")
    r = dev.call("update_item", path={"item_id": a["id"]},
                 json_body={"parent_id": folder["id"], "name": "taken.txt"})
    assert (r.status_code, err(r)) == (409, "name_exists")
    # A folder can't go inside itself.
    inner = dev.call("create_folder", json_body={"parent_id": dst["id"], "name": "in"}).json()
    r = dev.call("update_item", path={"item_id": dst["id"]}, json_body={"parent_id": inner["id"]})
    assert (r.status_code, err(r)) == (400, "move_into_self")
    # Rename to an excluded name.
    r = dev.call("update_item", path={"item_id": a["id"]}, json_body={"name": ".DS_Store"})
    assert (r.status_code, err(r)) == (422, "invalid_or_excluded_name")


def test_root_cannot_move_or_go(dev, session_dev):
    rid = session_dev.root["root_item_id"]
    r = dev.call("update_item", path={"item_id": rid}, json_body={"name": "x"})
    assert (r.status_code, err(r)) == (400, "cannot_move_root")
    r = dev.call("delete_item", path={"item_id": rid})
    assert (r.status_code, err(r)) == (400, "cannot_delete_root")


def test_delete_goes_to_trash(dev, folder):
    f = put_file(dev, folder["id"], "gone.txt", b"bye")["item"]
    d = dev.call("create_folder", json_body={"parent_id": folder["id"], "name": "gone"}).json()
    put_file(dev, d["id"], "inside.txt", b"in")
    # Stale content etag: refused, nothing deleted.
    r = dev.call("delete_item", path={"item_id": f["id"]}, params={"if_match": "c999999"})
    assert (r.status_code, err(r)) == (412, "content_etag_mismatch")
    assert r.json()["current"]["id"] == f["id"]
    r = dev.call("delete_item", path={"item_id": f["id"]}, params={"if_match": f["content_etag"]})
    assert r.status_code == 200 and r.json()["result"] == "trashed" and r.json()["trash_id"]
    r = dev.call("delete_item", path={"item_id": d["id"]})
    assert r.status_code == 200 and r.json()["result"] == "trashed"
    kids = children(dev, folder["id"])
    assert "gone.txt" not in kids and "gone" not in kids
    r = dev.call("get_item", path={"item_id": f["id"]})
    assert (r.status_code, err(r)) == (404, "not_found")
    # Idempotent.
    r = dev.call("delete_item", path={"item_id": f["id"]})
    assert r.status_code == 200 and r.json()["result"] == "already_deleted"
    # The name is free again.
    assert put_file(dev, folder["id"], "gone.txt", b"again")["result"] in ("created", "replaced")
