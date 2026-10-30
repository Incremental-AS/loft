"""Device tokens and the wrapped SMB credential.

The gateway must open SMB sessions as the user, so it needs the user's NAS
password while that user's devices are active. It never stores it in a form it
can read on its own:

    blob = AESGCM(master_key).encrypt(
               AESGCM(HKDF(token_secret, device_id)).encrypt(password))

* The inner key exists only while a request presents the device token. The DB
  holds sha256(token_secret), not the secret, so DB + master key from a stolen
  backup still cannot decrypt anything.
* The outer key keeps a leaked token useless without the gateway host.
* One blob per device. Revoking a device deletes its blob; revoking a user's
  last device leaves nothing to decrypt.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_PREFIX = "nst_"
_BLOB_V1 = b"\x01"


class CryptoError(Exception):
    pass


def load_master_key(path: str) -> bytes:
    raw = Path(path).read_bytes().strip()
    key = base64.b64decode(raw) if len(raw) != 32 else raw
    if len(key) != 32:
        raise CryptoError(f"master key at {path} must be 32 bytes (raw or base64)")
    return key


def generate_master_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


# ── tokens ────────────────────────────────────────────────────────────────

def new_token(device_id: str) -> tuple[str, bytes]:
    """Return (token string for the client, secret bytes)."""
    secret = secrets.token_bytes(32)
    tok = f"{TOKEN_PREFIX}{device_id}.{base64.urlsafe_b64encode(secret).decode().rstrip('=')}"
    return tok, secret


def parse_token(tok: str) -> tuple[str, bytes]:
    if not tok.startswith(TOKEN_PREFIX) or "." not in tok:
        raise CryptoError("malformed token")
    device_id, b64 = tok[len(TOKEN_PREFIX):].split(".", 1)
    try:
        secret = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except Exception as e:  # noqa: BLE001
        raise CryptoError("malformed token") from e
    if len(secret) != 32:
        raise CryptoError("malformed token")
    return device_id, secret


# Sign-in codes are typed by a person (App Review), so: 24 characters from an
# alphabet without 0/O/1/I/L/U, shown as 6 groups of 4. The first 8 are the
# code's id (lookup), the last 16 its secret (78.5 bits; redemption is rate
# limited per IP and per code, and the DB keeps only sha256 of the secret).
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_signin_code() -> tuple[str, str, bytes]:
    """Return (code as shown, code id, secret bytes)."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(24))
    return "-".join(raw[i:i + 4] for i in range(0, 24, 4)), raw[:8], raw[8:].encode()


def parse_signin_code(code: str) -> tuple[str, bytes]:
    raw = "".join(ch for ch in code.upper() if ch.isalnum())
    raw = raw.replace("0", "O").replace("1", "I")      # never in a code; refuse below
    if len(raw) != 24 or any(ch not in CODE_ALPHABET for ch in raw):
        raise CryptoError("malformed code")
    return raw[:8], raw[8:].encode()


def token_hash(secret: bytes) -> bytes:
    # 256 random bits: a fast hash is enough, a KDF would only cost latency.
    return hashlib.sha256(secret).digest()


def hash_matches(secret: bytes, stored: bytes | None) -> bool:
    return stored is not None and hmac.compare_digest(token_hash(secret), stored)


# ── credential wrapping ───────────────────────────────────────────────────

def _inner_key(secret: bytes, device_id: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=device_id.encode(),
                info=b"loft smb credential v1").derive(secret)


def wrap_credential(master: bytes, secret: bytes, device_id: str, nas_user: str,
                    password: str) -> bytes:
    aad = f"{device_id}\x00{nas_user}".encode()
    n1, n2 = os.urandom(12), os.urandom(12)
    inner = n1 + AESGCM(_inner_key(secret, device_id)).encrypt(n1, password.encode(), aad)
    return _BLOB_V1 + n2 + AESGCM(master).encrypt(n2, inner, aad)


def unwrap_credential(master: bytes, secret: bytes, device_id: str, nas_user: str,
                      blob: bytes) -> str:
    if not blob or blob[:1] != _BLOB_V1:
        raise CryptoError("unknown credential blob version")
    aad = f"{device_id}\x00{nas_user}".encode()
    try:
        inner = AESGCM(master).decrypt(blob[1:13], blob[13:], aad)
        return AESGCM(_inner_key(secret, device_id)).decrypt(inner[:12], inner[12:], aad).decode()
    except Exception as e:  # noqa: BLE001  (InvalidTag)
        raise CryptoError("credential does not decrypt") from e
