"""Thumbnail worker server for hosts without systemd (the container image).

What loft-thumb.socket (Accept=yes) + loft-thumb@.service do under systemd: one fresh
`python -I -m loft_gateway.thumbworker` per connection, its stdin/stdout the
connection, with hard limits. Here the limits are rlimits (address space, CPU time,
open files, processes, no core dumps), a wall-clock kill and a cap on parallel workers.

It runs as its own unprivileged user (not the gateway's), so a decoder bug can't read
the master key, the TLS key or the index. Unlike the systemd unit it can NOT take the
network away or hide the file system beyond normal permissions: the container docs
say so.

    python -m loft_gateway.thumbserver --fd N          # listening socket inherited (container.py)
    python -m loft_gateway.thumbserver --socket PATH   # bind it ourselves
"""
from __future__ import annotations

import argparse
import os
import resource
import signal
import socket
import subprocess
import sys
import threading

MAX_WORKERS = 4                  # MaxConnections=4
WALL_S = 40                      # RuntimeMaxSec=40
CPU_S = 40
AS_BYTES = 1536 << 20            # address space; MemoryMax=512M is RSS, AS needs headroom
NOFILE = 256                     # LimitNOFILE=256
NPROC = 64                       # TasksMax=16 per worker, per user here


def _limits():
    resource.setrlimit(resource.RLIMIT_AS, (AS_BYTES, AS_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_S, CPU_S))
    resource.setrlimit(resource.RLIMIT_NOFILE, (NOFILE, NOFILE))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (NPROC, NPROC))
    except (ValueError, OSError):
        pass
    os.umask(0o077)
    os.setsid()


def serve(listener: socket.socket):
    slots = threading.BoundedSemaphore(MAX_WORKERS)
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"}

    def handle(conn: socket.socket):
        try:
            p = subprocess.Popen([sys.executable, "-I", "-m", "loft_gateway.thumbworker"],
                                 stdin=conn, stdout=conn, stderr=sys.stderr, close_fds=True,
                                 cwd="/", env=env, preexec_fn=_limits)
        except OSError as e:
            print(f"thumbserver: spawn failed: {e}", file=sys.stderr)
            conn.close()
            slots.release()
            return
        conn.close()
        try:
            p.wait(timeout=WALL_S)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        finally:
            slots.release()

    while True:
        conn, _ = listener.accept()
        if not slots.acquire(blocking=False):
            conn.close()             # the gateway counts this as "busy" and retries later
            continue
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="loft-thumbserver")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fd", type=int, help="inherited listening socket")
    g.add_argument("--socket", help="unix socket path to bind (mode 0600)")
    a = ap.parse_args(argv)
    if a.fd is not None:
        listener = socket.socket(fileno=a.fd)
    else:
        try:
            os.unlink(a.socket)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        listener.bind(a.socket)
        os.umask(old)
        listener.listen(16)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try:
        serve(listener)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
