"""Thumbnail worker: turns ONE file into a small JPEG or PNG, in its own sandbox.

Never runs inside the gateway. Image, PDF and video decoders are large C libraries
and a file on the NAS is input anyone with write access to a share controls, so a
decoder bug must not reach the master key, the index or other users' sessions. In
production every request gets a fresh process (deploy/loft-thumb@.service, socket
activated: DynamicUser, no network, no writable files, 512 MB, 40 s). It sees only the
bytes of the one file, and only the ranges it asks the gateway for.

Protocol on fd 0/1 (one stream socket). The gateway writes first:
    {"kind": "image|pdf|video|zip", "ext": "heic", "size": N, "px": 256}\\n
then the worker, any number of times:
    R <offset> <length>\\n       the gateway answers  <n>\\n  and n bytes (n < length only at EOF)
and finally one of:
    OK <jpeg|png> <n>\\n         and n bytes
    NO <reason>\\n

    python -I -m loft_gateway.thumbworker      (stdin and stdout are the socket)
"""
from __future__ import annotations

import io
import json
import socket
import sys
import warnings

MAX_REQ = 8 << 20                # per range request; the gateway enforces it too
PIXEL_CAP = 150_000_000          # Pillow's decompression-bomb limit
ZIP_MEMBER_CAP = 32 << 20
# Embedded previews, best first: iWork (Pages/Numbers/Keynote), OOXML (Word/Excel/
# PowerPoint saved with a preview picture), ODF.
ZIP_PREVIEWS = ("preview.jpg", "QuickLook/Thumbnail.jpg", "QuickLook/Preview.jpg",
                "preview-web.jpg", "docProps/thumbnail.jpeg", "docProps/thumbnail.jpg",
                "docProps/thumbnail.png", "Thumbnails/thumbnail.png")


class NoThumb(Exception):
    pass


class Remote(io.RawIOBase):
    """The file, read on demand from the gateway."""

    def __init__(self, rf, wf, size: int):
        self.rf, self.wf, self.size, self.pos = rf, wf, size, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(base + off, 0)
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos, MAX_REQ)
        if n <= 0:
            return 0
        self.wf.write(f"R {self.pos} {n}\n".encode())
        self.wf.flush()
        got = int(self.rf.readline(32))
        data = self.rf.read(got) if got else b""
        if len(data) != got or got > n:
            raise NoThumb("short read")
        b[:got] = data
        self.pos += got
        return got


def _pil():
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = PIXEL_CAP
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    return Image


def image(f, ext: str, px: int):
    Image = _pil()
    from PIL import ImageOps
    if ext in ("heic", "heif", "hif"):
        import pillow_heif
        pillow_heif.register_heif_opener()
    im = Image.open(f)
    im.draft("RGB", (px, px))           # JPEG: decode at 1/2 … 1/8 scale, much faster
    return ImageOps.exif_transpose(im)


def pdf(f, px: int):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(f)
    if len(doc) == 0:
        raise NoThumb("empty pdf")
    page = doc[0]
    w, h = page.get_size()
    if w <= 0 or h <= 0:
        raise NoThumb("bad page size")
    return page.render(scale=min(px / max(w, h), 4.0)).to_pil()


def video(f, px: int):
    import av
    with av.open(f, mode="r") as c:
        vs = next((s for s in c.streams if s.type == "video"), None)
        if vs is None:
            raise NoThumb("no video stream")
        vs.thread_type = "SLICE"
        vs.codec_context.thread_count = 1
        # Skip a black first frame: 10 % in, at most 10 s (seek lands on a keyframe).
        if c.duration and c.duration > 2_000_000:
            try:
                c.seek(min(c.duration // 10, 10_000_000))
            except av.FFmpegError:
                c.seek(0)
        for frame in c.decode(vs):
            im = frame.to_image()
            if frame.rotation:                    # counter-clockwise, as PIL rotates
                im = im.rotate(frame.rotation, expand=True)
            return im
    raise NoThumb("no frame")


def zipped(f, px: int):
    import zipfile
    try:
        z = zipfile.ZipFile(f)
    except zipfile.BadZipFile:
        raise NoThumb("not a zip")
    names = {n: n for n in z.namelist()}
    for want in ZIP_PREVIEWS:
        if want in names:
            info = z.getinfo(want)
            if info.file_size > ZIP_MEMBER_CAP:
                continue
            with z.open(info) as m:
                data = m.read(ZIP_MEMBER_CAP + 1)
            if len(data) > ZIP_MEMBER_CAP:
                continue
            return image(io.BytesIO(data), want.rsplit(".", 1)[-1].lower(), px)
    raise NoThumb("no embedded preview")


def encode(im, px: int) -> tuple[str, bytes]:
    Image = _pil()
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    if im.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
        im = im.point(lambda v: v / 256).convert("L")
    im.thumbnail((px, px), Image.Resampling.LANCZOS, reducing_gap=3.0)
    alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    out = io.BytesIO()
    if alpha:
        im.convert("RGBA").save(out, "PNG")
        return "png", out.getvalue()
    im.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
    return "jpeg", out.getvalue()


def run(rf, wf):
    req = json.loads(rf.readline(4096))
    kind, ext, px = req["kind"], req["ext"], int(req["px"])
    f = io.BufferedReader(Remote(rf, wf, int(req["size"])), buffer_size=1 << 20)
    try:
        if kind == "image":
            im = image(f, ext, px)
        elif kind == "pdf":
            im = pdf(f, px)
        elif kind == "video":
            im = video(f, px)
        elif kind == "zip":
            im = zipped(f, px)
        else:
            raise NoThumb("unknown kind")
        fmt, data = encode(im, px)
    except NoThumb as e:
        wf.write(f"NO {e}\n".encode())
    except Exception as e:  # noqa: BLE001 - any decoder failure is "no thumbnail"
        wf.write(f"NO {type(e).__name__}\n".encode())
    else:
        wf.write(f"OK {fmt} {len(data)}\n".encode() + data)
    wf.flush()


def main():
    sock = socket.socket(fileno=0)
    run(sock.makefile("rb"), sock.makefile("wb"))


if __name__ == "__main__":
    sys.exit(main())
