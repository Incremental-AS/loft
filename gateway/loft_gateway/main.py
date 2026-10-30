"""Entry point: one process, two listeners (direct + public), one Gateway.

    python -m loft_gateway.main [--config /etc/loft/gateway.toml]
"""
from __future__ import annotations

import argparse
import asyncio
import threading
import time

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


def _janitor(gw: Gateway):
    while True:
        time.sleep(60)
        try:
            gw.expire_sessions()
        except Exception:  # noqa: BLE001
            pass


USAGE = """\
loft-gateway [--config FILE]            run the gateway (same as `serve`)
loft-gateway serve [--config FILE]
loft-gateway init [...]                 first-time setup: gateway.toml, keys, pairing code
loft-gateway doctor [--user U] [...]    check the setup, the SMB server and the clock
loft-gateway pair [--no-qr]             pairing link + QR code for the Loft apps
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
    if cmd != "serve":
        raise SystemExit(f"unknown command {cmd!r}\n\n{USAGE}")
    ap = argparse.ArgumentParser(prog="loft-gateway", usage=USAGE)
    ap.add_argument("--config")
    a = ap.parse_args(rest)
    gw = build(a.config)
    threading.Thread(target=_janitor, args=(gw,), daemon=True).start()
    asyncio.run(serve(gw))


if __name__ == "__main__":
    main()
