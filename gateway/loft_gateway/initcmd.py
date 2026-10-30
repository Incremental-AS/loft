"""`loft-gateway init`: first-time setup of a gateway (docs/design.md §B.5).

Asks for the SMB server (or takes flags), writes gateway.toml, makes the direct
listener's TLS key + a standby key, the master key (a systemd credential when
systemd-creds is there, else a 0600 file), and prints the pairing code.

    loft-gateway init                                   # asks
    loft-gateway init -y --smb-server nas.local --lan-url https://10.0.0.5:8443

Nothing secret is printed: not the master key, not the TLS keys.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import grp
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import admin, config, crypto

CERT_DAYS = 825     # trust comes from the pin; 825 keeps Apple's TLS rules happy


# ── keys ──────────────────────────────────────────────────────────────────

def make_tls_key(keyfile: Path, certfile: Path, hosts: list[str]) -> str:
    """EC P-256 key + self-signed cert for the direct listener. Returns the SPKI pin."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = []
    for h in dict.fromkeys(hosts):
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            sans.append(x509.DNSName(h))
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "loft direct")])
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(minutes=5))
         .not_valid_after(now + dt.timedelta(days=CERT_DAYS))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                      key_encipherment=False, data_encipherment=False,
                                      key_agreement=False, key_cert_sign=False, crl_sign=False,
                                      encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False))
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    cert = b.sign(key, hashes.SHA256())
    _write(keyfile, key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()), 0o600)
    _write(certfile, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return admin.spki_pin(str(certfile))


def _write(path: Path, data: bytes, mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _give_to_service_user(path: Path, user: str | None, mode: int):
    """root:<user> + mode, when running as root and the service user exists (.deb)."""
    if not user or os.geteuid() != 0:
        return
    try:
        gid = grp.getgrnam(user).gr_gid
    except KeyError:
        return
    os.chown(path, 0, gid)
    os.chmod(path, mode)


def store_master_key(mode: str, file_path: Path, cred_path: Path) -> tuple[str, Path]:
    """Make the master key and store it. Returns (how, where). The key is never printed."""
    key = crypto.generate_master_key().encode()
    if mode == "auto":
        mode = "systemd" if (shutil.which("systemd-creds") and os.geteuid() == 0
                             and Path("/run/systemd/system").exists()) else "file"
    if mode == "systemd":
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["systemd-creds", "encrypt", "--name=master.key", "-", str(cred_path)],
                       input=key, check=True, capture_output=True)
        os.chmod(cred_path, 0o600)
        return "systemd", cred_path
    _write(file_path, key + b"\n", 0o600)
    return "file", file_path


# ── config ────────────────────────────────────────────────────────────────

def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_toml(v: dict) -> str:
    lines = [
        "# Loft gateway settings (written by `loft-gateway init`). No secrets in this file.",
        "# Every setting and its default: `loft-admin config --effective`.",
        "# All top-level keys go ABOVE [direct].",
        "",
        f"smb_server = {_q(v['smb_server'])}",
        f"smb_encryption = {_q(v['smb_encryption'])}        # \"required\" or \"server\"",
        f"db_path = {_q(v['db_path'])}",
    ]
    if v.get("master_key_path"):
        lines.append(f"master_key_path = {_q(v['master_key_path'])}")
    lines += [
        "",
        "# Optional limits (empty = none): shares, blocked_shares, blocked_paths,",
        "# allowed_users_re, denied_users_re, code_users_re.",
        "shares = []",
        "blocked_shares = []",
        "blocked_paths = []",
        "",
        "# Pairing (docs/pairing.md)",
        f"gateway_name = {_q(v['gateway_name'])}",
        f"lan_url = {_q(v['lan_url'])}",
        f"public_url = {_q(v['public_url'])}",
        f"public_mode = {_q(v['public_mode'])}          # \"none\" | \"tunnel\" | \"direct\"",
        "# Live pin, then standby. Keep the standby key offline; see the init output.",
        "direct_spki_pins = [" + ", ".join(_q(p) for p in v["pins"]) + "]",
    ]
    if v.get("thumb_socket") is not None:
        lines.append(f"thumb_socket = {_q(v['thumb_socket'])}")
    lines += [
        "",
        "[direct]",
        f"host = {_q(v['direct_host'])}",
        f"port = {v['direct_port']}",
        f"certfile = {_q(v['certfile'])}",
        f"keyfile = {_q(v['keyfile'])}",
        "",
        "[public]                                # for a tunnel: proxy -> http://<host>:<port>",
        f"host = {_q(v['public_host'])}",
        f"port = {v['public_port']}",
        "",
    ]
    return "\n".join(lines)


def _guess_lan_ip() -> str:
    # The address the default route leaves from. No packet is sent (UDP connect).
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))            # TEST-NET-1: never routed anywhere real
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _ask(prompt: str, default: str | None, interactive: bool, choices=None) -> str:
    if not interactive:
        if default is None:
            raise SystemExit(f"init: {prompt.lower()} is required (flag or interactive)")
        return default
    while True:
        hint = f" [{default}]" if default else ""
        ans = input(f"{prompt}{hint}: ").strip() or (default or "")
        if ans and (not choices or ans in choices):
            return ans
        print(f"  choose one of: {', '.join(choices)}" if choices else "  required")


def build_parser(ap: argparse.ArgumentParser):
    ap.add_argument("--config", default=os.environ.get("LOFT_CONFIG", "/etc/loft/gateway.toml"))
    ap.add_argument("-y", "--non-interactive", action="store_true",
                    help="never ask; take flags and defaults")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing gateway.toml and keys")
    ap.add_argument("--smb-server")
    ap.add_argument("--smb-encryption", choices=["required", "server"])
    ap.add_argument("--name", help="gateway name shown in the apps")
    ap.add_argument("--lan-url", help="https://<address>:<port> devices use at home")
    ap.add_argument("--public-url", default=None)
    ap.add_argument("--public-mode", choices=["none", "tunnel", "direct"])
    ap.add_argument("--direct-host", default="0.0.0.0")
    ap.add_argument("--direct-port", type=int, default=8443)
    ap.add_argument("--public-host", default="127.0.0.1")
    ap.add_argument("--public-port", type=int, default=8080)
    ap.add_argument("--state-dir", default="/var/lib/loft", help="index database lives here")
    ap.add_argument("--key-dir", help="TLS keys (default: the config's folder)")
    ap.add_argument("--master-key", choices=["auto", "systemd", "file"], default="auto",
                    help="auto: a systemd credential when systemd-creds is there and we are "
                         "root, else a 0600 file")
    ap.add_argument("--master-key-file", help="where the file form goes "
                                              "(default <config dir>/master.key)")
    ap.add_argument("--thumb-socket", default=None,
                    help="thumbnail worker socket (default /run/loft-thumb.sock)")
    ap.add_argument("--service-user", default="loft",
                    help="as root: give this group read access to the TLS key and master key")
    ap.add_argument("--no-qr", action="store_true")


def run(a: argparse.Namespace) -> int:
    cfg = Path(a.config)
    interactive = not a.non_interactive and sys.stdin.isatty()
    if cfg.exists() and not a.force:
        raise SystemExit(f"{cfg} exists. `loft-gateway doctor` checks it; --force starts over.")
    etc = cfg.parent
    key_dir = Path(a.key_dir) if a.key_dir else etc

    if interactive:
        print("Loft gateway setup. Enter keeps the value in [brackets].\n")
    smb = a.smb_server or _ask("SMB server (host name or address of your NAS)", None,
                               interactive)
    enc = a.smb_encryption or _ask(
        "SMB encryption: required (SMB3 encrypted, recommended) or server (only when the "
        "NAS asks for it, for older boxes)", "required", interactive, ["required", "server"])
    name = a.name or _ask("Gateway name shown in the apps", "Loft", interactive)
    lan = a.lan_url or _ask("Address devices use at home",
                            f"https://{_guess_lan_ip()}:{a.direct_port}", interactive)
    mode = a.public_mode or _ask(
        "Outside access: none (home network / VPN), tunnel (e.g. Cloudflare Tunnel) or "
        "direct (port forward)", "none", interactive, ["none", "tunnel", "direct"])
    pub = a.public_url if a.public_url is not None else (
        _ask("Outside address (https://...)", None, interactive) if mode != "none" else "")
    for label, url in (("lan url", lan), *((("public url", pub),) if mode != "none" else ())):
        if not url.startswith("https://") or not urlsplit(url).hostname:
            raise SystemExit(f"init: {label} must be https://host[:port], not {url!r}")

    hosts = [urlsplit(lan).hostname]
    if mode == "direct":
        hosts.append(urlsplit(pub).hostname)     # the same key answers there
    hosts += ["localhost"]

    certfile, keyfile = key_dir / "direct.crt", key_dir / "direct.key"
    sb_cert, sb_key = key_dir / "standby" / "direct.crt", key_dir / "standby" / "direct.key"
    live_pin = make_tls_key(keyfile, certfile, hosts)
    standby_pin = make_tls_key(sb_key, sb_cert, hosts)
    _give_to_service_user(keyfile, a.service_user, 0o640)

    how, where = store_master_key(a.master_key, Path(a.master_key_file or etc / "master.key"),
                                  etc / "master.key.cred")
    if how == "file":
        _give_to_service_user(where, a.service_user, 0o640)

    v = dict(smb_server=smb, smb_encryption=enc, db_path=str(Path(a.state_dir) / "index.db"),
             master_key_path=str(where) if how == "file" else None,
             gateway_name=name, lan_url=lan.rstrip("/"), public_url=(pub or "").rstrip("/"),
             public_mode=mode, pins=[live_pin, standby_pin], thumb_socket=a.thumb_socket,
             direct_host=a.direct_host, direct_port=a.direct_port, certfile=str(certfile),
             keyfile=str(keyfile), public_host=a.public_host, public_port=a.public_port)
    _write(cfg, render_toml(v).encode(), 0o644)
    s = config.load(str(cfg))                       # what the gateway will read

    print(f"\nWrote {cfg}")
    print(f"TLS key  {keyfile}  fingerprint {live_pin}")
    print(f"Standby  {sb_key}  fingerprint {standby_pin}")
    print("         Move the standby key off this machine (keep it safe and offline). If the live")
    print("         key is ever lost or exposed, install the standby key and the apps keep trusting it.")
    print(f"Master key: {'systemd credential ' if how == 'systemd' else '0600 file '}{where}")
    if how == "systemd":
        print("         The unit loads it with LoadCredentialEncrypted=master.key:" + str(where))
    if mode == "direct":
        print("\nOutside access is `direct`: sign-in works from the internet. Failed sign-ins are")
        print(f"limited to {s.login_max_fail_user} per account and {s.login_max_fail_ip} per address "
              f"per {s.login_window_s // 60} minutes.")
    print("\nNext: `loft-gateway doctor --user <account>` checks the NAS, then start the service.\n")
    link = admin.pairing_link(s)
    print(f"Pair a device with {s.gateway_name!r}: scan this code in the Loft app, or open the link.")
    if not a.no_qr:
        print(admin.qr_text(link) or "(install `qrencode` or `segno` to show a QR code here)")
    print(link)
    print(admin.manual_text(admin.pairing_payload(s)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="loft-gateway init",
                                 description="First-time setup of a Loft gateway.")
    build_parser(ap)
    return run(ap.parse_args(argv))


__all__ = ["main", "run", "build_parser", "make_tls_key", "render_toml", "store_master_key"]
