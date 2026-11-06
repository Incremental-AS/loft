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
    # Accounts that may connect assistants (MCP), on top of allowed_users_re (full match,
    # case-insensitive; denied_users_re still refuses). The default, ".*", is every account
    # that may sign in: connecting needs an approval code from a signed-in Loft app, and
    # the assistant opens only folders the user approves. Narrow it to name accounts;
    # empty: nobody. Taking an account out ends its connections at their next call.
    assistant_users_re: str = ".*"
    # An assistant approval code (Loft app > Connect an assistant) may be made away from
    # home too (the code itself is the approval; it works once, within 10 minutes).
    # False: only on the home network, like sign-in codes.
    assistant_codes_over_internet: bool = True
    # Wrong approval codes from everyone together, per login window. Each code also has
    # its own limit (login_max_fail_user), so this only stops a guessing swarm.
    assistant_code_max_fail_global: int = 500
    # The internet address assistants use (https://<host>, /mcp and OAuth under it), when
    # it isn't public_url: e.g. a friendlier hostname routed to the same public listener.
    # Empty: public_url. Pairing links keep public_url either way.
    assistant_url: str = ""
    # Where assistants may send the browser back after sign-in. Empty: any https host
    # (any MCP client may register). Set it, e.g. ["api.jeevo.ai"], to allow only those;
    # loopback (an app on the user's own computer) is always allowed.
    assistant_return_hosts: list[str] = field(default_factory=list)

    rescan_interval_s: int = 300
    background_rescan: bool = True      # tests switch it off for determinism
    # Change notify (watch.py): the NAS reports changes, the gateway re-reads only
    # those folders. The full rescan becomes the safety net, run every
    # rescan_interval_watched_s while every watch of a root is up and a pass has
    # started since; rescan_interval_s otherwise.
    notify: bool = True
    notify_debounce_s: float = 1.0      # gather a burst into one listing per folder
    notify_folder_min_s: float = 10.0   # a busy folder is re-read at most this often
    notify_move_grace_s: float = 3.0    # a missing item waits this long for its move's other half
    notify_overflow_rescan_s: int = 600 # NAS overflowed: rescan that share, this often at most
    # Same as rescan_interval_s for now. SMB notify doesn't see files written on the NAS
    # itself (its own web or phone uploads, jobs running on it), so the scan stays as
    # often as before until changes are detected on the NAS itself. 6 * 3600 was the plan.
    rescan_interval_watched_s: int = 300
    safe_save_window_s: int = 60        # delete-then-create inside this = one new version
    versions_keep: int = 10
    versions_days: int = 30
    trash_days: int = 30
    upload_chunk_max: int = 32 * 1024 * 1024
    upload_ttl_s: int = 24 * 3600
    longpoll_max_s: int = 25            # well under Cloudflare's 100 s
    session_idle_s: int = 1800          # cached SMB session per device
    # ...but kept while a rescan runs on it, up to this long after the device's last
    # request, so a full pass of a big root finishes on an idle Mac. The NAS password
    # stays in memory that much longer, at most.
    rescan_keeps_session_s: int = 3 * 3600
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
    if s.assistant_url and not s.assistant_url.startswith("https://"):
        raise ValueError("assistant_url must be \"https://...\" (or empty: public_url)")
    if s.public_mode != "none" and not s.public_url.startswith("https://"):
        raise ValueError("public_mode needs public_url = \"https://...\"")
