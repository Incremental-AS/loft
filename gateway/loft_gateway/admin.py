"""Admin CLI on the gateway host.

    loft-admin devices [--user U]
    loft-admin revoke DEVICE_ID
    loft-admin audit [--user U] [-n 50]
    loft-admin codes                 # sign-in codes (App Review), never the code itself
    loft-admin revoke-code CODE_ID
    loft-admin gen-master-key        # prints a base64 key for systemd-creds
    loft-admin pair                  # pairing link + QR code for the Loft apps
    loft-admin config --effective    # every setting as the gateway reads it, defaults included (JSON)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import time

from . import config, crypto
from .db import DB


def _t(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="loft-admin")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("devices")
    d.add_argument("--user")
    r = sub.add_parser("revoke")
    r.add_argument("device_id")
    a = sub.add_parser("audit")
    a.add_argument("--user")
    a.add_argument("-n", type=int, default=50)
    sub.add_parser("codes")
    rc = sub.add_parser("revoke-code")
    rc.add_argument("code_id")
    sub.add_parser("gen-master-key")
    pr = sub.add_parser("pair")
    pr.add_argument("--no-qr", action="store_true")
    cf = sub.add_parser("config")
    cf.add_argument("--effective", action="store_true", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "gen-master-key":
        print(crypto.generate_master_key())
        return
    s = config.load(args.config)
    if args.cmd == "config":
        # Deploy check: diff this before/after an upgrade, so no setting a site
        # depends on silently falls back to a changed default. Paths only, no secrets.
        print(json.dumps(effective_config(s), indent=2, sort_keys=True))
        return
    if args.cmd == "pair":
        link = pairing_link(s)
        print(f"Pair a device with {s.gateway_name!r}: scan this code in the Loft app, or open the link on the device.")
        if not args.no_qr:
            print(qr_text(link) or "(install `qrencode` to show a QR code here)")
        print(link)
        print(manual_text(pairing_payload(s)))
        return
    db = DB(s.db_path)
    if args.cmd == "devices":
        q, p = "SELECT * FROM devices", ()
        if args.user:
            q, p = q + " WHERE nas_user=?", (args.user,)
        for row in db.all(q + " ORDER BY nas_user, created_at", p):
            state = "REVOKED" if row["revoked_at"] else (
                "expired" if row["expires_at"] < time.time() else "active")
            print(f"{row['id']}  {row['nas_user']:<16} {row['device_name']:<24} {state:<8}"
                  f" last {_t(row['last_used_at'])} from {row['last_ip'] or '-'}")
    elif args.cmd == "revoke":
        # Same effect as the API path: credential blob wiped, session dropped on
        # the next request (the running process re-reads the row every request).
        row = db.one("SELECT nas_user FROM devices WHERE id=?", (args.device_id,))
        if not row:
            raise SystemExit("no such device")
        with db.tx() as c:
            c.execute("UPDATE devices SET revoked_at=?, wrapped_cred=x'', prev_wrapped_cred=NULL,"
                      " prev_token_hash=NULL, prev_valid_until=NULL WHERE id=?",
                      (time.time(), args.device_id))
        db.audit("device_revoked", nas_user=row["nas_user"], device_id=args.device_id, by="admin-cli")
        print("revoked", args.device_id)
    elif args.cmd == "codes":
        for row in db.all("SELECT * FROM signin_codes ORDER BY created_at"):
            state = "REVOKED" if row["revoked_at"] else (
                "expired" if row["expires_at"] < time.time() else
                "used up" if row["uses"] >= row["max_uses"] else "active")
            print(f"{row['id']}  {row['nas_user']:<16} {state:<8} used {row['uses']}/{row['max_uses']}"
                  f"  made {_t(row['created_at'])}  until {_t(row['expires_at'])}")
    elif args.cmd == "revoke-code":
        # Same effect as Gateway.revoke_code: the wrapped credential is wiped.
        row = db.one("SELECT nas_user FROM signin_codes WHERE id=?", (args.code_id.upper(),))
        if not row:
            raise SystemExit("no such code")
        with db.tx() as c:
            c.execute("UPDATE signin_codes SET revoked_at=?, wrapped_cred=x'' WHERE id=?",
                      (time.time(), args.code_id.upper()))
        db.audit("code_revoked", nas_user=row["nas_user"], code_id=args.code_id.upper(),
                 by="admin-cli")
        print("revoked", args.code_id.upper())
    elif args.cmd == "audit":
        q, p = "SELECT * FROM audit", ()
        if args.user:
            q, p = q + " WHERE nas_user=?", (args.user,)
        for row in reversed(db.all(q + " ORDER BY id DESC LIMIT ?", (*p, args.n))):
            det = json.loads(row["detail"]) if row["detail"] else {}
            print(f"{_t(row['at'])}  {row['event']:<22} {row['nas_user'] or '-':<14}"
                  f" {row['route'] or '-':<6} {row['ip'] or '-':<15} {det}")



def effective_config(s: config.Settings) -> dict:
    import dataclasses
    return dataclasses.asdict(s)


PAIR_BASE = "https://loft.incremental.no/pair#"


def spki_pin(certfile: str) -> str:
    """base64(sha256(SubjectPublicKeyInfo)) of the certificate's key, the form the
    apps pin (and /v1/identity serves)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_pem_x509_certificate(open(certfile, "rb").read())
    spki = cert.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def pairing_payload(s: config.Settings) -> dict:
    """What a device needs to reach this gateway and trust it from the first byte.
    Format: docs/pairing.md. No secrets: anyone with the code still needs
    an account on the SMB server."""
    if not s.lan_url.startswith("https://"):
        raise SystemExit("lan_url is not set in gateway.toml (https://<address>:<port> of the direct listener)")
    pins = []
    if s.direct.certfile:
        pins.append(spki_pin(s.direct.certfile))
    pins += [p for p in s.direct_spki_pins if p not in pins]
    if not pins:
        raise SystemExit("no key pins: set [direct] certfile (and direct_spki_pins with the standby pin)")
    p = {"v": 1, "name": s.gateway_name, "lan": s.lan_url.rstrip("/"), "pins": pins}
    if s.public_mode != "none":
        p["pub"], p["mode"] = s.public_url.rstrip("/"), s.public_mode
    return p


def encode_payload(p: dict) -> str:
    raw = json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_payload(text: str) -> dict:
    text = text.split("#", 1)[-1].strip()
    return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))


def manual_text(p: dict) -> str:
    """The apps' "Set up manually" fields, labelled as the apps label them. The first fingerprint is
    the live key; any after it are standby keys, and each one works."""
    addr = p["lan"].removeprefix("https://")
    lines = ["", "Or set it up manually in the app:", f"  Address          {addr}"]
    lines += [f"  Key fingerprint  {pin}" for pin in p["pins"][:1]]
    lines += [f"  (standby)        {pin}" for pin in p["pins"][1:]]
    return "\n".join(lines)


def pairing_link(s: config.Settings) -> str:
    return PAIR_BASE + encode_payload(pairing_payload(s))


def qr_text(link: str) -> str | None:
    """A terminal QR code. segno if installed, else the qrencode tool, else None."""
    try:
        import io

        import segno
        buf = io.StringIO()
        segno.make(link, error="m").terminal(out=buf, compact=True)
        return buf.getvalue()
    except ImportError:
        pass
    if shutil.which("qrencode"):
        return subprocess.run(["qrencode", "-t", "UTF8", "-m", "2", link],
                              capture_output=True, text=True, check=False).stdout or None
    return None


if __name__ == "__main__":
    main()
