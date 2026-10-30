"""Roots (shares, folders, choosing a root) and the device list."""
from conftest import err, sign_in


def test_shares_and_folders(direct, cfg, share):
    c = sign_in(direct, cfg, "conformance roots")
    shares = c.call("browse_roots").json()["shares"]
    assert share in shares
    r = c.call("browse_roots", params={"share": share, "path": cfg.root_path})
    assert r.status_code == 200, r.text
    assert r.json()["share"] == share and isinstance(r.json()["folders"], list)
    r = c.call("browse_roots", params={"share": share, "path": "../x"})
    assert (r.status_code, err(r)) == (400, "bad_path")
    r = c.call("browse_roots", params={"share": "loft-no-such-share-x"})
    assert (r.status_code, err(r)) == (404, "no_such_share")
    c.call("revoke_device", path={"device_id": c.call("get_me").json()["device_id"]})


def test_no_root_until_chosen(direct, cfg, share):
    c = sign_in(direct, cfg, "conformance noroot")
    me = c.call("get_me").json()
    assert me["root"] is None
    r = c.call("latest_anchor")
    assert (r.status_code, err(r)) == (409, "no_root_selected")
    r = c.call("set_root", json_body={"share": share, "path": cfg.root_path})
    assert r.status_code == 200, r.text
    root = r.json()["root"]
    assert root["share"] == share and root["root_item_id"] == root["id"] and root["anchor"]
    assert c.call("get_me").json()["root"] == root
    it = c.call("get_item", path={"item_id": root["root_item_id"]}).json()
    assert it["type"] == "dir" and it["parent_id"] is None
    c.call("revoke_device", path={"device_id": me["device_id"]})


def test_all_shares_root(direct, cfg, share):
    c = sign_in(direct, cfg, "conformance allshares")
    r = c.call("set_root", json_body={"share": "*"})
    assert r.status_code == 200, r.text
    root = r.json()["root"]
    assert root["share"] == "*"
    kids = c.call("list_children", path={"item_id": root["root_item_id"]}).json()["items"]
    names = {k["name"]: k for k in kids}
    assert share in names and names[share]["fixed"] is True and names[share]["type"] == "dir"
    # A share can't be renamed or deleted from a device.
    r = c.call("update_item", path={"item_id": names[share]["id"]},
               json_body={"name": share + "-renamed"})
    assert (r.status_code, err(r)) == (403, "share_is_fixed"), r.text
    r = c.call("delete_item", path={"item_id": names[share]["id"]})
    assert (r.status_code, err(r)) == (403, "share_is_fixed")
    c.call("revoke_device", path={"device_id": c.call("get_me").json()["device_id"]})


def test_device_list_and_revoke(direct, cfg, dev):
    other = sign_in(direct, cfg, "conformance other device")
    other_id = other.call("get_me").json()["device_id"]
    r = dev.call("list_devices")
    assert r.status_code == 200
    body = r.json()
    ids = {d["id"]: d for d in body["devices"]}
    assert body["current"] in ids and other_id in ids
    assert ids[other_id]["device_name"] == "conformance other device"
    assert ids[other_id]["revoked_at"] is None
    r = dev.call("revoke_device", path={"device_id": other_id})
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = other.call("get_me")
    assert (r.status_code, err(r)) == (401, "device_revoked")
    r = other.call("refresh")
    assert (r.status_code, err(r)) == (401, "device_revoked")
    ids = {d["id"]: d for d in dev.call("list_devices").json()["devices"]}
    assert ids[other_id]["revoked_at"]
    r = dev.call("revoke_device", path={"device_id": "0" * 32})
    assert (r.status_code, err(r)) == (404, "no_such_device")
