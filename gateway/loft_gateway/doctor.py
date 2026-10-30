"""`loft-gateway doctor`: is this gateway set up right? (docs/design.md §B.5)

    loft-gateway doctor                      # config, keys, cert, SMB server, clock
    loft-gateway doctor --user alice         # + sign in as alice: encryption, share list
    loft-gateway doctor --json

Checks the settings, the master key, the direct listener's cert against the pins, the
SMB server (reachable, dialect, encryption), the clock against the SMB server's, and,
with --user, a real sign-in and share enumeration with that account. The password is
read from the terminal (or LOFT_DOCTOR_PASSWORD) and never printed.
Exit status: 0 all good, 1 something failed, 2 warnings only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import admin, config, crypto

OK, WARN, FAIL, SKIP = "ok", "warn", "FAIL", "skip"
CLOCK_WARN_S, CLOCK_FAIL_S = 30, 300


@dataclass
class Report:
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str):
        self.checks.append({"check": name, "status": status, "detail": detail})

    @property
    def exit_code(self) -> int:
        st = {c["status"] for c in self.checks}
        return 1 if FAIL in st else 2 if WARN in st else 0


# ── settings, keys, cert ──────────────────────────────────────────────────

def check_settings(r: Report, path: str) -> config.Settings | None:
    if not Path(path).exists():
        r.add("config", FAIL, f"{path} does not exist (run `loft-gateway init`)")
        return None
    try:
        s = config.load(path)
    except (ValueError, OSError) as e:
        r.add("config", FAIL, f"{path}: {e}")
        return None
    if not s.smb_server:
        r.add("config", FAIL, "smb_server is not set")
        return s
    r.add("config", OK, f"{path}: smb_server {s.smb_server}, smb_encryption {s.smb_encryption}, "
                        f"public_mode {s.public_mode}")
    return s


def check_master_key(r: Report, s: config.Settings):
    cred = Path(s.master_key_path).with_name("master.key.cred")
    if os.environ.get("CREDENTIALS_DIRECTORY") or Path(s.master_key_path).exists():
        try:
            crypto.load_master_key(s.master_key_path)
            mode = os.stat(s.master_key_path).st_mode & 0o777
            if not os.environ.get("CREDENTIALS_DIRECTORY") and mode & 0o007:
                r.add("master key", FAIL, f"{s.master_key_path} is readable by everyone "
                                          f"({oct(mode)}); chmod 600")
            else:
                r.add("master key", OK, f"{s.master_key_path} (32 bytes)")
        except (crypto.CryptoError, OSError) as e:
            r.add("master key", FAIL, str(e))
    elif cred.exists():
        r.add("master key", OK, f"systemd credential {cred} (decrypted only inside the "
                                "service; not readable from here)")
    else:
        r.add("master key", FAIL, f"neither {s.master_key_path} nor {cred} exists")


def check_cert(r: Report, s: config.Settings):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    d = s.direct
    if not (d.certfile and d.keyfile):
        r.add("tls", FAIL, "[direct] certfile/keyfile not set: the direct listener won't start")
        return
    try:
        cert = x509.load_pem_x509_certificate(Path(d.certfile).read_bytes())
    except (OSError, ValueError) as e:
        r.add("tls", FAIL, f"certfile {d.certfile}: {e}")
        return
    try:
        key = serialization.load_pem_private_key(Path(d.keyfile).read_bytes(), None)
    except PermissionError:
        key = None
        r.add("tls key", WARN, f"can't read {d.keyfile} as this user (run as root or the "
                               "service user to check it matches the cert)")
    except (OSError, ValueError) as e:
        r.add("tls key", FAIL, f"keyfile {d.keyfile}: {e}")
        return
    der = lambda k: k.public_bytes(serialization.Encoding.DER,                   # noqa: E731
                                   serialization.PublicFormat.SubjectPublicKeyInfo)
    if key is not None:
        if der(key.public_key()) != der(cert.public_key()):
            r.add("tls key", FAIL, "keyfile and certfile are not a pair")
        else:
            r.add("tls key", OK, "key matches the cert")
        if Path(d.keyfile).stat().st_mode & 0o007:
            r.add("tls key", FAIL, f"{d.keyfile} is readable by everyone; chmod 640")
    pin = admin.spki_pin(d.certfile)
    pins = s.direct_spki_pins
    if pin not in pins:
        r.add("pins", FAIL, "the cert's pin is not in direct_spki_pins: paired apps would "
                            "refuse this gateway after their next /v1/identity refresh")
    elif len(pins) < 2:
        r.add("pins", WARN, "only one pin: add a standby pin so a lost key doesn't strand "
                            "every paired device")
    else:
        r.add("pins", OK, f"cert pin is pin {pins.index(pin) + 1} of {len(pins)}")
    now = dt.datetime.now(dt.timezone.utc)
    end = cert.not_valid_after_utc
    if end < now:
        r.add("cert dates", WARN, f"expired {end:%Y-%m-%d}. The apps trust the pin, not the "
                                  "dates, but other tools will complain")
    else:
        r.add("cert dates", OK, f"valid until {end:%Y-%m-%d}")
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = {str(n) for n in san.get_values_for_type(x509.DNSName)} | {
            str(n) for n in san.get_values_for_type(x509.IPAddress)}
    except x509.ExtensionNotFound:
        names = set()
    for label, url in (("lan_url", s.lan_url), ("public_url", s.public_url
                                                if s.public_mode == "direct" else "")):
        if not url:
            continue
        host = urlsplit(url).hostname or ""
        if host not in names:
            r.add("cert names", WARN, f"{label} host {host} is not in the cert's names "
                                      f"({', '.join(sorted(names)) or 'none'}). Pinning still "
                                      "works; host-name checks would not")


def check_urls(r: Report, s: config.Settings):
    if not s.lan_url:
        r.add("pairing", FAIL, "lan_url is not set: `loft-gateway pair` can't make a code")
        return
    u = urlsplit(s.lan_url)
    if u.scheme != "https":
        r.add("pairing", FAIL, "lan_url must be https://")
    elif (u.port or 443) != s.direct.port:
        r.add("pairing", WARN, f"lan_url port {u.port or 443} differs from [direct] port "
                               f"{s.direct.port} (fine behind a port mapping)")
    else:
        r.add("pairing", OK, f"lan_url {s.lan_url}")
    if s.public_mode == "direct":
        r.add("public mode", OK, f"direct: sign-in from the internet at {s.public_url}, limited "
                                 f"to {s.login_max_fail_user} failures per account and "
                                 f"{s.login_max_fail_ip} per address per "
                                 f"{s.login_window_s // 60} min")
        if s.login_max_fail_user > 10 or s.login_max_fail_ip > 50:
            r.add("login limits", WARN, "loose login limits with sign-in open to the internet")
    elif s.public_mode == "tunnel":
        host = s.public.host
        if host not in ("127.0.0.1", "::1", "localhost"):
            r.add("public mode", WARN, f"tunnel mode, but the public listener binds {host}: it "
                                       "trusts CF-Connecting-IP, so only the tunnel should reach it")
        else:
            r.add("public mode", OK, f"tunnel: {s.public_url} -> {host}:{s.public.port} "
                                     "(device tokens only)")


# ── SMB server ────────────────────────────────────────────────────────────

def negotiate(server: str, timeout: float = 10):
    """SMB2/3 NEGOTIATE only (no sign-in). Returns (dialect, supports_encryption,
    server_time or None)."""
    from smbprotocol.connection import Connection
    conn = Connection(uuid.uuid4(), server, 445, require_signing=False)
    seen = {}
    orig = conn._send_smb2_negotiate

    def spy(*a, **k):
        resp = orig(*a, **k)
        seen["resp"] = resp
        return resp
    conn._send_smb2_negotiate = spy
    try:
        conn.connect(timeout=timeout)
        t = None
        try:
            t = seen["resp"]["system_time"].get_value()
        except Exception:  # noqa: BLE001
            pass
        return conn.dialect, bool(conn.supports_encryption), t
    finally:
        try:
            conn.disconnect(True)
        except Exception:  # noqa: BLE001
            pass


DIALECTS = {0x0202: "2.0.2", 0x0210: "2.1", 0x0300: "3.0", 0x0302: "3.0.2", 0x0311: "3.1.1"}


def check_smb(r: Report, s: config.Settings, timeout: float = 10):
    host = s.smb_server
    try:
        with socket.create_connection((host, 445), timeout=timeout):
            pass
    except OSError as e:
        r.add("smb reachable", FAIL, f"{host}:445: {e}")
        return
    r.add("smb reachable", OK, f"{host}:445 answers")
    try:
        dialect, enc, server_time = negotiate(host, timeout)
    except Exception as e:  # noqa: BLE001
        r.add("smb dialect", FAIL, f"SMB2/3 negotiate failed: {e}")
        return
    name = DIALECTS.get(dialect, hex(dialect or 0))
    if dialect < 0x0300:
        r.add("smb dialect", FAIL if s.smb_encryption == "required" else WARN,
              f"SMB {name}: no SMB3 encryption possible" + (
                  " and smb_encryption is required" if s.smb_encryption == "required" else ""))
    else:
        r.add("smb dialect", OK, f"SMB {name}")
    if s.smb_encryption == "required" and not enc:
        r.add("smb encryption", FAIL, "the server offers no SMB3 encryption but smb_encryption "
                                      "is required: every sign-in will fail. Turn encryption on "
                                      "in the NAS, or set smb_encryption = \"server\"")
    elif enc:
        r.add("smb encryption", OK, f"server supports SMB3 encryption (gateway: "
                                    f"{s.smb_encryption})")
    else:
        r.add("smb encryption", WARN, "server offers no encryption: traffic to the NAS is in "
                                      "the clear (smb_encryption = \"server\")")
    check_clock(r, server_time)


def check_clock(r: Report, server_time):
    if not isinstance(server_time, dt.datetime) or server_time.year < 2000:
        r.add("clock", SKIP, "the SMB server didn't report its time")
        return
    if server_time.tzinfo is None:
        server_time = server_time.replace(tzinfo=dt.timezone.utc)
    skew = (dt.datetime.now(dt.timezone.utc) - server_time).total_seconds()
    msg = f"{skew:+.0f} s against the SMB server"
    if abs(skew) >= CLOCK_FAIL_S:
        r.add("clock", FAIL, msg + ": fix NTP on this host or the NAS (token expiry, "
                                   "file times and sign-in all depend on it)")
    elif abs(skew) >= CLOCK_WARN_S:
        r.add("clock", WARN, msg)
    else:
        r.add("clock", OK, msg)


def check_signin(r: Report, s: config.Settings, user: str, password: str):
    from .rootfs import ALL_SHARES, RootFS
    from .storage.base import AuthFailed
    from .storage.smb import SMBBackend
    try:
        sess = SMBBackend(s.smb_server, s.smb_encryption == "required").authenticate(user,
                                                                                     password)
    except AuthFailed:
        r.add("smb sign-in", FAIL, f"{user}: wrong user name or password")
        return
    except Exception as e:  # noqa: BLE001 - StorageError, or a library error: report it
        r.add("smb sign-in", FAIL, f"{user}: {e}")
        return
    try:
        encrypted = _session_encrypted(sess)
        r.add("smb sign-in", OK, f"{user} signed in" + (
            ", session encrypted" if encrypted else ", session NOT encrypted" if encrypted
            is False else ""))
        if encrypted is False and s.smb_encryption == "required":
            r.add("smb encryption", FAIL, "session is not encrypted although required")
        try:
            listed = sess.list_shares()
        except Exception as e:  # noqa: BLE001
            r.add("shares", FAIL if not s.shares else WARN,
                  f"share enumeration failed: {e}" + ("" if s.shares else
                                                      " (set `shares` in gateway.toml)"))
            listed = None
        if listed is not None:
            offered = RootFS({"share": ALL_SHARES, "path": ""}, sess, s.blocked_shares,
                             s.shares, {}).shares()
            missing = [x for x in s.shares if x not in offered]
            r.add("shares", WARN if (missing or not offered) else OK,
                  f"server lists {len(listed)}; Loft offers {user}: "
                  f"{', '.join(offered) or 'none'}" + (
                      f"; configured but not reachable: {', '.join(missing)}" if missing else ""))
    finally:
        sess.close()


def _session_encrypted(sess) -> bool | None:
    try:
        from smbclient._pool import get_smb_tree
        _, _ = get_smb_tree(sess._unc("IPC$"), **sess._kw)
        for conn in sess._cache.values():
            for ss in conn.session_table.values():
                return bool(ss.encrypt_data)
    except Exception:  # noqa: BLE001
        return None
    return None


def run(a: argparse.Namespace) -> int:
    r = Report()
    s = check_settings(r, a.config)
    if s is not None:
        check_master_key(r, s)
        check_cert(r, s)
        check_urls(r, s)
        if s.thumb_socket and not Path(s.thumb_socket).exists():
            r.add("thumbnails", WARN, f"{s.thumb_socket} is missing (loft-thumb.socket not "
                                      "running?): no thumbnails")
        if s.smb_server:
            check_smb(r, s, a.timeout)
            if a.user:
                pw = os.environ.get("LOFT_DOCTOR_PASSWORD") or getpass.getpass(
                    f"Password for {a.user} on {s.smb_server}: ")
                check_signin(r, s, a.user, pw)
            else:
                r.add("shares", SKIP, "pass --user <account> to sign in and list shares")
    if a.json:
        print(json.dumps({"ok": r.exit_code == 0, "checks": r.checks}, indent=2))
    else:
        w = max(len(c["check"]) for c in r.checks)
        for c in r.checks:
            print(f"[{c['status']:>4}] {c['check']:<{w}}  {c['detail']}")
        print({0: "\nAll good.", 1: "\nSomething needs fixing (FAIL above).",
               2: "\nWorks, with warnings."}[r.exit_code])
    return r.exit_code


def build_parser(ap: argparse.ArgumentParser):
    ap.add_argument("--config", default=os.environ.get("LOFT_CONFIG", "/etc/loft/gateway.toml"))
    ap.add_argument("--user", help="sign in as this SMB account to check encryption and shares")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=float, default=10)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="loft-gateway doctor",
                                 description="Check a Loft gateway's setup.")
    build_parser(ap)
    return run(ap.parse_args(argv))


