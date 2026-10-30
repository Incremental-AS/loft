"""Gateway settings, read from one TOML file (default /etc/loft/gateway.toml).

Every value has a default so tests and local dev can run from an empty file.
Secrets are never in this file: the master key comes from a systemd credential
(LoadCredentialEncrypted=master.key) or, in dev, from a path given here.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Listener:
    host: str
    port: int
    certfile: str | None = None   # direct listener only: the pinned gateway cert
    keyfile: str | None = None


@dataclass
class Settings:
    db_path: str = "/var/lib/loft/index.db"
    # Where the master key is read from. systemd puts LoadCredentialEncrypted
    # files under $CREDENTIALS_DIRECTORY; this path is the dev fallback.
    master_key_path: str = "/etc/loft/master.key"

    # The SMB server (host name or address) the gateway signs in to as each user.
    # Required: `loft-gateway init` asks for it.
    smb_server: str = ""
    # "required": every SMB message is SMB3-encrypted; a server that can't (SMB < 3,
    # guest mapping) fails the sign-in. "server": encrypted only when the server or share
    # demands it, for older NAS boxes. Keep "required" unless the NAS can't do it.
    smb_encryption: str = "required"
    # Shares offered. Empty (the default) = whatever the server lists for the signed-in
    # account (SRVSVC share enum; Samba with access-based share enumeration lists only
    # the shares that account may open). A non-empty list offers only those, each checked
    # with can_access.
    shares: list[str] = field(default_factory=list)
    # Never offered, never reachable through Loft, whatever the account can open
    # (case-insensitive). For server or backup shares that should never reach a laptop.
    blocked_shares: list[str] = field(default_factory=list)
    # Folders inside allowed shares that never reach a device ("share/path", whole
    # subtree, case- and NFC-insensitive). Folders above a blocked path can't be renamed,
    # moved or deleted from a device.
    blocked_paths: list[str] = field(default_factory=list)
    # Accounts allowed to sign in (full match, case-insensitive). The gateway holds the
    # password of an active user in memory (the residual risk of signing in as the user).
    allowed_users_re: str = r"[A-Za-z0-9._-]+"
    # Refused even when allowed_users_re matches: service accounts that can open
    # everything (backup, replication) and shared machine accounts belong here.
    denied_users_re: str = r"guest"
    direct: Listener = field(default_factory=lambda: Listener("0.0.0.0", 8443))
    public: Listener = field(default_factory=lambda: Listener("127.0.0.1", 8080))
    # SPKI sha256 pins of the direct-route key (current + standby), served on
    # GET /v1/identity over the WebPKI-protected public route.
    direct_spki_pins: list[str] = field(default_factory=list)
    # What `loft-gateway pair` puts in the pairing code. lan_url: how devices on the home
    # network reach the direct listener (https://<address>:<port>). public_url: optional
    # outside address. public_mode: "none" (home network / VPN only), "tunnel" (a TLS-ending
    # proxy such as Cloudflare Tunnel forwards to the public listener; sign-in refused
    # there) or "direct" (a port forward to the direct listener: same pinned key, end to
    # end, so sign-in works from outside too).
    gateway_name: str = "Loft"
    lan_url: str = ""
    public_url: str = ""
    public_mode: str = "none"

    token_ttl_days: int = 90            # sliding: every refresh resets it
    token_rotate_grace_s: int = 120     # previous token stays valid this long
    # Many NAS boxes have no SMB lockout, so these limits may be the only brute-force
    # wall on the direct route (and on the internet, in public_mode "direct").
    login_max_fail_user: int = 5        # per NAS user per window
    login_max_fail_ip: int = 20
    login_window_s: int = 900
    # Sign-in codes: a device signed in on the direct route can mint a code that signs
    # in another device over the internet (e.g. for an app store reviewer). Only accounts
    # matching this may mint or redeem one; the empty default means nobody.
    code_users_re: str = ""
    code_max_uses: int = 10             # Apple reviews every update, often two reviewers
    code_ttl_days: int = 90

    rescan_interval_s: int = 300
    background_rescan: bool = True      # tests switch it off for determinism
    safe_save_window_s: int = 60        # delete-then-create inside this = one new version
    versions_keep: int = 10
    versions_days: int = 30
    trash_days: int = 30
    upload_chunk_max: int = 32 * 1024 * 1024
    upload_ttl_s: int = 24 * 3600
    longpoll_max_s: int = 25            # well under Cloudflare's 100 s
    session_idle_s: int = 1800          # cached SMB session per device
    # Thumbnails: decoded only in the sandboxed worker (thumbs.py, loft-thumb@.service).
    # "" = spawn the worker as a subprocess (tests, dev): NOT for production.
    thumb_socket: str = "/run/loft-thumb.sock"
    thumb_concurrency: int = 2          # fits a 2-CPU gateway
    thumb_cache_mb: int = 64

    @property
    def meta_dir(self) -> str:
        """Hidden area inside each chosen root (`<root>/.loft/`) for
        uploads/versions/trash. Never listed, refused as a name."""
        return ".loft"


def _listener(d: dict, default: Listener) -> Listener:
    # A top-level key written below [direct]/[public] lands in that table: refuse it
    # here rather than ignore it.
    unknown = set(d) - {"host", "port", "certfile", "keyfile"}
    if unknown:
        raise ValueError(f"unknown listener setting(s) {sorted(unknown)}: top-level keys "
                         "must come before the [direct] and [public] tables")
    return Listener(
        host=d.get("host", default.host),
        port=int(d.get("port", default.port)),
        certfile=d.get("certfile", default.certfile),
        keyfile=d.get("keyfile", default.keyfile),
    )


def load(path: str | None = None) -> Settings:
    path = path or os.environ.get("LOFT_CONFIG", "/etc/loft/gateway.toml")
    raw: dict = {}
    if Path(path).exists():
        raw = tomllib.loads(Path(path).read_text())
    s = Settings()
    for k, v in raw.items():
        if k in ("direct", "public"):
            setattr(s, k, _listener(v, getattr(s, k)))
        elif hasattr(s, k):
            setattr(s, k, v)
        else:
            raise ValueError(f"unknown setting {k!r} in {path}")
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir and (Path(cred_dir) / "master.key").exists():
        s.master_key_path = str(Path(cred_dir) / "master.key")
    validate(s)
    return s


def validate(s: Settings) -> None:
    if s.smb_encryption not in ("required", "server"):
        raise ValueError(f"smb_encryption must be 'required' or 'server', not {s.smb_encryption!r}")
    if s.public_mode not in ("none", "tunnel", "direct"):
        raise ValueError(f"public_mode must be 'none', 'tunnel' or 'direct', not {s.public_mode!r}")
    if s.public_mode != "none" and not s.public_url.startswith("https://"):
        raise ValueError("public_mode needs public_url = \"https://...\"")
