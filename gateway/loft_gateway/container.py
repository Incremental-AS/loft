"""Entry point of the container image: the gateway and the thumbnail server, each as
its own unprivileged user.

    loft-container                 # start (first start with LOFT_SMB_SERVER set runs init)
    loft-container <cmd> [...]     # anything else runs as the gateway user, e.g.
                                   # `docker exec -it loft loft-container loft-gateway doctor --user alice`

Starts as root only to: run `init` on first start, bind the thumbnail socket
(owner: the gateway user, 0600), and drop to the two users. Then:

* loft-thumb  thumbserver.py, on the inherited socket: one decoder process per file
              with rlimits. No access to /etc/loft or /var/lib/loft.
* loft        the gateway.

First start (no /etc/loft/gateway.toml) reads: LOFT_SMB_SERVER (required),
LOFT_LAN_URL (required: how devices at home reach this container's port 8443),
LOFT_SMB_ENCRYPTION, LOFT_NAME, LOFT_PUBLIC_URL, LOFT_PUBLIC_MODE.
"""
from __future__ import annotations

import os
import pwd
import signal
import socket
import subprocess
import sys
from pathlib import Path

ETC, STATE = Path("/etc/loft"), Path("/var/lib/loft")
CFG = ETC / "gateway.toml"
THUMB_SOCK = "/run/loft/thumb.sock"
GW_USER, THUMB_USER = "loft", "loft-thumb"


def _drop(user: str):
    pw = pwd.getpwnam(user)

    def fn():
        os.setgroups([])
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)
        os.umask(0o077)
    return fn, {"HOME": pw.pw_dir, "USER": user}


def _env(extra: dict) -> dict:
    keep = {k: v for k, v in os.environ.items()
            if k in ("PATH", "LANG", "TZ") or k.startswith("LOFT_")}
    return {**keep, **extra, "LOFT_CONFIG": str(CFG)}


def first_start():
    if CFG.exists():
        return
    e = os.environ
    if not e.get("LOFT_SMB_SERVER") or not e.get("LOFT_LAN_URL"):
        sys.exit("No /etc/loft/gateway.toml yet: set LOFT_SMB_SERVER and LOFT_LAN_URL for the "
                 "first start (or mount your own gateway.toml).")
    args = ["loft-gateway", "init", "-y", "--config", str(CFG), "--state-dir", str(STATE),
            "--master-key", "file", "--master-key-file", str(ETC / "master.key"),
            "--thumb-socket", THUMB_SOCK, "--public-host", "0.0.0.0",
            "--smb-server", e["LOFT_SMB_SERVER"], "--lan-url", e["LOFT_LAN_URL"],
            "--smb-encryption", e.get("LOFT_SMB_ENCRYPTION", "required"),
            "--name", e.get("LOFT_NAME", "Loft"),
            "--public-mode", e.get("LOFT_PUBLIC_MODE", "none")]
    if e.get("LOFT_PUBLIC_URL"):
        args += ["--public-url", e["LOFT_PUBLIC_URL"]]
    fn, env = _drop(GW_USER)
    subprocess.run(args, check=True, preexec_fn=fn, env=_env(env))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.geteuid() != 0:
        sys.exit("loft-container starts as root and drops to the loft users itself")
    for d in (ETC, STATE):
        d.mkdir(parents=True, exist_ok=True)
        pw = pwd.getpwnam(GW_USER)
        os.chown(d, pw.pw_uid, pw.pw_gid)
        os.chmod(d, 0o700)
    if argv:                                 # a one-off command as the gateway user
        fn, env = _drop(GW_USER)
        sys.exit(subprocess.run(argv, preexec_fn=fn, env=_env(env)).returncode)

    first_start()
    run = Path(THUMB_SOCK).parent
    run.mkdir(parents=True, exist_ok=True)
    gw = pwd.getpwnam(GW_USER)
    os.chown(run, gw.pw_uid, gw.pw_gid)
    os.chmod(run, 0o700)
    try:
        os.unlink(THUMB_SOCK)
    except FileNotFoundError:
        pass
    lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lsock.bind(THUMB_SOCK)
    os.chown(THUMB_SOCK, gw.pw_uid, gw.pw_gid)
    os.chmod(THUMB_SOCK, 0o600)
    lsock.listen(16)

    tfn, tenv = _drop(THUMB_USER)
    thumbs = subprocess.Popen([sys.executable, "-I", "-m", "loft_gateway.thumbserver",
                               "--fd", str(lsock.fileno())], pass_fds=[lsock.fileno()],
                              preexec_fn=tfn, cwd="/",
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                   "LANG": "C.UTF-8", **tenv})
    lsock.close()
    gfn, genv = _drop(GW_USER)
    gateway = subprocess.Popen(["loft-gateway", "serve", "--config", str(CFG)],
                               preexec_fn=gfn, env=_env(genv), cwd="/")

    def stop(sig, _frame):
        for p in (gateway, thumbs):
            if p.poll() is None:
                p.send_signal(sig)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    rc = gateway.wait()
    if thumbs.poll() is None:
        thumbs.terminate()
        thumbs.wait(10)
    sys.exit(rc)


if __name__ == "__main__":
    main()
