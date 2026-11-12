"""Content search (GET /v1/search): a saved document is found by a word inside it, the
answer matches the published schema, and a file in the trash is no longer found."""
import time
import uuid

import pytest

from conftest import err, put_file


def _search(c, q, **params):
    r = c.call("search_content", params={"q": q, **params})
    if r.status_code == 404 and err(r) == "search_disabled":
        pytest.skip("content search is switched off on this gateway")
    assert r.status_code == 200, r.text
    return r.json()


def _until_found(c, q, want: str, wait_s: float = 30):
    end = time.monotonic() + wait_s
    while True:
        res = _search(c, q)
        if any(i["id"] == want for i in res["items"]):
            return res
        assert time.monotonic() < end, f"{q!r} did not find {want} within {wait_s} s: {res}"
        time.sleep(0.5)


def test_found_by_a_word_inside(dev, folder):
    word = "zq" + uuid.uuid4().hex[:10]           # a word no other file has
    it = put_file(dev, folder["id"], "notes.md", f"# Notes\n\nThe {word} plans.\n".encode())["item"]
    res = _until_found(dev, word, it["id"])
    hit = next(i for i in res["items"] if i["id"] == it["id"])
    assert hit["name"] == "notes.md" and hit["type"] == "file"
    assert hit["snippet"] is None or word in hit["snippet"]
    assert _search(dev, f"{word} absentword{uuid.uuid4().hex[:6]}")["items"] == []
    r = dev.call("delete_item", path={"item_id": it["id"]})
    assert r.status_code == 200, r.text
    assert all(i["id"] != it["id"] for i in _search(dev, word)["items"])


def test_bad_queries(dev):
    r = dev.call("search_content", params={"q": "!!!"})
    if err(r) == "search_disabled":
        pytest.skip("content search is switched off on this gateway")
    assert (r.status_code, err(r)) == (400, "bad_query")
    r = dev.call("search_content", params={"q": "word", "cursor": "not-a-cursor"})
    assert (r.status_code, err(r)) == (400, "bad_cursor")
