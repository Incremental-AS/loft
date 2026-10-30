"""Thumbnails for Finder (GET /v1/items/{id}/thumbnail).

The gateway never decodes a file itself: it hands the bytes to a sandboxed worker
(thumbworker.py), one fresh process per file, and answers its range requests from the
caller's own SMB session, so the worker can read only that one file and only what it
asks for, within a per-kind byte budget. Results are kept in a small in-memory LRU
keyed by (item id, content version, size); item ids belong to one root, so one user
never gets another's cached thumbnail.

Production: settings.thumb_socket = /run/loft-thumb.sock (loft-thumb.socket, Accept=yes).
Tests and dev: thumb_socket = "" spawns `python -I -m loft_gateway.thumbworker`.
"""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from typing import Callable, Iterator

log = logging.getLogger("loft.thumbs")

KINDS = {
    **dict.fromkeys(("jpg", "jpeg", "jpe", "png", "gif", "bmp", "tif", "tiff", "webp",
                     "heic", "heif", "hif", "avif", "psd", "ico"), "image"),
    **dict.fromkeys(("pdf", "ai"), "pdf"),
    **dict.fromkeys(("mp4", "m4v", "mov", "mkv", "webm", "avi", "mts", "m2ts", "3gp",
                     "mpg", "mpeg", "wmv"), "video"),
    **dict.fromkeys(("pages", "numbers", "key", "docx", "xlsx", "pptx", "odt", "ods",
                     "odp"), "zip"),
}
# Bytes the worker may read per file. Images and PSDs are read whole, so a bigger
# file gets no thumbnail; a PDF, video or package only reads what it needs.
READ_CAP = {"image": 256 << 20, "pdf": 256 << 20, "video": 128 << 20, "zip": 64 << 20}
MAX_REQ = 8 << 20
MAX_OUT = 4 << 20
SIZES = (64, 128, 256, 512, 1024)
DEADLINE_S = 30


def kind_of(name: str) -> str | None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return KINDS.get(ext)


def bucket(px: int) -> int:
    return next((s for s in SIZES if s >= px), SIZES[-1])


class Thumbnailer:
    def __init__(self, socket_path: str, concurrency: int = 2, cache_mb: int = 64):
        self.socket_path = socket_path
        self.slots = threading.BoundedSemaphore(concurrency)
        self.cache: OrderedDict[tuple, tuple[str, bytes] | None] = OrderedDict()
        self.cache_bytes, self.cache_max = 0, cache_mb << 20
        self.lock = threading.Lock()

    def get(self, key: tuple, name: str, size: int, px: int,
            read: Callable[[int, int], Iterator[bytes]]) -> tuple[str, bytes] | None:
        """(format, bytes) or None. `read(offset, length)` reads the caller's file."""
        kind = kind_of(name)
        if not kind or size <= 0 or (kind == "image" and size > READ_CAP["image"]):
            return None
        px = bucket(px)
        key = (*key, px)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
        if not self.slots.acquire(timeout=DEADLINE_S):
            raise TimeoutError("thumbnail workers busy")
        ext = name.rsplit(".", 1)[-1].lower()
        try:
            r = self._run(kind, ext, size, px, read)
        except Exception as e:  # noqa: BLE001 - NAS hiccup, timeout, bad worker: not cached
            log.warning("thumbnail .%s failed: %s", ext, e)
            return None
        finally:
            self.slots.release()
        with self.lock:
            self.cache[key] = r
            self.cache_bytes += len(r[1]) if r else 0
            while self.cache_bytes > self.cache_max and self.cache:
                _, old = self.cache.popitem(last=False)
                self.cache_bytes -= len(old[1]) if old else 0
        return r

    def _connect(self):
        if self.socket_path:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(DEADLINE_S)
            s.connect(self.socket_path)
            return s, None
        a, b = socket.socketpair()
        p = subprocess.Popen([sys.executable, "-I", "-m", "loft_gateway.thumbworker"],
                             stdin=b, stdout=b, stderr=subprocess.DEVNULL, close_fds=True)
        b.close()
        a.settimeout(DEADLINE_S)
        return a, p

    def _run(self, kind, ext, size, px, read) -> tuple[str, bytes] | None:
        """Serve one worker. Its every line is untrusted: strict parsing, hard caps.
        None = the worker says this file has no thumbnail (cached); raises otherwise."""
        sock, proc = self._connect()
        deadline = time.monotonic() + DEADLINE_S
        budget = READ_CAP[kind]
        try:
            rf, wf = sock.makefile("rb"), sock.makefile("wb")
            wf.write(json.dumps({"kind": kind, "ext": ext, "size": size, "px": px}).encode() + b"\n")
            wf.flush()
            while time.monotonic() < deadline:
                line = rf.readline(64).decode("ascii", "replace").split()
                if len(line) == 3 and line[0] == "R":
                    off, n = int(line[1]), int(line[2])
                    if not (0 <= off <= size and 0 < n <= MAX_REQ):
                        raise ValueError(f"bad range {off}+{n}")
                    n = min(n, size - off)
                    budget -= n
                    if budget < 0:
                        raise ValueError(f"{kind} read budget spent")
                    data = b"".join(read(off, n)) if n else b""
                    wf.write(f"{len(data)}\n".encode() + data)
                    wf.flush()
                elif len(line) == 3 and line[0] == "OK" and line[1] in ("jpeg", "png"):
                    n = int(line[2])
                    if not 0 < n <= MAX_OUT:
                        raise ValueError(f"bad output size {n}")
                    data = rf.read(n)
                    if len(data) != n:
                        raise ValueError("short output")
                    return line[1], data
                elif line and line[0] == "NO":
                    log.info("no thumbnail for .%s: %s", ext, " ".join(line[1:])[:80])
                    return None
                else:
                    raise ValueError(f"bad worker line {line[:3]!r}")
            raise TimeoutError("thumbnail deadline")
        finally:
            sock.close()
            if proc:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
