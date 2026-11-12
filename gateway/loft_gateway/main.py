"""Entry point: one process, two listeners (direct + public), one Gateway.

    python -m loft_gateway.main [--config /etc/loft/gateway.toml]
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import logging
import os
import signal
import sys
import threading
import time
import tracemalloc

import uvicorn

from . import config, crypto
from .api import create_app
from .db import DB
from .service import Gateway
from .storage.smb import SMBBackend


def build(cfg_path: str | None = None) -> Gateway:
    s = config.load(cfg_path)
    if not s.smb_server:
        raise SystemExit("smb_server is not set in gateway.toml (run `loft-gateway init`)")
    return Gateway(s, DB(s.db_path), SMBBackend(s.smb_server, s.smb_encryption == "required"),
                   crypto.load_master_key(s.master_key_path))


class _Server(uvicorn.Server):
    """On SIGTERM/SIGINT, stop both listeners at once and release the long-polls.
    Stock uvicorn: only the last-started server gets the signal, and each waits for
    its open requests, so a restart sat ~25 s behind the Mac's /v1/changes wait."""

    def __init__(self, cfg: uvicorn.Config, on_exit):
        super().__init__(cfg)
        self._on_exit = on_exit

    def handle_exit(self, sig, frame):
        self._on_exit()
        super().handle_exit(sig, frame)


async def serve(gw: Gateway):
    gw.search.start()                   # content indexing in the background (search.py)
    d, p = gw.s.direct, gw.s.public
    if not (d.certfile and d.keyfile):
        raise SystemExit("direct listener needs certfile + keyfile (the pinned gateway key); "
                         "password login must never run without TLS")
    def on_exit():
        gw.release_waiters()
        for s in servers:
            s.should_exit = True

    servers = [
        _Server(uvicorn.Config(create_app(gw, "direct"), host=d.host, port=d.port,
                                      ssl_certfile=d.certfile, ssl_keyfile=d.keyfile,
                                      proxy_headers=False, server_header=False,
                                      log_level="info"), on_exit),
        _Server(uvicorn.Config(create_app(gw, "public"), host=p.host, port=p.port,
                                      proxy_headers=False, server_header=False,
                                      log_level="info"), on_exit),
    ]
    await asyncio.gather(*(s.serve() for s in servers))


class _MallInfo2(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in
                "arena ordblks smblks hblks hblkhd usmblks fsmblks uordblks fordblks keepcost".split()]


def _mallinfo2() -> _MallInfo2:
    f = ctypes.CDLL(None).mallinfo2
    f.restype = _MallInfo2
    return f()


def _memory_report(gw: Gateway):
    """`systemctl kill --kill-whom=main -s USR2 loft-gateway` writes where the memory is
    to the journal: RSS, glibc's view (live mmapped blocks vs freed but kept), the
    caches, the largest object types, and, when the gateway was started with
    LOFT_TRACEMALLOC=<frames>, the allocation sites. Read-only, takes a second or two.
    --kill-whom=main: USR2 terminates a process without a handler (thumbworker children,
    gateways before 0.2). The type census can't see bytes/str buffers (not GC-tracked);
    glibc's mmapped figure and tracemalloc do."""
    def report(*_):
        try:
            with open("/proc/self/status") as f:
                st = dict(line.split(":", 1) for line in f if ":" in line)
        except OSError:
            st = {}                                     # no /proc (macOS)
        out = [f"memory report: VmRSS {st.get('VmRSS', '?').strip()}, "
               f"RssAnon {st.get('RssAnon', '?').strip()}"]
        try:
            m = _mallinfo2()
            out.append(f"glibc: {m.hblkhd >> 20} MiB in {m.hblks} mmapped blocks, arenas "
                       f"{m.arena >> 20} MiB ({m.uordblks >> 20} MiB in use, "
                       f"{m.fordblks >> 20} MiB freed but kept)")
        except (OSError, AttributeError):
            pass                                        # not glibc (musl, macOS)
        out.append(f"{len(gw._sessions)} SMB sessions, thumbnail cache "
                   f"{gw.thumbs.cache_bytes >> 20} MiB in {len(gw.thumbs.cache)} entries")
        sizes: dict[str, list[int]] = {}
        for o in gc.get_objects():
            t = sizes.setdefault(type(o).__qualname__, [0, 0])
            t[0] += 1
            try:
                t[1] += sys.getsizeof(o)
            except TypeError:
                pass
        for name, (n, b) in sorted(sizes.items(), key=lambda kv: -kv[1][1])[:15]:
            out.append(f"  {b >> 20:6d} MiB {n:9d} x {name}")
        if tracemalloc.is_tracing():
            cur, peak = tracemalloc.get_traced_memory()
            out.append(f"tracemalloc: {cur >> 20} MiB now, {peak >> 20} MiB peak")
            for st in tracemalloc.take_snapshot().statistics("traceback")[:15]:
                out.append(f"  {st.size >> 20:6d} MiB {st.count:9d} blocks")
                out.extend("      " + line for line in st.traceback.format()[-6:])
        print("\n".join(out), file=sys.stderr, flush=True)
    if os.environ.get("LOFT_TRACEMALLOC", "").isdigit():
        tracemalloc.start(int(os.environ["LOFT_TRACEMALLOC"]))
    signal.signal(signal.SIGUSR2, report)


def _janitor(gw: Gateway):
    while True:
        time.sleep(60)
        try:
            gw.expire_sessions()
        except Exception:  # noqa: BLE001
            pass


def _logging():
    """The gateway's own loggers (loft.*) to stderr = the journal, from INFO up.
    Not the root logger: smbprotocol logs every SMB request at INFO."""
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    lg = logging.getLogger("loft")
    lg.setLevel(logging.INFO)
    lg.addHandler(h)
    lg.propagate = False


USAGE = """\
loft-gateway [--config FILE]            run the gateway (same as `serve`)
loft-gateway serve [--config FILE]
loft-gateway init [...]                 first-time setup: gateway.toml, keys, pairing code
loft-gateway doctor [--user U] [...]    check the setup, the SMB server and the clock
loft-gateway pair [--no-qr]             pairing link + QR code for the Loft apps
loft-gateway mcp add|list|revoke        connections for assistants (MCP clients) at /mcp
loft-admin ...                          devices, audit, sign-in codes, config --effective
"""


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv and not argv[0].startswith("-") else "serve"
    rest = argv[1:] if argv and argv[0] == cmd else argv
    if cmd == "init":
        from . import initcmd
        raise SystemExit(initcmd.main(rest))
    if cmd == "doctor":
        from . import doctor
        raise SystemExit(doctor.main(rest))
    if cmd == "pair":
        from . import admin
        pp = argparse.ArgumentParser(prog="loft-gateway pair")
        pp.add_argument("--config")
        pp.add_argument("--no-qr", action="store_true")
        p = pp.parse_args(rest)
        raise SystemExit(admin.main([*(["--config", p.config] if p.config else []), "pair",
                                     *(["--no-qr"] if p.no_qr else [])]))
    if cmd == "mcp":
        from . import mcp
        raise SystemExit(mcp.cli(rest))
    if cmd != "serve":
        raise SystemExit(f"unknown command {cmd!r}\n\n{USAGE}")
    _logging()
    ap = argparse.ArgumentParser(prog="loft-gateway", usage=USAGE)
    ap.add_argument("--config")
    a = ap.parse_args(rest)
    gw = build(a.config)
    _memory_report(gw)
    threading.Thread(target=_janitor, args=(gw,), daemon=True).start()
    asyncio.run(serve(gw))


if __name__ == "__main__":
    main()
