"""Thumbnail worker: turns ONE file into a small JPEG or PNG, in its own sandbox.

Never runs inside the gateway. Image, PDF and video decoders are large C libraries
and a file on the NAS is input anyone with write access to a share controls, so a
decoder bug must not reach the master key, the index or other users' sessions. In
production every request gets a fresh process (deploy/loft-thumb@.service, socket
activated: DynamicUser, no network, no writable files, 512 MB, 40 s). It sees only the
bytes of the one file, and only the ranges it asks the gateway for.

The same sandbox extracts a document's TEXT for the MCP endpoint (read_file): kind
"text", with the document type in "ext" (pdf, docx, pptx, xlsx, odt, odp, ods).

Protocol on fd 0/1 (one stream socket). The gateway writes first:
    {"kind": "image|pdf|video|zip|text", "ext": "heic", "size": N, "px": 256}\\n
then the worker, any number of times:
    R <offset> <length>\\n       the gateway answers  <n>\\n  and n bytes (n < length only at EOF)
and finally one of:
    OK <jpeg|png|text> <n>\\n    and n bytes (text: UTF-8)
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
TEXT_OUT_CAP = 4 << 20           # bytes of UTF-8 text a document may yield
PDF_PAGE_CAP = 2000
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


# ── text (MCP read_file) ─────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _member(z, name: str) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > ZIP_MEMBER_CAP:
        raise NoThumb(f"{name} too large")
    with z.open(info) as m:
        data = m.read(ZIP_MEMBER_CAP + 1)
    if len(data) > ZIP_MEMBER_CAP:
        raise NoThumb(f"{name} too large")
    return data


def _paragraphs(xml: bytes, para: str, text: str, tab: str = "", brk: str = "") -> list[str]:
    """Paragraph texts of an OOXML/ODF part: every `para` element's `text` runs
    (tabs and line breaks kept), in document order. Nested paragraphs (a text box
    inside a paragraph) come out as their own paragraphs."""
    import xml.etree.ElementTree as ET
    out: list[str] = []
    stack: list[list[str]] = []
    for ev, el in ET.iterparse(io.BytesIO(xml), events=("start", "end")):
        t = _local(el.tag)
        if ev == "start":
            if t == para:
                stack.append([])
            continue
        if not stack:
            el.clear()
            continue
        if t == text and el.text:
            stack[-1].append(el.text)
        elif tab and t == tab:
            stack[-1].append("\t")
        elif brk and t == brk:
            stack[-1].append("\n")
        elif t == para:
            out.append("".join(stack.pop()).rstrip())
        if t not in (para, text):
            el.clear()
    return out


def _numbered(names, prefix: str, suffix: str = ".xml") -> list[str]:
    import re
    found = [(int(m.group(1)), n) for n in names
             if (m := re.fullmatch(re.escape(prefix) + r"(\d+)" + re.escape(suffix), n))]
    return [n for _, n in sorted(found)]


def text_pdf(f) -> str:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(f)
    parts = []
    for i in range(min(len(doc), PDF_PAGE_CAP)):
        page = doc[i]
        tp = page.get_textpage()
        parts.append(f"--- page {i + 1} ---\n" + tp.get_text_range().replace("\r\n", "\n").strip())
        if sum(map(len, parts)) > TEXT_OUT_CAP:
            break
    if len(doc) > PDF_PAGE_CAP:
        parts.append(f"[{len(doc) - PDF_PAGE_CAP} more pages not read]")
    return "\n\n".join(parts)


def text_docx(z) -> str:
    out = []
    for part in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
        xml = _member(z, part)
        if xml:
            out += _paragraphs(xml, "p", "t", tab="tab", brk="br")
    return "\n".join(out)


def text_pptx(z) -> str:
    names = z.namelist()
    out = []
    for i, slide in enumerate(_numbered(names, "ppt/slides/slide"), 1):
        lines = [p for p in _paragraphs(_member(z, slide) or b"", "p", "t", brk="br") if p]
        notes_name = f"ppt/notesSlides/notesSlide{slide.rsplit('slide', 1)[1]}"
        block = f"--- slide {i} ---\n" + "\n".join(lines)
        notes = _member(z, notes_name)
        if notes:
            # Notes pages repeat the slide number as a field; keep only real text.
            n = [p for p in _paragraphs(notes, "p", "t", brk="br") if p and not p.isdigit()]
            if n:
                block += "\nNotes: " + "\n".join(n)
        out.append(block)
    return "\n\n".join(out)


def text_xlsx(z) -> str:
    import re
    import xml.etree.ElementTree as ET
    shared: list[str] = []
    sst = _member(z, "xl/sharedStrings.xml")
    if sst:
        for si in ET.fromstring(sst):
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    # Sheet names in workbook order, via the workbook's relationships.
    rels = {}
    rx = _member(z, "xl/_rels/workbook.xml.rels")
    if rx:
        for r in ET.fromstring(rx):
            rels[r.get("Id")] = "xl/" + r.get("Target", "").lstrip("/").removeprefix("xl/")
    sheets = []
    wb = _member(z, "xl/workbook.xml")
    if wb:
        for el in ET.fromstring(wb).iter():
            if _local(el.tag) == "sheet":
                rid = next((v for k, v in el.attrib.items() if _local(k) == "id"), None)
                if rid in rels:
                    sheets.append((el.get("name") or rels[rid], rels[rid]))
    if not sheets:
        sheets = [(n.rsplit("/", 1)[1], n) for n in _numbered(z.namelist(), "xl/worksheets/sheet")]
    out = []
    for name, part in sheets:
        xml = _member(z, part)
        if not xml:
            continue
        rows = []
        for row in ET.fromstring(xml).iter():
            if _local(row.tag) != "row":
                continue
            cells = []
            for c in row:
                if _local(c.tag) != "c":
                    continue
                kind, v = c.get("t"), None
                for sub in c:
                    if _local(sub.tag) == "v":
                        v = sub.text
                    elif _local(sub.tag) == "is":
                        v = "".join(t.text or "" for t in sub.iter() if _local(t.tag) == "t")
                if kind == "s" and v is not None and v.isdigit() and int(v) < len(shared):
                    v = shared[int(v)]
                col = re.match(r"[A-Z]+", c.get("r") or "")
                if col:      # keep empty cells in place: column letters -> index
                    idx = 0
                    for ch in col.group(0):
                        idx = idx * 26 + ord(ch) - 64
                    cells += [""] * (idx - 1 - len(cells))
                cells.append((v or "").replace("\t", " ").replace("\n", " "))
            if any(cells):
                rows.append("\t".join(cells).rstrip("\t"))
        out.append(f"--- sheet {name} ---\n" + "\n".join(rows))
    return "\n\n".join(out)


def _attr(el, name: str) -> str:
    return next((v for k, v in el.attrib.items() if _local(k) == name), "")


def _odf_text(el) -> str:
    """Text of an ODF element: <text:s c="n"/> spaces, <text:tab/>, <text:line-break/>."""
    out = [el.text or ""]
    for c in el:
        t = _local(c.tag)
        if t == "s":
            out.append(" " * int(_attr(c, "c") or 1))
        elif t == "tab":
            out.append("\t")
        elif t == "line-break":
            out.append("\n")
        elif t not in ("p", "h"):          # a nested paragraph is listed on its own
            out.append(_odf_text(c))
        out.append(c.tail or "")
    return "".join(out)


def text_odf(z, ext: str) -> str:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_member(z, "content.xml") or b"<none/>")
    if ext != "ods":
        return "\n".join(_odf_text(el).rstrip() for el in root.iter() if _local(el.tag) in ("p", "h"))
    out = []
    for table in root.iter():
        if _local(table.tag) != "table":
            continue
        rows = []
        for row in table.iter():
            if _local(row.tag) != "table-row":
                continue
            cells = [" ".join(_odf_text(p) for p in cell if _local(p.tag) == "p")
                     for cell in row if _local(cell.tag) == "table-cell"]
            if any(cells):
                rows.append("\t".join(c.replace("\t", " ") for c in cells).rstrip("\t"))
        out.append(f"--- sheet {_attr(table, 'name')} ---\n" + "\n".join(rows))
    return "\n\n".join(out)


def extract_text(f, ext: str) -> str:
    if ext == "pdf":
        return text_pdf(f)
    import zipfile
    try:
        z = zipfile.ZipFile(f)
    except zipfile.BadZipFile:
        raise NoThumb("not a zip")
    if ext == "docx":
        return text_docx(z)
    if ext == "pptx":
        return text_pptx(z)
    if ext == "xlsx":
        return text_xlsx(z)
    if ext in ("odt", "odp", "ods"):
        return text_odf(z, ext)
    raise NoThumb("no text extractor")


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
    if kind == "text":
        try:
            data = extract_text(f, ext).strip().encode("utf-8")
            if not data:
                raise NoThumb("no text")
            if len(data) > TEXT_OUT_CAP:     # cut on a character boundary
                data = data[:TEXT_OUT_CAP].decode("utf-8", "ignore").encode("utf-8")
        except NoThumb as e:
            wf.write(f"NO {e}\n".encode())
        except Exception as e:  # noqa: BLE001 - any parser failure is "no text"
            wf.write(f"NO {type(e).__name__}\n".encode())
        else:
            wf.write(f"OK text {len(data)}\n".encode() + data)
        wf.flush()
        return
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
