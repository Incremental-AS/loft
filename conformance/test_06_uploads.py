"""Chunked upload sessions: create, PUT, status, resume, commit, and their errors."""
import hashlib

from conftest import children, err, put_file

BLOB = bytes((i * 31) % 251 for i in range(50_000))


def _create(dev, data, near):
    r = dev.call("upload_create", json_body={"size": len(data),
                                             "sha256": hashlib.sha256(data).hexdigest(),
                                             "near_id": near})
    assert r.status_code == 200, r.text
    u = r.json()
    assert u["received"] == 0 and u["size"] == len(data) and u["chunk_max"] > 0
    return u["upload_id"]


def _put(dev, uid, off, part, sha=None):
    return dev.call("upload_chunk", path={"upload_id": uid}, params={"offset": off},
                    content=part,
                    headers={"X-Chunk-SHA256": sha or hashlib.sha256(part).hexdigest()})


def test_chunked_upload_with_resume(dev, folder):
    uid = _create(dev, BLOB, folder["id"])
    r = _put(dev, uid, 0, BLOB[:20_000])
    assert r.status_code == 200 and r.json()["received"] == 20_000
    # The client "crashes"; it asks where to resume.
    st = dev.call("upload_status", path={"upload_id": uid}).json()
    assert st == {"upload_id": uid, "received": 20_000, "size": len(BLOB)}
    # A chunk at the wrong offset is refused and says where to go on.
    r = _put(dev, uid, 30_000, BLOB[30_000:40_000])
    assert (r.status_code, err(r)) == (409, "wrong_offset") and r.json()["received"] == 20_000
    # Committing before all bytes are in.
    r = dev.call("upload_commit", path={"upload_id": uid},
                 json_body={"parent_id": folder["id"], "name": "big.bin"})
    assert (r.status_code, err(r)) == (409, "upload_incomplete") and r.json()["received"] == 20_000
    r = _put(dev, uid, 20_000, BLOB[20_000:])
    assert r.status_code == 200 and r.json()["received"] == len(BLOB)
    r = dev.call("upload_commit", path={"upload_id": uid},
                 json_body={"parent_id": folder["id"], "name": "big.bin"})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["result"] == "created" and c["item"]["size"] == len(BLOB)
    assert c["item"]["sha256"] == hashlib.sha256(BLOB).hexdigest()
    assert dev.call("get_content", path={"item_id": c["item"]["id"]}).content == BLOB
    # The session is gone after commit.
    r = dev.call("upload_status", path={"upload_id": uid})
    assert (r.status_code, err(r)) == (404, "no_such_upload")


def test_chunk_errors(dev, folder):
    data = b"0123456789"
    uid = _create(dev, data, folder["id"])
    r = _put(dev, uid, 0, data[:5], sha="0" * 64)
    assert (r.status_code, err(r)) == (422, "chunk_hash_mismatch")
    r = _put(dev, uid, 0, data + b"extra")
    assert (r.status_code, err(r)) == (413, "chunk_too_large")
    assert dev.call("upload_status", path={"upload_id": uid}).json()["received"] == 0


def test_whole_file_hash_is_checked(dev, folder):
    data = b"what I said I'd send"
    r = dev.call("upload_create", json_body={"size": len(data), "sha256": "ab" * 32,
                                             "near_id": folder["id"]})
    uid = r.json()["upload_id"]
    assert _put(dev, uid, 0, data).status_code == 200
    r = dev.call("upload_commit", path={"upload_id": uid},
                 json_body={"parent_id": folder["id"], "name": "liar.txt"})
    assert (r.status_code, err(r)) == (422, "file_hash_mismatch")
    assert "liar.txt" not in children(dev, folder["id"])


def test_bad_upload_requests(dev, folder):
    r = dev.call("upload_create", json_body={"size": 3, "sha256": "not-a-hash"})
    assert (r.status_code, err(r)) == (400, "bad_upload")
    r = dev.call("upload_status", path={"upload_id": "0" * 32})
    assert (r.status_code, err(r)) == (404, "no_such_upload")
    uid = _create(dev, b"x", folder["id"])
    assert _put(dev, uid, 0, b"x").status_code == 200
    r = dev.call("upload_commit", path={"upload_id": uid}, json_body={"parent_id": folder["id"]})
    assert (r.status_code, err(r)) == (400, "parent_and_name_required")
    r = dev.call("upload_commit", path={"upload_id": uid},
                 json_body={"parent_id": folder["id"], "name": "Thumbs.db"})
    assert (r.status_code, err(r)) == (422, "invalid_or_excluded_name")


def test_upload_session_belongs_to_its_device(direct, cfg, dev, folder):
    from conftest import sign_in
    uid = _create(dev, b"mine", folder["id"])
    other = sign_in(direct, cfg, "conformance other uploader")
    other.call("set_root", json_body={"share": dev.call("get_me").json()["root"]["share"],
                                      "path": cfg.root_path})
    r = other.call("upload_status", path={"upload_id": uid})
    assert (r.status_code, err(r)) == (404, "no_such_upload")
    other.call("revoke_device", path={"device_id": other.call("get_me").json()["device_id"]})


def test_small_upload_in_one_chunk(dev, folder):
    c = put_file(dev, folder["id"], "small.txt", b"hello")
    assert c["result"] == "created" and c["replaced_id"] is None
    assert c["item"]["name"] == "small.txt" and c["item"]["content_etag"]
