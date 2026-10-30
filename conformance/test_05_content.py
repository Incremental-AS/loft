"""Downloads (whole, Range) and thumbnails."""
import struct
import zlib

from conftest import err, put_file

DATA = bytes(range(256)) * 40          # 10240 bytes


def _png(w=48, h=32) -> bytes:
    """A small valid RGB PNG, written by hand (no imaging library needed)."""
    raw = b"".join(b"\x00" + b"".join(bytes((x * 5 % 256, y * 7 % 256, 128)) for x in range(w))
                   for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_download_whole_and_ranges(dev, folder):
    it = put_file(dev, folder["id"], "data.bin", DATA, chunk=4096)["item"]
    assert it["size"] == len(DATA)
    r = dev.call("get_content", path={"item_id": it["id"]})
    assert r.status_code == 200 and r.content == DATA
    assert r.headers["ETag"] == f'"{it["content_etag"]}"'
    assert r.headers["Accept-Ranges"] == "bytes"
    for rng, (a, b) in {"bytes=100-199": (100, 199), "bytes=10000-": (10000, len(DATA) - 1),
                        "bytes=-40": (len(DATA) - 40, len(DATA) - 1),
                        "bytes=10200-99999": (10200, len(DATA) - 1)}.items():
        r = dev.call("get_content", path={"item_id": it["id"]}, headers={"Range": rng})
        assert r.status_code == 206, rng
        assert r.content == DATA[a:b + 1], rng
        assert r.headers["Content-Range"] == f"bytes {a}-{b}/{len(DATA)}", rng
        assert int(r.headers["Content-Length"]) == b - a + 1
    r = dev.call("get_content", path={"item_id": it["id"]}, headers={"Range": "bytes=20000-"})
    assert (r.status_code, err(r)) == (416, "range_not_satisfiable")
    assert r.headers["Content-Range"] == f"bytes */{len(DATA)}"
    r = dev.call("get_content", path={"item_id": it["id"]}, headers={"Range": "lines=1-2"})
    assert (r.status_code, err(r)) == (416, "bad_range")


def test_download_empty_file_and_folder(dev, folder):
    it = put_file(dev, folder["id"], "empty.txt", b"")["item"]
    r = dev.call("get_content", path={"item_id": it["id"]})
    assert r.status_code == 200 and r.content == b""
    r = dev.call("get_content", path={"item_id": folder["id"]})
    assert (r.status_code, err(r)) == (400, "is_a_folder")


def test_thumbnail(dev, folder):
    png = put_file(dev, folder["id"], "pic.png", _png())["item"]
    r = dev.call("get_thumbnail", path={"item_id": png["id"]}, params={"px": 64})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] in ("image/jpeg", "image/png")
    assert r.content[:3] == b"\xff\xd8\xff" or r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert r.headers["ETag"] == f'"{png["content_etag"]}"'
    txt = put_file(dev, folder["id"], "notes.txt", b"no picture here")["item"]
    r = dev.call("get_thumbnail", path={"item_id": txt["id"]})
    assert (r.status_code, err(r)) == (404, "no_thumbnail")
