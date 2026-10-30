import io
import socket
import threading
import zipfile

import pytest
from PIL import Image

from loft_gateway import thumbs
from loft_gateway.thumbs import Thumbnailer

from conftest import child, rooted


def _jpeg(w, h, orientation=None):
    im = Image.new("RGB", (w, h), (200, 30, 30))
    out = io.BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    im.save(out, "JPEG", exif=exif)
    return out.getvalue()


def _mp4(w, h, rotation=0):
    import av
    out = io.BytesIO()
    with av.open(out, "w", format="mp4") as c:
        s = c.add_stream("mpeg4", rate=10)
        s.width, s.height, s.pix_fmt = w, h, "yuv420p"
        if rotation:
            s.set_display_rotation(rotation)
        for i in range(30):
            f = av.VideoFrame.from_image(Image.new("RGB", (w, h), (0, 8 * i, 200)))
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
    return out.getvalue()


@pytest.fixture
def files(env):
    d = env.nas / "home" / "alice"
    (d / "photo.JPG").write_bytes(_jpeg(2000, 1000, orientation=6))     # EXIF: rotate 90
    logo = io.BytesIO()
    Image.new("RGBA", (300, 300), (0, 0, 255, 128)).save(logo, "PNG")
    (d / "logo.png").write_bytes(logo.getvalue())
    pdf = io.BytesIO()
    Image.new("RGB", (600, 800), "white").save(pdf, "PDF")
    (d / "doc.pdf").write_bytes(pdf.getvalue())
    (d / "clip.mp4").write_bytes(_mp4(320, 180, rotation=90))         # an iPhone portrait clip
    key = io.BytesIO()
    with zipfile.ZipFile(key, "w") as z:
        z.writestr("Index/Document.iwa", b"x" * 100)
        z.writestr("preview.jpg", _jpeg(400, 300))
    (d / "Deck.key").write_bytes(key.getvalue())
    (d / "notes.txt").write_bytes(b"hello")
    (d / "broken.jpg").write_bytes(b"\xff\xd8\xff not really a jpeg" * 50)
    h, root = rooted(env)
    ids = {n: child(env.public, h, root["root_item_id"], n)["id"]
           for n in ("photo.JPG", "logo.png", "doc.pdf", "clip.mp4", "Deck.key", "notes.txt",
                     "broken.jpg")}
    return h, ids


def _thumb(env, h, iid, px=256):
    return env.public.get(f"/v1/items/{iid}/thumbnail", params={"px": px}, headers=h)


def test_thumbnails_by_kind(env, files):
    h, ids = files
    want = {"photo.JPG": ("jpeg", "portrait"), "logo.png": ("png", "square"),
            "doc.pdf": ("jpeg", "portrait"), "clip.mp4": ("jpeg", "portrait"),
            "Deck.key": ("jpeg", "landscape")}
    for name, (fmt, shape) in want.items():
        r = _thumb(env, h, ids[name])
        assert r.status_code == 200, (name, r.text)
        assert r.headers["content-type"] == f"image/{fmt}", name
        assert r.headers["etag"].startswith('"c')
        im = Image.open(io.BytesIO(r.content))
        assert max(im.size) <= 256, (name, im.size)
        got = "square" if im.width == im.height else "portrait" if im.height > im.width else "landscape"
        assert got == shape, (name, im.size)
    assert Image.open(io.BytesIO(_thumb(env, h, ids["photo.JPG"], px=100).content)).size == (64, 128)


def test_no_thumbnail(env, files):
    h, ids = files
    for name in ("notes.txt", "broken.jpg"):
        r = _thumb(env, h, ids[name])
        assert r.status_code == 404 and r.json()["error"] == "no_thumbnail", name


def test_thumbnail_needs_access(env, files):
    _, ids = files
    hb, _ = rooted(env, user="bob", path="bob", name="Bob's Mac")
    r = _thumb(env, hb, ids["photo.JPG"])                  # alice's item, bob's token
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert env.public.get(f"/v1/items/{ids['photo.JPG']}/thumbnail").status_code == 401


def test_thumbnail_cache_and_version(env, files, monkeypatch):
    h, ids = files
    runs = []
    real = Thumbnailer._run
    monkeypatch.setattr(Thumbnailer, "_run", lambda self, *a: runs.append(a[0]) or real(self, *a))
    assert _thumb(env, h, ids["logo.png"]).status_code == 200
    assert _thumb(env, h, ids["logo.png"], px=200).status_code == 200      # same 256 bucket
    assert _thumb(env, h, ids["broken.jpg"]).status_code == 404
    assert _thumb(env, h, ids["broken.jpg"]).status_code == 404            # "no" is cached too
    assert len(runs) == 2
    im = io.BytesIO()
    Image.new("RGB", (50, 50), "green").save(im, "PNG")
    (env.nas / "home" / "alice" / "logo.png").write_bytes(im.getvalue())   # changed on the NAS
    r = _thumb(env, h, ids["logo.png"])
    assert r.status_code == 200 and len(runs) == 3
    px = Image.open(io.BytesIO(r.content)).getpixel((0, 0))[:3]
    assert px[1] > 100 and px[0] < 20 and px[2] < 20                       # green, JPEG-rounded


def test_read_budget(env, files, monkeypatch):
    h, ids = files
    monkeypatch.setitem(thumbs.READ_CAP, "video", 1000)
    assert _thumb(env, h, ids["clip.mp4"]).status_code == 404
    monkeypatch.setitem(thumbs.READ_CAP, "video", 128 << 20)
    assert _thumb(env, h, ids["clip.mp4"]).status_code == 200              # failure not cached


@pytest.mark.parametrize("lines", [
    [b"R -1 10\n"], [b"R 0 999999999\n"], [b"R 500 10\n"], [b"OK jpeg 99999999\n"],
    [b"OK gif 3\nabc"], [b"garbage\n"], [b"R 0 4\n", b"OK png 10\nshort"],
])
def test_hostile_worker(lines, monkeypatch):
    """A worker that a decoder bug took over gets nothing outside its one file."""
    reads = []

    def fake_connect(self):
        a, b = socket.socketpair()
        a.settimeout(2)

        def worker():
            with b:
                b.makefile("rb").readline()
                for ln in lines:
                    b.sendall(ln)
                    if ln.startswith(b"R 0 4"):
                        b.recv(16)
        threading.Thread(target=worker, daemon=True).start()
        return a, None

    monkeypatch.setattr(Thumbnailer, "_connect", fake_connect)
    t = Thumbnailer("")
    r = t.get(("item", 1), "x.png", 100, 256, lambda off, n: reads.append((off, n)) or [b"z" * n])
    assert r is None
    assert all(0 <= off and off + n <= 100 for off, n in reads)
