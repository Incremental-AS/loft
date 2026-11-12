"""Gateway core: devices + sessions, the index, the change feed, and every
write. The HTTP layer (api.py) is a thin shell around this class.

Credentials: crypto.py. Item ids, the change feed and safe-save: below.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import ipaddress
import logging
import re
import threading
import time
import unicodedata
import uuid
import weakref
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from . import crypto
from .config import Settings
from .db import DB
from .rootfs import (ALL_SHARES, Blocked, CrossShare, FixedItem, RootFS, is_blocked,
                     is_path_blocked, has_dot_parts)
from .nas_agent import NasAgent
from . import search as content
from .thumbs import Thumbnailer
from .watch import Watches
from .storage.base import (SFM_PRIVATE, AccessDenied, AuthFailed, Backend, Entry, Exists,
                           NotFound, Session, StorageError, WrongShareOwner, join, split,
                           to_nas)

log = logging.getLogger("loft.rescan")

# Names that never sync. The client excludes them too; the gateway
# hides them from listings and refuses uploads of them, so a client bug cannot
# put Office lock files or Finder metadata on the NAS.
EXCLUDED = ["~$*", ".~lock.*#", ".DS_Store", "._*", "Icon\r", "Thumbs.db",
            "desktop.ini", ".loft", ".loft-*"]
CHANGES_KEEP_S = 30 * 86400
HOUSEKEEPING_S = 3600
SLOW_READ_S = 60.0           # a folder read this slow is logged with where the time went
CORRECTIONS_LOGGED = 200     # per full pass: what a pass on an agent-covered root corrected


def utc(t: float | None) -> str:
    """A wall-clock time for the log, UTC to the millisecond."""
    if not t:
        return "-"
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_cost(st: dict, n: int) -> str:
    """Where a folder read's time went (reconcile's stats)."""
    return (f"{st.get('entries', 0)} entries; listing {st.get('list_s', 0):.1f} s,"
            f" moves {st.get('moves_s', 0):.1f} s, waiting for the index {st.get('wait_s', 0):.1f} s,"
            f" index {st.get('index_s', 0):.1f} s; {n} change(s)")

def rate_ip(ip: str | None) -> str | None:
    """The address that per-IP limits count: an IPv6 host gets a whole /64, so it counts
    as its /64 (review of af68b8f). An IPv4 client on a dual-stack listener
    (::ffff:a.b.c.d) counts as its IPv4 address. Audit and last_ip keep the full address."""
    if ip and ":" in ip:
        try:
            a = ipaddress.IPv6Address(ip)
        except ValueError:
            return ip
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{a}/64", strict=False).network_address) + "/64"
    return ip


UPLOAD_WRITE_PIECE = 4 << 20    # one SMB write; what an upload chunk holds in memory at once


def name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def is_excluded(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in EXCLUDED)


def valid_name(name: str) -> bool:
    # A Mac name holding the private code points themselves would come back from the
    # NAS as '|', ':' ... and stop matching its own row, so it is refused.
    return (bool(name) and name not in (".", "..") and len(to_nas(name).encode()) <= 255
            and not re.search(r'[/\\\x00]', name) and not SFM_PRIVATE & set(name)
            and not is_excluded(name))


# expires_at of a token that doesn't expire (an MCP connection's): 9999-12-31.
MCP_NEVER = 253402300799.0
ASSISTANT_ACCESS_TTL = 2 * 3600     # an OAuth access token; a long upload fits inside one
MCP_LIVE_ACCESS = 16                # live access tokens per OAuth connection (each device has its own)
ASSISTANT_CODE_TTL = 600            # an approval code


class ApiError(Exception):
    def __init__(self, status: int, code: str, **extra):
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


@dataclass
class Caller:
    device: dict
    session: Session
    ip: str | None
    route: str

    @property
    def user(self) -> str:
        return self.device["nas_user"]

    @property
    def device_id(self) -> str:
        return self.device["id"]


def etag_c(row) -> str:
    return f"c{row['content_version']}"


def etag_m(row) -> str:
    return f"m{row['meta_version']}"


def item_json(row, fixed: bool = False) -> dict:
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "name": row["name"],
        "type": "dir" if row["is_dir"] else "file",
        "size": row["size"],
        "mtime_ns": row["mtime_ns"],
        "content_etag": etag_c(row),
        "meta_etag": etag_m(row),
        "sha256": row["sha256"],
        # A share in an all-shares root: no rename, move or delete (Finder greys them out).
        "fixed": fixed,
    }


class Gateway:
    def __init__(self, settings: Settings, db: DB, backend: Backend, master_key: bytes):
        self.s, self.db, self.backend, self.master = settings, db, backend, master_key
        self._sessions: dict[str, tuple[Session, float]] = {}
        self._sess_lock = threading.Lock()
        self._scan_locks: dict[str, threading.Lock] = {}
        self._cond = threading.Condition()
        self._closing = False
        self.thumbs = Thumbnailer(settings.thumb_socket, settings.thumb_concurrency,
                                  settings.thumb_cache_mb)
        self._share_cache: dict = {}
        self.agent = NasAgent(self)
        self.watches = Watches(self)
        self._housekept: dict[str, float] = {}      # root id -> last housekeeping (monotonic)
        self._closed_sessions: weakref.WeakSet = weakref.WeakSet()   # rescans stop on these
        self._full_start: dict[str, float] = {}     # root id -> start of its last full pass
        self._scanning: dict[Session, int] = {}     # session -> rescans running on it
        self._owner_unread: set[tuple[str, str]] = set()   # logged once per process
        self._owner_audit: list[dict] = []           # mismatches seen inside a caller's tx
        self.search = content.Indexer(self)          # content search (search.py)
        if hasattr(backend, "share_guard"):          # SMB: check every new tree connect
            backend.share_guard = self.check_share_owner

    def check_share_owner(self, user: str, share: str, sid: str | None):
        """SMBSession's share guard. The first owner seen for (account, share) is kept;
        a different one later is refused with WrongShareOwner, before any listing,
        read or write in that share. A changed owner that is right (a NAS admin rebuilt
        the folder) is accepted with `loft-admin share-owner-forget USER SHARE`."""
        key = (user.casefold(), share.casefold())
        with self.db.tx() as c:
            nested = self.db._depth > 1              # inside this thread's own outer tx
            row = c.execute("SELECT owner_sid FROM share_owners WHERE nas_user=? AND share=?",
                            key).fetchone()
            if row is None and sid is not None:
                c.execute("INSERT INTO share_owners VALUES (?,?,?,?)", (*key, sid, time.time()))
        if row is None:
            if sid is not None:
                log.info("share %s (%s): owner %s noted", share, user, sid)
            elif key not in self._owner_unread:
                self._owner_unread.add(key)
                log.warning("share %s (%s): its owner can't be read; not checked", share, user)
            return
        if row["owner_sid"] != sid:
            # sid None with an owner noted before: the root was readable and no longer
            # is, which is itself a sign of another folder (review of 5fd6e34, S1).
            got = sid or "unreadable"
            log.error("share %s (%s): the NAS serves a folder owned by %s, not %s as before;"
                      " refused", share, user, got, row["owner_sid"])
            ev = dict(nas_user=user, share=share, expected=row["owner_sid"], got=got)
            if nested:
                # The refusal rolls the caller's tx back (an upload commit), and the
                # audit row with it; the janitor writes it instead (the monitor reads it).
                with self._sess_lock:
                    self._owner_audit.append(ev)
            else:
                self.db.audit("share_owner_mismatch", **ev)
            raise WrongShareOwner(f"{share}: the NAS serves another account's folder")

    def _flush_owner_audit(self):
        with self._sess_lock:
            evs, self._owner_audit = self._owner_audit, []
        for n, ev in enumerate(evs):
            try:
                self.db.audit("share_owner_mismatch", **ev)
            except Exception:
                # one failed write keeps it and the rest for the next pass
                # (review of 39aa1d8)
                log.exception("share_owner_mismatch audit not written; kept for the next pass")
                with self._sess_lock:
                    self._owner_audit[:0] = evs[n:]
                return

    def _stop_in_background(self):
        try:
            self._flush_owner_audit()       # queued mismatches, before the process ends
        finally:
            self.watches.stop_all()

    # ── devices, tokens, sessions ─────────────────────────────────────────

    def _check_rate(self, user: str, ip: str | None, first: str | None = None):
        cut = time.time() - self.s.login_window_s
        for k, cap in ((first or f"u:{user}", self.s.login_max_fail_user),
                       (f"ip:{rate_ip(ip)}", self.s.login_max_fail_ip)):
            n = self.db.one("SELECT COUNT(*) n FROM login_failures WHERE k=? AND at>?",
                            (k, cut))["n"]
            if n >= cap:
                raise ApiError(429, "too_many_attempts", retry_after=self.s.login_window_s)

    def login(self, user: str, password: str, device_name: str, ip: str | None,
              route: str) -> dict:
        if route != "direct":
            # api.py already refuses this on the public listener; this is the
            # second wall, so a routing mistake cannot open password login.
            raise ApiError(403, "password_login_direct_only")
        user = user.strip()
        if not user or not password:
            raise ApiError(400, "missing_credentials")
        if (not re.fullmatch(self.s.allowed_users_re, user, re.IGNORECASE)
                or re.fullmatch(self.s.denied_users_re, user, re.IGNORECASE)):
            self.db.audit("login_refused_account", nas_user=user, ip=ip, route=route)
            raise ApiError(403, "account_not_allowed")
        self._check_rate(user, ip)
        try:
            sess = self.backend.authenticate(user, password)
        except AuthFailed:
            self._record_failure(f"u:{user}", f"ip:{rate_ip(ip)}")
            self.db.audit("login_failed", nas_user=user, ip=ip, route=route)
            raise ApiError(401, "invalid_credentials")
        except StorageError as e:
            raise ApiError(503, "nas_unavailable", detail=str(e))
        return self._new_device(user, password, sess, device_name, ip, route, "login")

    def _record_failure(self, *keys: str):
        with self.db.tx() as c:
            now = time.time()
            c.executemany("INSERT INTO login_failures VALUES (?,?)", [(k, now) for k in keys])
            c.execute("DELETE FROM login_failures WHERE at<?", (now - 86400,))

    def _new_device(self, user: str, password: str, sess: Session, device_name: str,
                    ip: str | None, route: str, event: str, *, kind: str = "app",
                    read_only: bool = False, client_id: str | None = None, **detail) -> dict:
        device_id = uuid.uuid4().hex
        tok, secret = crypto.new_token(device_id)
        now = time.time()
        # A host-made MCP token lives in the assistant's settings and can't rotate, so it
        # doesn't expire: it lasts until it is revoked. (OAuth ones are re-keyed at once.)
        expires = MCP_NEVER if kind == "mcp" else now + self.s.token_ttl_days * 86400
        with self.db.tx() as c:
            c.execute("INSERT INTO devices (id, nas_user, device_name, token_hash, wrapped_cred,"
                      " created_at, last_used_at, last_ip, expires_at, kind, read_only, client_id)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (device_id, user, device_name[:100] or "Mac", crypto.token_hash(secret),
                       crypto.wrap_credential(self.master, secret, device_id, user, password),
                       now, now, ip, expires, kind, int(read_only), client_id))
        self._put_session(device_id, sess)
        self.db.audit(event, nas_user=user, device_id=device_id, ip=ip, route=route,
                      device_name=device_name, **detail)
        return {"device_id": device_id, "token": tok, "nas_user": user, "expires_at": expires}

    # ── assistants (MCP): connections, approval codes, OAuth tokens ──────
    #
    # An assistant connection is a device of kind 'mcp' on the user's whole drive (the
    # all-shares root). What it may open is narrowed by grants the user chooses
    # (mcp_grants, mcp.py). It is made either on the gateway host (`loft-gateway mcp
    # add`, a token that doesn't expire) or by the assistant signing in with OAuth
    # (oauth.py): the user approves on Loft's sign-in page with an approval code from a
    # Loft app or the gateway host, and the assistant gets a 2-hour access token and a
    # refresh token that doesn't rotate (refresh_oauth says why).

    def _assistant_user_ok(self, user: str) -> bool:
        """Its own setting (assistant_users_re), and the account must also be allowed to
        sign in at all; denied_users_re refuses either way."""
        return (bool(self.s.assistant_users_re)
                and bool(re.fullmatch(self.s.assistant_users_re, user, re.IGNORECASE))
                and bool(re.fullmatch(self.s.allowed_users_re, user, re.IGNORECASE))
                and not re.fullmatch(self.s.denied_users_re, user, re.IGNORECASE))

    def _nas_check(self, user: str, password: str, event: str, route: str) -> Session:
        try:
            return self.backend.authenticate(user, password)
        except AuthFailed:
            self.db.audit(event, nas_user=user, route=route)
            raise ApiError(401, "invalid_credentials")
        except StorageError as e:
            raise ApiError(503, "nas_unavailable", detail=str(e))

    def _whole_drive(self, device_id: str, sess: Session, route: str):
        d = dict(self.db.one("SELECT * FROM devices WHERE id=?", (device_id,)))
        self.set_root(Caller(d, sess, None, route), ALL_SHARES, "")

    def create_mcp_connection(self, user: str, password: str, name: str,
                              read_only: bool = False) -> dict:
        """A token for /mcp, made on the gateway host with the account's NAS password
        (never sent over the network). It doesn't expire; revoking ends it. It opens
        nothing until grants are set (mcp.set_access)."""
        user = user.strip()
        if not self._assistant_user_ok(user):
            raise ApiError(403, "account_not_allowed")
        sess = self._nas_check(user, password, "mcp_connection_refused", "cli")
        out = self._new_device(user, password, sess, name, None, "cli", "mcp_connection_created",
                               kind="mcp", read_only=read_only)
        try:
            self._whole_drive(out["device_id"], sess, "cli")
        except Exception:
            self.revoke(out["device_id"], by_user=user)   # no half-made connection left behind
            raise
        out["read_only"] = read_only
        return out

    def _store_assistant_code(self, user: str, password: str, created_by: str, ip, route) -> dict:
        code, code_id, secret = crypto.new_assistant_code()
        now = time.time()
        with self.db.tx() as c:
            c.execute("DELETE FROM signin_codes WHERE purpose='assistant' AND expires_at<?", (now,))
            c.execute("INSERT INTO signin_codes (id, nas_user, code_hash, wrapped_cred, created_by,"
                      " created_at, expires_at, max_uses, purpose) VALUES (?,?,?,?,?,?,?,1,'assistant')",
                      (code_id, user, crypto.token_hash(secret),
                       crypto.wrap_credential(self.master, secret, "code:" + code_id, user, password),
                       created_by, now, now + ASSISTANT_CODE_TTL))
        self.db.audit("assistant_code_minted", nas_user=user, ip=ip, route=route, code_id=code_id,
                      created_by=created_by)
        return {"code": code, "nas_user": user, "expires_at": now + ASSISTANT_CODE_TTL}

    def mint_assistant_code(self, tok: str, ip: str | None, route: str) -> dict:
        """A signed-in Loft app makes a code that approves an assistant's sign-in to its
        account. Home network only unless `assistant_codes_over_internet`."""
        if route != "direct" and not self.s.assistant_codes_over_internet:
            raise ApiError(403, "codes_direct_only")
        d, secret, blob = self._device_for_token(tok)
        if d["kind"] != "app":
            raise ApiError(403, "token_not_for_this_api")
        if not crypto.hash_matches(secret, d["token_hash"]):
            raise ApiError(401, "token_superseded")
        if not self._assistant_user_ok(d["nas_user"]):
            raise ApiError(403, "account_not_allowed")
        try:
            pw = crypto.unwrap_credential(self.master, secret, d["id"], d["nas_user"], blob)
        except crypto.CryptoError:
            raise ApiError(401, "invalid_token")
        try:
            return self._store_assistant_code(d["nas_user"], pw, d["id"], ip, route)
        finally:
            pw = None  # noqa: F841

    def mint_assistant_code_cli(self, user: str, password: str) -> dict:
        user = user.strip()
        if not self._assistant_user_ok(user):
            raise ApiError(403, "account_not_allowed")
        sess = self._nas_check(user, password, "assistant_code_refused", "cli")
        self._close_session(sess)          # only proved the password (review of bef7c4a, N2)
        return self._store_assistant_code(user, password, "cli", None, "cli")

    def _check_acode_rate(self, code_id: str | None, ip: str | None):
        self._check_rate("", ip, first=f"acode:{code_id}" if code_id else None)
        n = self.db.one("SELECT COUNT(*) n FROM login_failures WHERE k='acode:*' AND at>?",
                        (time.time() - self.s.login_window_s,))["n"]
        if n >= self.s.assistant_code_max_fail_global:
            raise ApiError(429, "too_many_attempts", retry_after=self.s.login_window_s)

    def redeem_assistant_code(self, code: str, ip: str | None, route: str) -> tuple[str, str]:
        """(NAS user, password) for a live approval code, which is used up."""
        # Limits: per code (its first four characters), per IP, and a high one for
        # everyone together. A single shared counter let five wrong codes from anyone
        # lock every user out (review, S1).
        try:
            code_id, secret = crypto.parse_assistant_code(code)
        except crypto.CryptoError:
            self._check_acode_rate(None, ip)
            self._record_failure("acode:*", f"ip:{rate_ip(ip)}")
            raise ApiError(401, "invalid_code")
        self._check_acode_rate(code_id, ip)
        row = self.db.one("SELECT * FROM signin_codes WHERE id=? AND purpose='assistant'", (code_id,))
        if not row or not crypto.hash_matches(secret, row["code_hash"]):
            self._record_failure(f"acode:{code_id}", "acode:*", f"ip:{rate_ip(ip)}")
            self.db.audit("assistant_code_failed", ip=ip, route=route, code_id=code_id)
            raise ApiError(401, "invalid_code")
        user = row["nas_user"]
        if row["revoked_at"] or row["expires_at"] < time.time() or row["uses"] >= row["max_uses"]:
            raise ApiError(401, "code_expired")
        if not self._assistant_user_ok(user):
            raise ApiError(403, "account_not_allowed")
        try:
            pw = crypto.unwrap_credential(self.master, secret, "code:" + code_id, user,
                                          row["wrapped_cred"])
        except crypto.CryptoError:
            raise ApiError(401, "invalid_code")
        with self.db.tx() as c:   # used up, and the wrapped password with it
            n = c.execute("UPDATE signin_codes SET uses=uses+1, wrapped_cred=x'' WHERE id=?"
                          " AND uses<max_uses AND revoked_at IS NULL", (code_id,)).rowcount
        if not n:
            raise ApiError(401, "code_expired")
        self.db.audit("assistant_code_used", nas_user=user, ip=ip, route=route, code_id=code_id)
        return user, pw

    def _refresh_token(self, device_id: str) -> tuple[str, bytes]:
        tok, secret = crypto.new_token(device_id)
        return "nsr_" + tok[len(crypto.TOKEN_PREFIX):], secret

    def _assistant_tokens(self, d: dict, pw: str, now: float) -> tuple[dict, tuple]:
        """New access + refresh tokens for an OAuth connection, and the column values."""
        access, a_secret = crypto.new_token(d["id"])
        refresh, r_secret = self._refresh_token(d["id"])
        cols = (crypto.token_hash(a_secret),
                crypto.wrap_credential(self.master, a_secret, d["id"], d["nas_user"], pw),
                now + ASSISTANT_ACCESS_TTL,
                crypto.token_hash(r_secret),
                crypto.wrap_credential(self.master, r_secret, "rt:" + d["id"], d["nas_user"], pw),
                now + self.s.token_ttl_days * 86400)
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                "expires_in": ASSISTANT_ACCESS_TTL}, cols

    def create_oauth_connection(self, user: str, pw: str, client_id: str, client_name: str,
                                ip: str | None, route: str) -> dict:
        """The token response for a sign-in the user approved (oauth.py)."""
        if not self._assistant_user_ok(user):
            raise ApiError(403, "account_not_allowed")
        sess = self._nas_check(user, pw, "nas_credential_rejected", route)
        out = self._new_device(user, pw, sess, client_name, ip, route, "assistant_signed_in",
                               kind="mcp", client_id=client_id)
        now = time.time()
        d = dict(self.db.one("SELECT * FROM devices WHERE id=?", (out["device_id"],)))
        resp, cols = self._assistant_tokens(d, pw, now)
        with self.db.tx() as c:
            c.execute("UPDATE devices SET token_hash=?, wrapped_cred=?, expires_at=?, refresh_hash=?,"
                      " refresh_wrapped=?, refresh_expires_at=?, client_id=? WHERE id=?",
                      (*cols, client_id, d["id"]))
        try:
            self._whole_drive(d["id"], sess, route)
        except Exception:
            self.revoke(d["id"], by_user=user)
            raise
        return resp

    def refresh_oauth(self, refresh: str, client_id: str, ip: str | None, route: str) -> dict:
        """A new access token for an OAuth connection. The refresh token doesn't rotate:
        the response carries the same one, and it lasts token_ttl_days from its last use.
        Jeevo syncs one connection to all of a user's devices and each refreshes by
        itself, so rotation with reuse detection would end the connection the first time
        a device refreshed with a token another device had already used (docs/mcp.md).
        For the same reason the access tokens it replaces keep working until their own
        expiry (oauth_access), at most MCP_LIVE_ACCESS of them."""
        if not refresh.startswith("nsr_"):
            raise ApiError(400, "invalid_grant")
        try:
            device_id, secret = crypto.parse_token(crypto.TOKEN_PREFIX + refresh[4:])
        except crypto.CryptoError:
            raise ApiError(400, "invalid_grant")
        d = self.db.one("SELECT * FROM devices WHERE id=? AND kind='mcp'", (device_id,))
        now = time.time()
        if (not d or d["revoked_at"] or d["client_id"] != client_id
                or not crypto.hash_matches(secret, d["refresh_hash"])
                or (d["refresh_expires_at"] or 0) < now):
            raise ApiError(400, "invalid_grant")
        d = dict(d)
        try:
            pw = crypto.unwrap_credential(self.master, secret, "rt:" + d["id"], d["nas_user"],
                                          d["refresh_wrapped"])
        except crypto.CryptoError:
            raise ApiError(400, "invalid_grant")
        access, a_secret = crypto.new_token(d["id"])
        wrapped = crypto.wrap_credential(self.master, a_secret, d["id"], d["nas_user"], pw)
        pw = None  # noqa: F841
        with self.db.tx() as c:
            # The token being replaced is read inside the transaction: two refreshes at
            # once would otherwise both keep the same stale one, and the first one's new
            # token would end up in neither place (review of af68b8f).
            c.execute("INSERT INTO oauth_access (device_id, token_hash, wrapped_cred, expires_at)"
                      " SELECT id, token_hash, wrapped_cred, expires_at FROM devices"
                      " WHERE id=? AND expires_at>? AND revoked_at IS NULL", (d["id"], now))
            c.execute("DELETE FROM oauth_access WHERE device_id=? AND (expires_at<=? OR rowid NOT IN"
                      " (SELECT rowid FROM oauth_access WHERE device_id=? ORDER BY expires_at DESC"
                      " LIMIT ?))", (d["id"], now, d["id"], MCP_LIVE_ACCESS - 1))
            if not c.execute("UPDATE devices SET token_hash=?, wrapped_cred=?, expires_at=?,"
                             " refresh_expires_at=? WHERE id=? AND revoked_at IS NULL",
                             (crypto.token_hash(a_secret), wrapped, now + ASSISTANT_ACCESS_TTL,
                              now + self.s.token_ttl_days * 86400, d["id"])).rowcount:
                raise ApiError(400, "invalid_grant")      # signed out meanwhile
        self.db.audit("token_refresh", nas_user=d["nas_user"], device_id=d["id"], ip=ip, route=route)
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                "expires_in": ASSISTANT_ACCESS_TTL}

    # ── sign-in codes (App Review) ────────────────────────────────────────

    def _code_user_ok(self, user: str) -> bool:
        return bool(self.s.code_users_re) and bool(
            re.fullmatch(self.s.code_users_re, user, re.IGNORECASE)) and not re.fullmatch(
            self.s.denied_users_re, user, re.IGNORECASE)

    def mint_code(self, tok: str, ip: str | None, route: str, max_uses: int | None,
                  days: int | None) -> dict:
        """A device signed in on the DIRECT route makes a code for its own account.
        The code carries that device's NAS credential, re-wrapped under the code."""
        if route != "direct":
            raise ApiError(403, "codes_direct_only")
        d, secret, blob = self._device_for_token(tok)
        if d["kind"] != "app":
            raise ApiError(403, "token_not_for_this_api")
        if not crypto.hash_matches(secret, d["token_hash"]):
            raise ApiError(401, "token_superseded")
        user = d["nas_user"]
        if not self._code_user_ok(user):
            self.db.audit("code_refused_account", nas_user=user, device_id=d["id"], ip=ip,
                          route=route)
            raise ApiError(403, "account_not_allowed")
        try:
            pw = crypto.unwrap_credential(self.master, secret, d["id"], user, blob)
        except crypto.CryptoError:
            raise ApiError(401, "invalid_token")
        uses = max(1, min(max_uses or self.s.code_max_uses, self.s.code_max_uses))
        ttl = max(1, min(days or self.s.code_ttl_days, self.s.code_ttl_days)) * 86400
        code, code_id, csecret = crypto.new_signin_code()
        now = time.time()
        with self.db.tx() as c:
            c.execute("INSERT INTO signin_codes (id, nas_user, code_hash, wrapped_cred, created_by,"
                      " created_at, expires_at, max_uses) VALUES (?,?,?,?,?,?,?,?)",
                      (code_id, user, crypto.token_hash(csecret),
                       crypto.wrap_credential(self.master, csecret, "code:" + code_id, user, pw),
                       d["id"], now, now + ttl, uses))
        pw = None  # noqa: F841
        self.db.audit("code_minted", nas_user=user, device_id=d["id"], ip=ip, route=route,
                      code_id=code_id, max_uses=uses)
        return {"code": code, "code_id": code_id, "nas_user": user, "max_uses": uses,
                "expires_at": now + ttl}

    def redeem_code(self, code: str, device_name: str, ip: str | None, route: str) -> dict:
        """Sign a new device in with a code, on either route."""
        try:
            code_id, csecret = crypto.parse_signin_code(code)
        except crypto.CryptoError:
            self._check_rate("", ip, first="code:-")
            self._record_failure(f"ip:{rate_ip(ip)}")
            raise ApiError(401, "invalid_code")
        self._check_rate("", ip, first=f"code:{code_id}")
        row = self.db.one("SELECT * FROM signin_codes WHERE id=? AND purpose='device'", (code_id,))
        now = time.time()
        if not row or not crypto.hash_matches(csecret, row["code_hash"]):
            self._record_failure(f"code:{code_id}", f"ip:{rate_ip(ip)}")
            self.db.audit("code_failed", ip=ip, route=route, code_id=code_id)
            raise ApiError(401, "invalid_code")
        user = row["nas_user"]
        if row["revoked_at"] or row["expires_at"] < now or row["uses"] >= row["max_uses"]:
            self.db.audit("code_dead", nas_user=user, ip=ip, route=route, code_id=code_id)
            raise ApiError(401, "code_expired")
        if not self._code_user_ok(user):     # config narrowed since the code was made
            self.db.audit("code_refused_account", nas_user=user, ip=ip, route=route,
                          code_id=code_id)
            raise ApiError(403, "account_not_allowed")
        try:
            pw = crypto.unwrap_credential(self.master, csecret, "code:" + code_id, user,
                                          row["wrapped_cred"])
        except crypto.CryptoError:
            raise ApiError(401, "invalid_code")
        with self.db.tx() as c:   # claim a use first: two redemptions can't both take the last
            n = c.execute("UPDATE signin_codes SET uses=uses+1 WHERE id=? AND uses<max_uses"
                          " AND revoked_at IS NULL", (code_id,)).rowcount
        if not n:
            raise ApiError(401, "code_expired")
        try:
            sess = self.backend.authenticate(user, pw)
        except AuthFailed:
            self.db.audit("nas_credential_rejected", nas_user=user, ip=ip, route=route,
                          code_id=code_id)
            raise ApiError(401, "nas_credentials_invalid")
        except StorageError as e:
            raise ApiError(503, "nas_unavailable", detail=str(e))
        try:
            return self._new_device(user, pw, sess, device_name, ip, route, "code_login",
                                    code_id=code_id)
        finally:
            pw = None  # noqa: F841

    def revoke_code(self, code_id: str, by: str = "admin"):
        row = self.db.one("SELECT nas_user FROM signin_codes WHERE id=?", (code_id,))
        if not row:
            raise ApiError(404, "no_such_code")
        with self.db.tx() as c:
            c.execute("UPDATE signin_codes SET revoked_at=?, wrapped_cred=x'' WHERE id=?",
                      (time.time(), code_id))
        self.db.audit("code_revoked", nas_user=row["nas_user"], code_id=code_id, by=by)

    def _device_for_token(self, tok: str) -> tuple[dict, bytes, bytes]:
        """Return (device row, secret, wrapped credential that secret opens)."""
        try:
            device_id, secret = crypto.parse_token(tok)
        except crypto.CryptoError:
            raise ApiError(401, "invalid_token")
        d = self.db.one("SELECT * FROM devices WHERE id=?", (device_id,))
        now = time.time()
        if not d:
            raise ApiError(401, "invalid_token")
        if crypto.hash_matches(secret, d["token_hash"]):
            blob = d["wrapped_cred"]
        elif (d["prev_valid_until"] and d["prev_valid_until"] > now
                and crypto.hash_matches(secret, d["prev_token_hash"])):
            blob = d["prev_wrapped_cred"]
        elif d["kind"] == "mcp" and (a := self._earlier_access(d["id"], secret)):
            # an OAuth access token a refresh (maybe on another device) has replaced
            if d["revoked_at"]:
                raise ApiError(401, "device_revoked")
            if a["expires_at"] < now:
                raise ApiError(401, "token_expired")
            return dict(d), secret, a["wrapped_cred"]
        else:
            raise ApiError(401, "invalid_token")
        # Only a caller holding the real secret learns WHY it is dead, so the
        # app can say "This Mac was removed" vs "Sign-in expired"; a forged
        # token for a guessed device id stays invalid_token.
        if d["revoked_at"]:
            raise ApiError(401, "device_revoked")
        if d["expires_at"] < now:
            raise ApiError(401, "token_expired")
        return dict(d), secret, blob

    def _earlier_access(self, device_id: str, secret: bytes):
        for a in self.db.all("SELECT * FROM oauth_access WHERE device_id=?", (device_id,)):
            if crypto.hash_matches(secret, a["token_hash"]):
                return a
        return None

    def _close_session(self, sess: Session):
        self.watches.forget(sess)          # a watch never outlives its session
        try:
            self._closed_sessions.add(sess)
        except TypeError:                  # not weak-referenceable: rescans just run out
            pass
        sess.close()

    def session_live(self, sess: Session) -> bool:
        with self._sess_lock:
            return any(s is sess for s, _ in self._sessions.values())

    def session_for_user(self, user: str) -> Session | None:
        """A live NAS session of any non-revoked device of this account (the content
        indexer reads as the root's own account; it never signs in by itself)."""
        with self._sess_lock:
            ids = list(self._sessions)
        if not ids:
            return None
        mine = {r["id"] for r in self.db.all(
            "SELECT id FROM devices WHERE nas_user=? AND revoked_at IS NULL", (user,))}
        with self._sess_lock:
            for k, (sess, _) in self._sessions.items():
                if k in mine and sess not in self._closed_sessions:
                    return sess
        return None

    def _put_session(self, device_id: str, sess: Session):
        with self._sess_lock:
            old = self._sessions.pop(device_id, None)
            self._sessions[device_id] = (sess, time.time())
        if old:
            self._close_session(old[0])

    def _adopt_session(self, device_id: str, sess: Session) -> Session:
        """Keep the session this request made, unless another request for the same device
        made one first (parallel Range slices, or a Mac's burst, after a restart or 30 min
        idle). Then use that one and close ours, which nothing has used yet. Replacing it
        instead closed a session another request was reading through (2026-11-06)."""
        with self._sess_lock:
            hit = self._sessions.get(device_id)
            if hit is None:
                self._sessions[device_id] = (sess, time.time())
                return sess
        sess.close()                        # its own connection: the kept one is untouched
        return hit[0]

    def _drop_session(self, device_id: str):
        with self._sess_lock:
            old = self._sessions.pop(device_id, None)
        if old:
            self._close_session(old[0])

    def expire_sessions(self):
        """Run every minute (main._janitor). Drops idle sessions, and sessions of
        devices revoked from another process (`loft-admin revoke`, `loft-gateway mcp
        revoke`): that revoke can't reach this process's cache, so without this the
        session and its watches lived on until idle (review of bef7c4a, N1)."""
        self._flush_owner_audit()
        now = time.time()
        cut = now - self.s.session_idle_s
        # A full pass of a big root outlasts session_idle_s (ohh: ~75 min), and one
        # stopped half-way keeps nothing it deferred: a Mac left idle never finished
        # one (2026-11-06). A session with a rescan running stays up to
        # rescan_keeps_session_s after its last request.
        scan_cut = now - max(self.s.rescan_keeps_session_s, self.s.session_idle_s)
        with self._sess_lock:
            ids = list(self._sessions)
        known, live = set(ids), set()
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            live |= {r["id"] for r in self.db.all(
                "SELECT id FROM devices WHERE revoked_at IS NULL AND id IN (%s)"
                % ",".join("?" * len(part)), tuple(part))}
        with self._sess_lock:
            dead = [k for k, (s, t) in self._sessions.items()
                    if (k in known and k not in live)
                    or (t < cut and (t < scan_cut or s not in self._scanning))]
            gone = [self._sessions.pop(k)[0] for k in dead]
        for s in gone:
            self._close_session(s)

    def authenticate(self, tok: str, ip: str | None, route: str, kind: str = "app") -> Caller:
        d, secret, blob = self._device_for_token(tok)
        if d["kind"] != kind:
            # An MCP token never opens the REST API, and an app token never opens /mcp.
            raise ApiError(403, "token_not_for_this_api")
        if kind == "mcp" and not self._assistant_user_ok(d["nas_user"]):
            # the account was taken out of assistant_users_re after it connected
            raise ApiError(403, "account_not_allowed")
        with self._sess_lock:
            hit = self._sessions.get(d["id"])
            if hit:
                self._sessions[d["id"]] = (hit[0], time.time())
        sess = hit[0] if hit else None
        if sess is None:
            try:
                pw = crypto.unwrap_credential(self.master, secret, d["id"], d["nas_user"], blob)
            except crypto.CryptoError:
                raise ApiError(401, "invalid_token")
            try:
                sess = self.backend.authenticate(d["nas_user"], pw)
            except AuthFailed:
                # NAS password changed or account disabled: this device's stored
                # credential is dead. The user must sign in again (direct route).
                self.db.audit("nas_credential_rejected", nas_user=d["nas_user"],
                              device_id=d["id"], ip=ip, route=route)
                raise ApiError(401, "nas_credentials_invalid")
            except StorageError as e:
                raise ApiError(503, "nas_unavailable", detail=str(e))
            finally:
                pw = None  # noqa: F841
            sess = self._adopt_session(d["id"], sess)
        with self.db.tx() as c:
            c.execute("UPDATE devices SET last_used_at=?, last_ip=? WHERE id=?",
                      (time.time(), ip, d["id"]))
        return Caller(d, sess, ip, route)

    def refresh(self, tok: str, ip: str | None, route: str) -> dict:
        d, secret, blob = self._device_for_token(tok)
        if d["kind"] != "app":
            raise ApiError(403, "token_not_for_this_api")
        if not crypto.hash_matches(secret, d["token_hash"]):
            raise ApiError(401, "token_superseded")   # only the current token may rotate
        try:
            pw = crypto.unwrap_credential(self.master, secret, d["id"], d["nas_user"], blob)
        except crypto.CryptoError:
            raise ApiError(401, "invalid_token")
        new_tok, new_secret = crypto.new_token(d["id"])
        now = time.time()
        with self.db.tx() as c:
            c.execute("UPDATE devices SET token_hash=?, wrapped_cred=?, prev_token_hash=?,"
                      " prev_wrapped_cred=?, prev_valid_until=?, expires_at=? WHERE id=?",
                      (crypto.token_hash(new_secret),
                       crypto.wrap_credential(self.master, new_secret, d["id"], d["nas_user"], pw),
                       d["token_hash"], d["wrapped_cred"], now + self.s.token_rotate_grace_s,
                       now + self.s.token_ttl_days * 86400, d["id"]))
        pw = None  # noqa: F841
        self.db.audit("token_refresh", nas_user=d["nas_user"], device_id=d["id"], ip=ip,
                      route=route)
        return {"device_id": d["id"], "token": new_tok,
                "expires_at": now + self.s.token_ttl_days * 86400}

    def list_devices(self, user: str) -> list[dict]:
        rows = self.db.all("SELECT id, device_name, created_at, last_used_at, last_ip,"
                           " expires_at, revoked_at, kind, read_only FROM devices WHERE nas_user=?"
                           " ORDER BY created_at", (user,))
        # read_only is a boolean on the wire (schemas.Device), an integer in SQLite
        return [{**dict(r), "read_only": bool(r["read_only"])} for r in rows]

    def revoke(self, device_id: str, *, by_user: str | None = None, ip=None, route=None):
        d = self.db.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if not d or (by_user is not None and d["nas_user"] != by_user):
            raise ApiError(404, "no_such_device")
        with self.db.tx() as c:
            # Wipe the credential, not just flag the row: a revoked device must
            # leave nothing behind that could ever decrypt to the password.
            c.execute("UPDATE devices SET revoked_at=?, wrapped_cred=x'', prev_wrapped_cred=NULL,"
                      " prev_token_hash=NULL, prev_valid_until=NULL, refresh_hash=NULL,"
                      " refresh_wrapped=NULL WHERE id=?",
                      (time.time(), device_id))
            c.execute("DELETE FROM oauth_access WHERE device_id=?", (device_id,))
        self._drop_session(device_id)
        self.db.audit("device_revoked", nas_user=d["nas_user"], device_id=device_id, ip=ip,
                      route=route, by=by_user or "admin")

    # ── roots ─────────────────────────────────────────────────────────────

    def _offered(self, caller: Caller) -> list[str]:
        fake = {"share": ALL_SHARES, "path": ""}
        try:
            return RootFS(fake, caller.session, self.s.blocked_shares, self.s.shares,
                          self._share_cache).shares()
        except StorageError as e:
            raise ApiError(503, "nas_unavailable", detail=str(e))

    def browse(self, caller: Caller, share: str | None, path: str) -> dict:
        if not share:
            return {"shares": self._offered(caller)}
        if has_dot_parts(path) or "\\" in path:
            raise ApiError(400, "bad_path")
        if share not in self._offered(caller):
            raise ApiError(404, "no_such_share")
        if is_path_blocked(share, path, self.s.blocked_paths):
            raise ApiError(404, "not_found")
        try:
            ents = caller.session.scandir(share, path)
        except NotFound:
            raise ApiError(404, "not_found")
        except AccessDenied:
            raise ApiError(403, "access_denied")
        dirs = sorted(e.name for e in ents if e.is_dir and not is_excluded(e.name)
                      and not is_path_blocked(share, join(path, e.name), self.s.blocked_paths))
        return {"share": share, "path": path.strip("/"), "folders": dirs}

    def set_root(self, caller: Caller, share: str, path: str) -> dict:
        """`share == "*"`: every share the account can open, as top-level folders."""
        path = path.strip("/")
        if has_dot_parts(path) or "\\" in path:
            raise ApiError(400, "bad_path")
        if share == ALL_SHARES:
            path = ""
        elif share not in self._offered(caller):
            raise ApiError(404, "no_such_share")
        elif is_path_blocked(share, path, self.s.blocked_paths):
            raise ApiError(404, "not_found")
        try:
            if share == ALL_SHARES:
                pass
            elif path:
                if not caller.session.stat(share, path).is_dir:
                    raise ApiError(400, "not_a_folder")
            elif not caller.session.can_access(share):
                raise ApiError(403, "access_denied")
        except NotFound:
            raise ApiError(404, "not_found")
        except AccessDenied:
            raise ApiError(403, "access_denied")
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM roots WHERE nas_user=? AND share=? AND path=?",
                          (caller.user, share, path)).fetchone()
            if not r:
                rid = uuid.uuid4().hex
                c.execute("INSERT INTO roots (id, nas_user, share, path, epoch, created_at)"
                          " VALUES (?,?,?,?,?,?)",
                          (rid, caller.user, share, path, uuid.uuid4().hex[:12], time.time()))
                c.execute("INSERT INTO items (id, root_id, parent_id, name, name_key, is_dir)"
                          " VALUES (?,?,NULL,?,?,1)",
                          (rid, rid, "Loft" if share == ALL_SHARES else split(path)[1] or share,
                           ""))
                r = c.execute("SELECT * FROM roots WHERE id=?", (rid,)).fetchone()
            c.execute("UPDATE devices SET root_id=? WHERE id=?", (r["id"], caller.device_id))
        caller.device["root_id"] = r["id"]
        self.db.audit("set_root", nas_user=caller.user, device_id=caller.device_id,
                      ip=caller.ip, route=caller.route, share=share, path=path)
        return self.me(caller)

    def _root(self, caller: Caller):
        rid = caller.device.get("root_id")
        r = rid and self.db.one("SELECT * FROM roots WHERE id=?", (rid,))
        if not r:
            raise ApiError(409, "no_root_selected")
        return r

    def _fs(self, root, session: Session) -> RootFS:
        return RootFS(root, session, self.s.blocked_shares, self.s.shares, self._share_cache,
                      self.s.blocked_paths)

    def _json(self, root, row) -> dict:
        # fixed: the all-shares root itself and each share directly under it
        return item_json(row, fixed=(root["share"] == ALL_SHARES
                                     and root["id"] in (row["id"], row["parent_id"])))

    def me(self, caller: Caller) -> dict:
        out = {"nas_user": caller.user, "device_id": caller.device_id,
               "device_name": caller.device["device_name"], "route": caller.route,
               "root": None}
        rid = caller.device.get("root_id")
        r = rid and self.db.one("SELECT * FROM roots WHERE id=?", (rid,))
        if r:
            out["root"] = {"id": r["id"], "share": r["share"], "path": r["path"],
                           "root_item_id": r["id"], "anchor": self.latest_anchor(r)}
        return out

    # ── index helpers ─────────────────────────────────────────────────────

    def _item(self, root, item_id: str, *, live=True):
        it = self.db.one("SELECT * FROM items WHERE id=? AND root_id=?", (item_id, root["id"]))
        if not it or (live and it["deleted"]):
            raise ApiError(404, "not_found")
        return it

    def _relpath(self, root, item) -> str:
        """Share-relative path of an item (walks the parent chain)."""
        parts = []
        cur = item
        while cur["parent_id"] is not None:
            parts.append(cur["name"])
            cur = self.db.one("SELECT * FROM items WHERE id=?", (cur["parent_id"],))
        return join(root["path"], *reversed(parts))

    def _record(self, c, root_id: str, item_id: str, kind: str):
        seq = c.execute("INSERT INTO changes (root_id, item_id, kind, at) VALUES (?,?,?,?)",
                        (root_id, item_id, kind, time.time())).lastrowid
        c.execute("UPDATE items SET updated_seq=? WHERE id=?", (seq, item_id))

    def _notify(self):
        with self._cond:
            self._cond.notify_all()
        self.search.wake()

    def release_waiters(self):
        """Shutting down: answer every open long-poll now (with whatever it has), so
        uvicorn isn't left waiting up to longpoll_max_s for them to close."""
        self._closing = True
        self._notify()
        # Closing the watches is a round trip per share: not in the signal handler.
        threading.Thread(target=self._stop_in_background, daemon=True).start()

    def _new_item(self, c, root_id, parent_id, e: Entry, sha256=None) -> str:
        iid = uuid.uuid4().hex
        c.execute("INSERT INTO items (id, root_id, parent_id, name, name_key, is_dir, size,"
                  " mtime_ns, file_id, sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (iid, root_id, parent_id, e.name, name_key(e.name), int(e.is_dir),
                   e.size, e.mtime_ns, e.file_id, sha256))
        self._record(c, root_id, iid, "upsert")
        return iid

    def _mark_deleted(self, c, root_id, item_id, superseded_by=None):
        """Mark an item and every live descendant deleted, one change each."""
        stack = [item_id]
        while stack:
            iid = stack.pop()
            stack += [r["id"] for r in c.execute(
                "SELECT id FROM items WHERE parent_id=? AND deleted=0", (iid,))]
            c.execute("UPDATE items SET deleted=1, superseded_by=? WHERE id=?",
                      (superseded_by if iid == item_id else None, iid))
            self.search.forget(c, iid)        # out of the content index with it
            self._record(c, root_id, iid, "delete")

    # ── reconcile: the NAS is the truth, the index follows ────────────────

    def reconcile(self, root, session: Session, start_id: str | None = None,
                  max_depth: int | None = None, defer: dict | None = None,
                  report: list | None = None, stats: dict | None = None) -> int:
        """Walk the NAS under `start_id` (default: root), bring the index in line,
        and record a change for every difference. Returns the number of changes.

        report: a list that gets one dict per change made (kind new | modified |
        moved | deleted | file_id, path, the row before and the NAS after), for the
        caller to log. stats: filled with where the time went (entries, list_s,
        moves_s, wait_s for the index lock, index_s).

        Items that vanished from one path and appear at another with the same
        server file id are MOVES (same item id, meta_version+1), not
        delete+create, so an external rename in Finder-over-SMB keeps the
        File Provider identity and any local state attached to it.

        With max_depth, an entry new to this folder whose file id belongs to an item
        elsewhere in the root is that item, moved here, if its old path is gone (one
        stat to check). `defer` (rescan() only): an item missing here is not deleted
        but put in `defer`, since a folder listed later may be where it went.

        A subfolder that can't be read for any reason but "gone" or "denied" (a
        timeout, a lock, a dropped connection) leaves its index rows alone.
        """
        start_id = start_id or root["id"]
        fs = self._fs(root, session)
        start = self.db.one("SELECT * FROM items WHERE id=?", (start_id,))
        start_path = self._relpath(root, start)
        # The walk below runs outside the transaction, so a commit, rename or delete
        # can land between it and step 2. Every write records a change, so any row
        # whose updated_seq is past this point is newer than our listing: the
        # listing has no say over it (it would delete a file saved a moment ago,
        # roll back a modify, or undo a rename). The next pass sees the truth.
        seq0 = self.db.one("SELECT COALESCE(MAX(seq), 0) s FROM changes")["s"]
        st = stats if stats is not None else {}
        t_list = time.monotonic()

        # 1. the NAS side, breadth-first, shallowest first
        found: list[tuple[str, int, Entry]] = []     # (parent rel-path, depth, entry)
        unread: set[str] = set()                     # rel-paths whose rows stay as they are
        queue = deque([("", 1)])
        while queue:
            rel, depth = queue.popleft()
            try:
                ents = fs.scandir(join(start_path, rel))
            except AccessDenied:
                # The walk's own start folder unreadable is NOT an empty folder:
                # read that way, losing access to the Loft folder deleted every
                # item on every Mac. Raise and leave the index alone instead.
                if not rel:
                    raise
                continue
            except NotFound:
                if not rel and start_id == root["id"]:
                    raise
                if not rel and defer is not None:
                    return 0             # rescan: the folder's own parent decides about it
                continue
            except StorageError:
                if not rel:
                    raise
                unread.add(rel)
                continue
            for e in ents:
                if is_excluded(e.name):
                    continue
                found.append((rel, depth, e))
                if e.is_dir and (max_depth is None or depth < max_depth):
                    queue.append((join(rel, e.name), depth + 1))

        t_moves = time.monotonic()
        st.update(entries=len(found), list_s=t_moves - t_list)
        at_multi_root = root["share"] == ALL_SHARES and start_id == root["id"]
        start_share = "" if at_multi_root else self._share_key(self.db.one, root, start_id)
        xmoves = (self._cross_folder_moves(root, fs, start_id, start_share, found)
                  if max_depth and not at_multi_root else {})
        unread_keys = {"/".join(name_key(x) for x in u.split("/")) for u in unread}

        def rep(kind: str, path: str, old: dict | None, e: Entry | None, **kw):
            if report is not None:
                report.append({"kind": kind, "path": join(start_path, path),
                               "old": {k: old[k] for k in ("id", "size", "mtime_ns", "file_id",
                                                           "updated_seq", "is_dir")}
                               if old else None,
                               "new": {"size": e.size, "mtime_ns": e.mtime_ns,
                                       "file_id": e.file_id, "is_dir": e.is_dir} if e else None,
                               **kw})

        n = 0
        t_wait = time.monotonic()
        st["moves_s"] = t_wait - t_moves
        with self.db.tx() as c:
            t_index = time.monotonic()
            st["wait_s"] = t_index - t_wait
            # 2. the index side: live descendants of start within max_depth
            items: dict[str, dict] = {}
            frontier, depth = [start_id], 0
            while frontier and (max_depth is None or depth < max_depth):
                depth += 1
                rows = c.execute(
                    f"SELECT * FROM items WHERE deleted=0 AND parent_id IN "
                    f"({','.join('?' * len(frontier))})", frontier).fetchall()
                for r in rows:
                    items[r["id"]] = dict(r)
                frontier = [r["id"] for r in rows if r["is_dir"]]
            by_parent: dict[tuple, str] = {(r["parent_id"], r["name_key"]): i
                                           for i, r in items.items()}
            seen: set[str] = set()
            dir_ids = {"": start_id}

            # Rows written since seq0 = rows with a change since seq0 (_record sets
            # updated_seq to its change's seq). Asked through the changes index:
            # items has none on updated_seq, so this was a scan of every row in the
            # index, once per folder listed.
            fresh = c.execute("SELECT id, parent_id, name_key, file_id, is_dir FROM items WHERE id IN"
                              " (SELECT item_id FROM changes WHERE root_id=? AND seq>?)",
                              (root["id"], seq0)).fetchall()
            fresh_ids = {r["id"] for r in fresh}
            fresh_names = {(r["parent_id"], r["name_key"]) for r in fresh}
            q = lambda sql, a: c.execute(sql, a).fetchone()           # noqa: E731
            fresh_fids = {(self._share_key(q, root, r["id"]), r["file_id"], r["is_dir"])
                          for r in fresh if r["file_id"] is not None}

            def under_fresh(iid: str) -> bool:
                """The item or a folder above it was written after the listing."""
                while iid in items:
                    if iid in fresh_ids:
                        return True
                    iid = items[iid]["parent_id"]
                return iid in fresh_ids

            # Where each indexed item was relative to `start` BEFORE this pass,
            # and which (rel, name) pairs exist on the NAS now. An item is only
            # a move candidate if its old location is really gone.
            memo: dict[str, str] = {}

            def old_rel(iid: str) -> str:
                r = items[iid]
                if r["parent_id"] == start_id:
                    return r["name_key"]
                if iid not in memo:
                    memo[iid] = join(old_rel(r["parent_id"]), r["name_key"])
                return memo[iid]
            old_paths = {iid: old_rel(iid) for iid in items}

            def name_rel(iid: str) -> str:
                """old_rel in the names as listed (for report)."""
                parts, cur = [], items.get(iid)
                while cur is not None:
                    parts.append(cur["name"])
                    cur = items.get(cur["parent_id"])
                return join(*reversed(parts))

            # File ids are only unique within one share: on a NAS where every share
            # is its own btrfs subvolume, Samba hands out the raw inode number, so an
            # all-shares root has the same id in several shares (tens of thousands on
            # a large NAS). Every file-id match is keyed by share; an id that is
            # still ambiguous within its share matches nothing.
            def item_share(iid: str) -> str:
                return old_paths[iid].split("/")[0] if at_multi_root else start_share

            def ent_share(rel: str, e: Entry) -> str:
                return name_key((rel or e.name).split("/")[0]) if at_multi_root else start_share

            by_fid: dict[tuple, str | None] = {}
            for i, r in items.items():
                if r["file_id"] is not None:
                    key = (item_share(i), r["file_id"], r["is_dir"])
                    by_fid[key] = None if key in by_fid else i
            now_paths = {(join(name_key(rel) if rel else "", name_key(e.name)), e.is_dir)
                         for rel, _, e in found}

            for rel, _depth, e in found:
                pid = dir_ids.get(rel)
                if pid is None:          # parent was not a dir in the index (type flip)
                    continue
                k = name_key(e.name)
                iid = by_parent.get((pid, k))
                cur = items.get(iid) if iid else None
                if cur and cur["id"] in fresh_ids:
                    seen.add(cur["id"])  # keep it as its writer left it
                    if e.is_dir and cur["is_dir"]:
                        dir_ids[join(rel, e.name)] = cur["id"]
                    continue
                if cur is None and ((pid, k) in fresh_names
                                    or (ent_share(rel, e), e.file_id, int(e.is_dir)) in fresh_fids):
                    continue             # its row moved, was renamed or deleted since
                if cur and bool(cur["is_dir"]) != e.is_dir:
                    cur = None           # file <-> folder: old one goes, new one comes
                if cur is None and e.file_id is not None:
                    mid = by_fid.get((ent_share(rel, e), e.file_id, int(e.is_dir)))
                    if (mid and mid not in seen
                            and (old_paths[mid], bool(items[mid]["is_dir"])) not in now_paths):
                        moved = items[mid]
                        if report is not None:
                            rep("moved", join(rel, e.name), moved, e,
                                was=join(start_path, name_rel(mid)))
                        by_parent.pop((moved["parent_id"], moved["name_key"]), None)
                        c.execute("UPDATE items SET parent_id=?, name=?, name_key=?,"
                                  " meta_version=meta_version+1 WHERE id=?", (pid, e.name, k, mid))
                        moved.update(parent_id=pid, name=e.name, name_key=k,
                                     meta_version=moved["meta_version"] + 1)
                        by_parent[(pid, k)] = mid
                        self._record(c, root["id"], mid, "upsert")
                        n += 1
                        cur = moved
                if cur is None and not rel and (e.file_id, int(e.is_dir)) in xmoves:
                    xm = xmoves[(e.file_id, int(e.is_dir))]
                    row = c.execute("SELECT * FROM items WHERE id=? AND deleted=0", (xm,)).fetchone()
                    if row and xm not in fresh_ids and xm not in seen:
                        rep("moved", join(rel, e.name), dict(row), e, was="(another folder)")
                        c.execute("UPDATE items SET parent_id=?, name=?, name_key=?,"
                                  " meta_version=meta_version+1 WHERE id=?", (pid, e.name, k, xm))
                        cur = dict(row, parent_id=pid, name=e.name, name_key=k,
                                   meta_version=row["meta_version"] + 1)
                        items[xm] = cur
                        by_parent[(pid, k)] = xm
                        self._record(c, root["id"], xm, "upsert")
                        n += 1
                        if defer is not None:
                            defer.pop(xm, None)
                if cur is None:
                    iid = self._new_item(c, root["id"], pid, e)
                    items[iid] = dict(c.execute("SELECT * FROM items WHERE id=?",
                                                (iid,)).fetchone())
                    rep("new", join(rel, e.name), None, e, id=iid)
                    by_parent[(pid, k)] = iid
                    seen.add(iid)
                    n += 1
                else:
                    iid = cur["id"]
                    seen.add(iid)
                    upd = []
                    if not e.is_dir and (cur["size"] != e.size or cur["mtime_ns"] != e.mtime_ns):
                        upd.append(("content_version=content_version+1, size=?, mtime_ns=?,"
                                    " sha256=NULL", (e.size, e.mtime_ns)))
                    if cur["name"] != e.name:
                        upd.append(("name=?, meta_version=meta_version+1", (e.name,)))
                    if cur["file_id"] != e.file_id:
                        c.execute("UPDATE items SET file_id=? WHERE id=?", (e.file_id, iid))
                    for sql, args in upd:
                        c.execute(f"UPDATE items SET {sql} WHERE id=?", (*args, iid))
                    if upd:
                        self._record(c, root["id"], iid, "upsert")
                        n += 1
                        rep("modified", join(rel, e.name), cur, e)
                    elif cur["file_id"] != e.file_id:
                        rep("file_id", join(rel, e.name), cur, e)   # not a change: a move hint
                if e.is_dir:
                    dir_ids[join(rel, e.name)] = iid

            def under_unread(iid: str) -> bool:
                p = old_paths.get(iid)
                return p is not None and any(p.startswith(u + "/") for u in unread_keys)

            for iid, r in items.items():
                if iid not in seen and not under_fresh(iid) and not under_unread(iid):
                    still = c.execute("SELECT deleted FROM items WHERE id=?", (iid,)).fetchone()
                    if still and not still["deleted"]:
                        if defer is not None and r["file_id"] is not None:
                            defer[iid] = (r["parent_id"], r["name_key"], r["updated_seq"])
                            continue
                        self._mark_deleted(c, root["id"], iid)
                        n += 1
                        if report is not None:
                            rep("deleted", name_rel(iid), r, None)
            if start_id == root["id"] and max_depth is None:
                c.execute("UPDATE roots SET last_scan_at=? WHERE id=?", (time.time(), root["id"]))
        st["index_s"] = time.monotonic() - t_index
        if n:
            self._notify()
        return n

    def _share_key(self, q, root, item_id: str) -> str:
        """name_key of the share an item is in, for an all-shares root ("" for a
        one-share root, where every item is in the same share). q(sql, args) -> row."""
        if root["share"] != ALL_SHARES or item_id == root["id"]:
            return ""
        cur = q("SELECT id, parent_id, name_key FROM items WHERE id=?", (item_id,))
        while cur and cur["parent_id"] not in (None, root["id"]):
            cur = q("SELECT id, parent_id, name_key FROM items WHERE id=?", (cur["parent_id"],))
        return cur["name_key"] if cur and cur["parent_id"] == root["id"] else ""

    def _cross_folder_moves(self, root, fs: RootFS, start_id: str, start_share: str,
                            found) -> dict:
        """{(file_id, is_dir): item id} for entries new to `start_id` whose file id
        is an indexed item elsewhere IN THE SAME SHARE (no SMB move crosses a share;
        ids repeat across shares) that is no longer at its old path: moved in from
        there (Finder over SMB, a bot's mv). More than one such item: no match.
        Runs before the index transaction, since it may stat the NAS."""
        out: dict = {}
        above, cur = set(), start_id
        while cur:                        # never move a folder into its own subtree
            above.add(cur)
            r = self.db.one("SELECT parent_id FROM items WHERE id=?", (cur,))
            cur = r["parent_id"] if r else None
        for rel, _d, e in found:
            if rel or e.file_id is None:
                continue
            if self.db.one("SELECT 1 FROM items WHERE parent_id=? AND name_key=? AND deleted=0",
                           (start_id, name_key(e.name))):
                continue
            gone = []
            for cand in self.db.all("SELECT * FROM items WHERE root_id=? AND file_id=? AND is_dir=?"
                                    " AND deleted=0 AND parent_id IS NOT ?",
                                    (root["id"], e.file_id, int(e.is_dir), start_id)):
                if (cand["id"] in above or cand["parent_id"] is None
                        or self._share_key(self.db.one, root, cand["id"]) != start_share):
                    continue
                try:
                    there = fs.stat(self._relpath(root, cand))
                except NotFound:
                    there = None
                except (StorageError, Blocked, FixedItem):
                    gone = None           # can't tell: leave it for the next pass
                    break
                if there is None or there.file_id != e.file_id:
                    gone.append(cand["id"])
            if gone and len(gone) == 1:
                out[(e.file_id, int(e.is_dir))] = gone[0]
        return out

    def rescan(self, root, session: Session, start_id: str | None = None) -> dict:
        """_rescan, with the session marked as scanning: the janitor keeps it while
        the pass runs (expire_sessions, rescan_keeps_session_s)."""
        with self._sess_lock:
            self._scanning[session] = self._scanning.get(session, 0) + 1
        try:
            return self._rescan(root, session, start_id)
        finally:
            with self._sess_lock:
                n = self._scanning.pop(session) - 1
                if n:
                    self._scanning[session] = n

    def _rescan(self, root, session: Session, start_id: str | None = None) -> dict:
        """Whole root (or the subtree under start_id: a share whose change watch
        overflowed), one folder at a time, breadth-first: each folder is listed
        and committed on its own (reconcile, max_depth=1), so what it finds shows
        up on the Macs while the pass is still going, and a folder that fails is
        logged and skipped instead of ending the pass. Items missing from where
        they were are deleted at the end, unless a later folder turned out to be
        where they went (a move keeps its id)."""
        t0 = time.time()
        start_id = start_id or root["id"]
        whole = start_id == root["id"]
        # the NAS agent's log up to here is covered by this pass (agent_since below)
        seq0 = self.agent.max_seq() if whole and self.agent.enabled else None
        defer: dict[str, tuple] = {}
        st = {"folders": 0, "changes": 0, "skipped": 0}
        # On a root the NAS agent covers, notify and the agent should already have
        # told the index everything: whatever this pass still finds is a miss, logged
        # item by item with what the index had, so the cause can be found (F1 of
        # review_maja_ba880e0, 2026-11-10).
        covered = whole and self.agent_covers(root)
        rep: list | None = [] if covered else None
        logged = 0
        queue = deque([start_id])
        while queue:
            if self._closing:
                log.info("rescan %s stopped: gateway shutting down", root["id"][:8])
                return st
            if session in self._closed_sessions:
                # signed out, revoked or idle: every folder left would only fail
                # (review of 3027669, n2). The next request's session starts a new pass.
                log.info("rescan %s stopped: its session was closed", root["id"][:8])
                return st
            fid = queue.popleft()
            fst: dict = {}
            t_f = time.monotonic()
            try:
                nf = self.reconcile(root, session, fid, max_depth=1, defer=defer,
                                    report=rep, stats=fst)
                st["changes"] += nf
                secs = time.monotonic() - t_f
                if secs > SLOW_READ_S:
                    it = self.db.one("SELECT * FROM items WHERE id=?", (fid,))
                    log.warning("rescan %s (%s): folder %s took %.0f s (%s)", root["id"][:8],
                                root["nas_user"], (self._relpath(root, it) if it else fid[:8])
                                or "(root)", secs, read_cost(fst, nf))
                if rep:
                    logged = self._log_corrections(root, rep, logged)
                    rep.clear()
            except (StorageError, Blocked, FixedItem) as e:
                if rep:
                    rep.clear()                  # rolled back: nothing was corrected
                if fid == start_id:
                    raise
                st["skipped"] += 1
                log.warning("rescan %s: folder %s skipped, its index rows left as they were: %s",
                            root["id"][:8], fid[:8], e)
                continue
            st["folders"] += 1
            queue.extend(r["id"] for r in self.db.all(
                "SELECT id FROM items WHERE parent_id=? AND deleted=0 AND is_dir=1", (fid,))
                if r["id"] not in defer)
        st["changes"] += self.finish_deferred(root, defer, scanned=whole, report=rep)
        if rep:
            logged = self._log_corrections(root, rep, logged)
        if covered:
            st["corrected"] = logged
        if whole:
            self._full_start[root["id"]] = t0
            if seq0 is not None:
                with self.db.tx() as c:
                    c.execute("UPDATE roots SET agent_seq=MAX(agent_seq, ?), agent_since=?"
                              " WHERE id=?", (seq0, t0, root["id"]))
        st["seconds"] = round(time.time() - t0, 1)
        log.info("rescan %s (%s)%s done: %s", root["id"][:8], root["nas_user"],
                 "" if whole else f" from {start_id[:8]}", st)
        return st

    def finish_deferred(self, root, defer: dict, scanned: bool = False,
                        report: list | None = None) -> int:
        """Delete the items a pass found missing from their folder, unless they
        were found elsewhere since (a move) or written to. scanned: a whole-root
        pass ends here, so the root's last_scan_at is set in the same commit.
        report: as reconcile's."""
        gone = 0
        if not defer and not scanned:
            return 0
        deleted = []
        with self.db.tx() as c:
            for iid, (pid, k, useq) in defer.items():
                r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
                if (r and not r["deleted"] and r["parent_id"] == pid and r["name_key"] == k
                        and r["updated_seq"] == useq):
                    if report is not None:
                        deleted.append(dict(r))
                    self._mark_deleted(c, root["id"], iid)
                    gone += 1
            if scanned:
                c.execute("UPDATE roots SET last_scan_at=? WHERE id=?", (time.time(), root["id"]))
        for r in deleted:
            report.append({"kind": "deleted", "path": self._relpath(root, r),
                           "old": {k: r[k] for k in ("id", "size", "mtime_ns", "file_id",
                                                     "updated_seq", "is_dir")}, "new": None})
        if gone:
            self._notify()
        return gone

    def _log_corrections(self, root, report: list, logged: int) -> int:
        """Log what a full pass on an agent-covered root corrected (see _rescan): per
        item the index row before and the NAS now (size, mtime, file id), when the
        index last wrote the row, and when the agent last reported the item's folder
        (and whether this root had applied that report). Up to CORRECTIONS_LOGGED
        lines per pass; returns how many corrections there were so far."""
        now = time.time()
        user_key = name_key(root["nas_user"] or "")
        agent_seq = (self.db.one("SELECT agent_seq FROM roots WHERE id=?", (root["id"],))
                     or {"agent_seq": 0})["agent_seq"]
        for r in report:
            if r["kind"] == "file_id":
                continue                       # not a change (a move hint): not counted
            logged += 1
            if logged > CORRECTIONS_LOGGED:
                if logged == CORRECTIONS_LOGGED + 1:
                    log.warning("rescan %s (%s): more than %d corrections in this pass;"
                                " the rest only counted", root["id"][:8], root["nas_user"],
                                CORRECTIONS_LOGGED)
                continue
            old, new, path = r["old"], r["new"], r["path"]

            def ns(v):
                return utc(v / 1e9) if v else "-"
            if r["kind"] == "modified":
                what = (f"size {old['size']}->{new['size']},"
                        f" mtime {ns(old['mtime_ns'])}->{ns(new['mtime_ns'])},"
                        f" file_id {old['file_id']}->{new['file_id']}")
            elif r["kind"] == "deleted":
                what = (f"was size {old['size']}, mtime {ns(old['mtime_ns'])},"
                        f" file_id {old['file_id']}")
            else:                              # new, moved
                what = (f"size {new['size']}, mtime {ns(new['mtime_ns'])},"
                        f" file_id {new['file_id']}")
                if r["kind"] == "moved":
                    what = f"from {r.get('was')}, " + what
            if new and new["mtime_ns"]:
                what += f", listed {now - new['mtime_ns'] / 1e9:.0f} s after its mtime"
            if old:
                ch = self.db.one("SELECT at FROM changes WHERE seq=?", (old["updated_seq"],))
                what += (f"; row written {utc(ch['at']) if ch else '(change pruned)'}"
                         f" (seq {old['updated_seq']}, item {old['id'][:8]})")
            # the agent's side: its last report of the folder this item is in
            folder = path.rpartition("/")[0] if "/" in path else ""
            if root["share"] == ALL_SHARES:
                share, _, folder = folder.partition("/")
            else:
                share = root["share"]
            if share:
                a = self.db.one("SELECT seq, at FROM nas_changes WHERE share_key=? AND"
                                " user_key IN ('', ?) AND kind='dir' AND path=?"
                                " ORDER BY seq DESC LIMIT 1", (name_key(share), user_key, folder))
                what += (f"; agent last reported {folder or '(share root)'} at {utc(a['at'])}"
                         f" (log seq {a['seq']}, {'applied' if a['seq'] <= agent_seq else 'NOT applied yet'})"
                         if a else f"; agent never reported {folder or '(share root)'} (in the log's 30 days)")
            log.warning("rescan %s (%s): full pass corrected %s %s: %s", root["id"][:8],
                        root["nas_user"], r["kind"], path, what)
        return logged

    def agent_covers(self, root) -> bool:
        """The NAS agent's log is the complete record of this root (nas_agent.py)."""
        if not (self.s.notify and self.agent.enabled):
            return False
        rw = self.watches.get(root["id"])
        names = rw.share_names if rw is not None else None
        return self.agent.covers(root, set(names) if names is not None else None)

    def rescan_interval(self, root) -> float:
        """rescan_interval_agent_s (0: never) while the NAS agent covers the root.
        rescan_interval_watched_s while the NAS reports every change for this
        root over SMB: all its watches are up, and a full pass has started since
        they came up (so nothing from before the watch is still unseen).
        Otherwise the short interval, as before change notify."""
        if self.agent_covers(root):
            return self.s.rescan_interval_agent_s or float("inf")
        since = self.watches.since(root["id"]) if self.s.notify else None
        if since is not None and self._full_start.get(root["id"], 0) >= since:
            return self.s.rescan_interval_watched_s
        return self.s.rescan_interval_s

    def maybe_rescan(self, caller: Caller):
        """Start the root's change watches if they aren't running, and a full-root
        rescan in the background if the last one is stale. Both run as the calling
        user, so they never see more than that user can."""
        root = self._root(caller)
        if self.s.notify:
            try:
                self.watches.ensure(root, caller.session)
            except Exception:  # noqa: BLE001  (the rescan still covers it)
                log.exception("notify %s (%s): starting the watches failed", root["id"][:8],
                              root["nas_user"])
        if not self.s.background_rescan:
            return
        full = not (root["last_scan_at"]
                    and time.time() - root["last_scan_at"] < self.rescan_interval(root))
        # Housekeeping (trash, uploads, the change feed) ran after each full pass; with
        # the NAS agent there may be none for weeks, so it has its own clock too.
        house = time.monotonic() - self._housekept.get(root["id"], float("-inf")) \
            >= HOUSEKEEPING_S
        if not (full or house):
            return
        lock = self._scan_locks.setdefault(root["id"], threading.Lock())
        if not lock.acquire(blocking=False):
            return
        self._housekept[root["id"]] = time.monotonic()

        def run():
            try:
                if full:
                    self.rescan(root, caller.session)
                self.housekeeping(root, caller.session)
            except Exception:  # noqa: BLE001  (next request retries)
                log.exception("rescan %s (%s) failed; the next request retries",
                              root["id"][:8], root["nas_user"])
            finally:
                lock.release()
        threading.Thread(target=run, daemon=True, name=f"rescan-{root['id'][:8]}").start()

    # ── reads ─────────────────────────────────────────────────────────────

    def get_item(self, caller: Caller, item_id: str) -> dict:
        root = self._root(caller)
        return self._json(root, self._item(root, item_id))

    def children(self, caller: Caller, item_id: str, cursor: str | None, limit: int) -> dict:
        root = self._root(caller)
        parent = self._item(root, item_id)
        if not parent["is_dir"]:
            raise ApiError(400, "not_a_folder")
        if not cursor:                       # first page: make the listing true
            self.reconcile(root, caller.session, parent["id"], max_depth=1)
        rows = self.db.all("SELECT * FROM items WHERE parent_id=? AND deleted=0 AND name_key>?"
                           " ORDER BY name_key LIMIT ?", (parent["id"], cursor or "", limit + 1))
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [self._json(root, r) for r in rows],
                "next_cursor": rows[-1]["name_key"] if more else None}

    def latest_anchor(self, root) -> str:
        seq = self.db.one("SELECT COALESCE(MAX(seq), 0) s FROM changes WHERE root_id=?",
                          (root["id"],))["s"]
        return f"{root['epoch']}.{seq}"

    def _probe_root(self, caller: Caller, root):
        """One stat of the Loft folder per long-poll. The feed itself is index-only,
        so without this a Mac whose account lost the folder, or whose NAS is down
        behind a live gateway, would read "Up to date" instead of "No access" or
        "Can't reach the NAS"."""
        try:
            self._fs(root, caller.session).stat("" if root["share"] == ALL_SHARES
                                                else root["path"])
        except AccessDenied:
            raise ApiError(403, "access_denied")
        except NotFound:
            raise ApiError(404, "root_missing")
        except StorageError as e:
            raise ApiError(503, "nas_unavailable", detail=str(e))

    def changes(self, caller: Caller, anchor: str, limit: int = 500, wait: int = 0) -> dict:
        root = self._root(caller)
        try:
            epoch, seq_s = anchor.rsplit(".", 1)
            seq = int(seq_s)
        except ValueError:
            raise ApiError(400, "bad_anchor")
        if epoch != root["epoch"] or seq < root["min_seq"]:
            # File Provider: NSFileProviderError.syncAnchorExpired -> re-enumerate
            raise ApiError(410, "anchor_expired")
        self._probe_root(caller, root)
        deadline = time.time() + min(max(wait, 0), self.s.longpoll_max_s)
        while True:
            rows = self.db.all("SELECT seq, item_id, kind FROM changes WHERE root_id=? AND seq>?"
                               " ORDER BY seq LIMIT ?", (root["id"], seq, limit))
            if rows or time.time() >= deadline or self._closing:
                break
            with self._cond:
                self._cond.wait(timeout=min(5, max(0.05, deadline - time.time())))
        last: dict[str, str] = {}
        for r in rows:
            last[r["item_id"]] = r["kind"]
        updated, deleted = [], []
        for iid, kind in last.items():
            it = self.db.one("SELECT * FROM items WHERE id=?", (iid,))
            if kind == "delete" or not it or it["deleted"]:
                deleted.append(iid)
            else:
                updated.append(self._json(root, it))
        new_seq = rows[-1]["seq"] if rows else seq
        return {"updated": updated, "deleted": deleted, "anchor": f"{root['epoch']}.{new_seq}",
                "more": len(rows) == limit}

    def open_content(self, caller: Caller, item_id: str):
        """Return (item row after a freshness check, share, path)."""
        root = self._root(caller)
        it = self._item(root, item_id)
        if it["is_dir"]:
            raise ApiError(400, "is_a_folder")
        path = self._relpath(root, it)
        fs = self._fs(root, caller.session)
        try:
            st = fs.stat(path)
        except NotFound:
            self.reconcile(root, caller.session, it["parent_id"], max_depth=1)
            raise ApiError(404, "not_found")
        if st.size != it["size"] or st.mtime_ns != it["mtime_ns"]:
            self.reconcile(root, caller.session, it["parent_id"], max_depth=1)
            it = self._item(root, item_id)
        return (it, *fs.nas(path))

    # ── content search ────────────────────────────────────────────────────

    def content_hits(self, places: list, query: str, offset: int, limit: int,
                     deadline_s: float = 6.0) -> tuple[list, int | None]:
        """Hits of a content query, best first, each checked NOW for the caller it is
        found for. places = [(caller, root, start_item_id or None)]: the index roots to
        search, each with the caller (and so the NAS session) that must be able to open
        the hit, and optionally a folder the hit must be in. Returns ([(caller, root, item
        row, passage or None)], offset of the next candidate or None when there are none).

        The index only proposes. A hit is returned when its item is still live in that
        root, is below the start folder, and the caller's own NAS session can open it
        (stat); a changed file is re-read into the index and shown without a passage."""
        by_scope = {}
        for caller, root, start in places:
            by_scope.setdefault(root["id"], (caller, root, start))
        if not by_scope:
            return [], None
        deadline = time.monotonic() + deadline_s
        out: list = []
        while len(out) < limit:
            want = max(limit - len(out), 10) * 2
            rows = self.search.index.candidates(list(by_scope), query, offset, want)
            for r in rows:
                if len(out) >= limit or time.monotonic() > deadline:
                    return out, offset
                offset += 1
                caller, root, start = by_scope[r["scope"]]
                hit = self._check_hit(caller, root, start, r)
                if hit is not None:
                    it, fresh = hit
                    snip = self.search.index.passage(r["id"], query) if fresh else None
                    out.append((caller, root, it, snip))
            if len(rows) < want:
                return out, None
        return out, offset

    def _check_hit(self, caller: Caller, root, start: str | None, doc):
        """(item row, fresh) when the caller may open the item now, else None."""
        it = self.db.one("SELECT * FROM items WHERE id=? AND root_id=? AND deleted=0"
                         " AND is_dir=0", (doc["item_id"], root["id"]))
        if it is None:
            return None
        if start is not None and start != root["id"]:
            cur, ok = it, False
            while cur is not None and cur["parent_id"] is not None:
                if cur["parent_id"] == start:
                    ok = True
                    break
                cur = self.db.one("SELECT id, parent_id FROM items WHERE id=?",
                                  (cur["parent_id"],))
            if not ok:
                return None
        path = self._relpath(root, it)
        try:
            st = self._fs(root, caller.session).stat(path)
        except (NotFound, AccessDenied, Blocked, WrongShareOwner):
            return None
        except StorageError as e:
            raise ApiError(503, "nas_unavailable", detail=str(e))
        if st.is_dir:
            return None
        fresh = (st.size == it["size"] and st.mtime_ns == it["mtime_ns"]
                 and doc["content_version"] == it["content_version"])
        if not fresh and (st.size != it["size"] or st.mtime_ns != it["mtime_ns"]):
            self.reconcile(root, caller.session, it["parent_id"], max_depth=1)
            it = self.db.one("SELECT * FROM items WHERE id=? AND deleted=0", (it["id"],))
            if it is None:
                return None
            self.search.wake()
        return it, fresh

    def search_content(self, caller: Caller, query: str, cursor: str | None,
                       limit: int) -> dict:
        """GET /v1/search: files in the caller's root whose text has every word of the
        query (in any of its forms), best first, each checked against the caller's NAS
        rights now. complete: False while the root's documents are still being indexed."""
        root = self._root(caller)
        if not self.search.enabled:
            raise ApiError(404, "search_disabled")
        if not content.words_in(query):
            raise ApiError(400, "bad_query")
        try:
            offset = content.decode_cursor(cursor)
        except ValueError:
            raise ApiError(400, "bad_cursor")
        self.search.wake()
        hits, nxt = self.content_hits([(caller, root, None)], query, offset, limit)
        return {"items": [{**self._json(root, it), "snippet": snip}
                          for _c, _r, it, snip in hits],
                "next_cursor": content.encode_cursor(nxt) if nxt is not None else None,
                "complete": bool(root["last_scan_at"]) and self.search.caught_up(root["id"])}

    def thumbnail(self, caller: Caller, item_id: str, px: int) -> tuple[str, bytes, str]:
        """(format, bytes, content etag). Same checks as a download: the caller's root,
        blocked paths, a fresh stat with the caller's own NAS session."""
        it, share, path = self.open_content(caller, item_id)
        try:
            r = self.thumbs.get((it["id"], it["content_version"]), it["name"], it["size"], px,
                                lambda off, n: caller.session.read(share, path, off, n))
        except TimeoutError:
            raise ApiError(503, "busy")
        if r is None:
            raise ApiError(404, "no_thumbnail")
        return (*r, f"c{it['content_version']}")

    # ── uploads ───────────────────────────────────────────────────────────

    def _meta(self, root, kind: str, name: str, near: str | None) -> str:
        """Path in the root's own hidden area: `<root>/.loft/{uploads,versions,trash}/<name>`
        (per root: no share-level ACLs to maintain on the NAS). In an all-shares root, the area of the share that
        holds `near`, so the rename into or out of it stays inside one share."""
        return RootFS(root, None, self.s.blocked_shares, self.s.shares,
                      blocked_paths=self.s.blocked_paths).meta(kind, name, near)

    def upload_create(self, caller: Caller, size: int, sha256: str,
                      near_id: str | None = None) -> dict:
        """`near_id`: the parent (create) or the item (modify) the upload is for.
        Required in an all-shares root, to put the temp file in the right share."""
        root = self._root(caller)
        if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
            raise ApiError(400, "bad_upload")
        near = None
        if near_id:
            near = self._relpath(root, self._item(root, near_id))
        elif root["share"] == ALL_SHARES:
            raise ApiError(400, "near_id_required")
        uid = uuid.uuid4().hex
        temp = self._meta(root, "uploads", uid, near)
        fs = self._fs(root, caller.session)
        fs.makedirs(split(temp)[0])
        fs.create_empty(temp)
        now = time.time()
        with self.db.tx() as c:
            c.execute("INSERT INTO uploads VALUES (?,?,?,?,?,0,?,?,?)",
                      (uid, caller.device_id, root["id"], size, sha256, temp, now,
                       now + self.s.upload_ttl_s))
        return {"upload_id": uid, "received": 0, "size": size,
                "chunk_max": self.s.upload_chunk_max}

    def _upload(self, caller: Caller, uid: str):
        u = self.db.one("SELECT * FROM uploads WHERE id=? AND device_id=?",
                        (uid, caller.device_id))
        if not u or u["expires_at"] < time.time():
            raise ApiError(404, "no_such_upload")
        return u

    def upload_status(self, caller: Caller, uid: str) -> dict:
        u = self._upload(caller, uid)
        return {"upload_id": uid, "received": u["received"], "size": u["size"]}

    def upload_chunk(self, caller: Caller, uid: str, offset: int, data,
                     chunk_sha256: str | None) -> dict:
        """`data`: the bytes, or a seekable file holding them (the HTTP route spools a
        chunk to local disk instead of holding up to chunk_max in memory). It goes to
        the NAS in UPLOAD_WRITE_PIECE pieces. Bytes written past `received` by a chunk
        that then fails are overwritten by the next one, and commit checks the whole
        file's SHA-256 anyway."""
        if not hasattr(data, "read"):
            data = io.BytesIO(data)
        n = data.seek(0, io.SEEK_END)
        u = self._upload(caller, uid)
        if offset != u["received"]:
            # Resumable: the client re-reads `received` and continues from there.
            raise ApiError(409, "wrong_offset", received=u["received"])
        if n > self.s.upload_chunk_max or offset + n > u["size"]:
            raise ApiError(413, "chunk_too_large")
        if chunk_sha256:
            h = hashlib.sha256()
            data.seek(0)
            while piece := data.read(UPLOAD_WRITE_PIECE):
                h.update(piece)
            if h.hexdigest() != chunk_sha256.lower():
                raise ApiError(422, "chunk_hash_mismatch")
        fs = self._fs(self._root(caller), caller.session)
        data.seek(0)
        pos = 0
        while piece := data.read(UPLOAD_WRITE_PIECE):
            fs.write_at(u["temp_path"], offset + pos, piece)
            pos += len(piece)
        if n == 0:
            fs.write_at(u["temp_path"], offset, b"")
        with self.db.tx() as c:
            c.execute("UPDATE uploads SET received=? WHERE id=? AND received=?",
                      (offset + n, uid, offset))
        return {"upload_id": uid, "received": offset + n, "size": u["size"]}

    def _conflict_name(self, name: str, device_name: str) -> str:
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            stem, ext = name, ""
        stamp = datetime.now().strftime("%Y-%m-%d %H%M")
        dev = re.sub(r'[/\\:\x00]', "-", device_name)[:40]
        return f"{stem} (conflict, {dev}, {stamp})" + (f".{ext}" if ext else "")

    def _free_conflict_name(self, c, root, parent, name: str, device_name: str) -> str:
        """A conflict name not yet taken in `parent`. The stamp is per minute, so a
        second conflict on the same file from the same device within a minute gets
        " 2", " 3" ... instead of failing the commit with name_exists_on_nas."""
        base = self._conflict_name(name, device_name)
        stem, dot, ext = base.rpartition(".")
        if not dot or not stem or not stem.endswith(")"):
            stem, ext = base, ""
        cand, n = base, 1
        while c.execute("SELECT 1 FROM items WHERE root_id=? AND parent_id=? AND name_key=? AND deleted=0",
                        (root["id"], parent["id"], name_key(cand))).fetchone():
            n += 1
            cand = f"{stem[:-1]} {n})" + (f".{ext}" if ext else "")
        return cand

    def _version_out(self, c, caller: Caller, root, old, new_item_id: str):
        """Move `old`'s current file into the versions area, recorded under
        `new_item_id` (the same id for a plain modify; the successor's id when a
        safe-save replaced the item). Prunes to versions_keep / versions_days."""
        fs = self._fs(root, caller.session)
        vid = uuid.uuid4().hex
        src = self._relpath(root, old)
        vpath = self._meta(root, "versions", vid, src)
        fs.makedirs(split(vpath)[0])
        fs.rename(src, vpath)
        c.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)",
                  (vid, new_item_id, root["id"], old["content_version"], old["size"],
                   old["mtime_ns"], vpath, time.time()))
        return (src, vpath)

    def _prune_versions(self, caller: Caller, root, item_id: str):
        rows = self.db.all("SELECT * FROM versions WHERE item_id=? ORDER BY created_at DESC",
                           (item_id,))
        cut = time.time() - self.s.versions_days * 86400
        for i, v in enumerate(rows):
            if i >= self.s.versions_keep or v["created_at"] < cut:
                try:
                    self._fs(root, caller.session).remove(v["path"])
                except NotFound:
                    pass
                with self.db.tx() as c:
                    c.execute("DELETE FROM versions WHERE id=?", (v["id"],))

    def commit(self, caller: Caller, uid: str, parent_id: str | None, name: str | None,
               item_id: str | None = None, if_match: str | None = None,
               replaces: str | None = None) -> dict:
        """Place a finished upload. Three ways a save reaches us, all ending as
        ONE new version of the file on the NAS:

          1. modify   item_id given            -> CAS on content etag
          2. replace  create + replaces=X      -> successor of X (create-before-delete)
          3. revive   create, X trashed < 60 s -> successor of X (delete-before-create)

        A failed CAS never overwrites: the upload lands as a conflict copy.
        """
        root = self._root(caller)
        u = self._upload(caller, uid)
        if u["received"] != u["size"]:
            raise ApiError(409, "upload_incomplete", received=u["received"])
        sess, fs = caller.session, self._fs(root, caller.session)
        h = hashlib.sha256()
        for chunk in fs.read(u["temp_path"]):
            h.update(chunk)
        if h.hexdigest() != u["sha256"]:
            raise ApiError(422, "file_hash_mismatch")

        # Bring the destination folder up to date first. Without this a stale
        # index misses (a) a same-name file someone just wrote over SMB, so the
        # collision check passes and the rename fails or twins it, and (b) an
        # SMB-side edit of the target, so the CAS below compares against an etag
        # the NAS has already moved past and silently overwrites that edit.
        if item_id:
            pre = self._item(root, item_id)
            self.reconcile(root, sess, pre["parent_id"], max_depth=1)
        elif parent_id:
            self.reconcile(root, sess, self._item(root, parent_id)["id"], max_depth=1)

        target = None
        if item_id:
            target = self._item(root, item_id)
            if target["is_dir"]:
                raise ApiError(400, "is_a_folder")
            parent = self._item(root, target["parent_id"])
            name = target["name"]
        else:
            if not parent_id or not name:
                raise ApiError(400, "parent_and_name_required")
            name = unicodedata.normalize("NFC", name)
            if not valid_name(name):
                raise ApiError(422, "invalid_or_excluded_name")
            parent = self._item(root, parent_id)
            if not parent["is_dir"]:
                raise ApiError(400, "not_a_folder")
        if root["share"] == ALL_SHARES and parent["id"] == root["id"]:
            raise FixedItem(name)            # no files beside the shares
        temp = u["temp_path"]
        parent_path = self._relpath(root, parent)
        if fs.share_of(temp) != fs.share_of(parent_path):
            # An upload begun for another share (a resumed one): copy it into this
            # share's area first, outside the transaction, so a failure later on
            # leaves the upload row pointing at the file that really exists.
            moved = self._meta(root, "uploads", uid, parent_path)
            fs.makedirs(split(moved)[0])
            fs.copy(temp, moved)
            with self.db.tx() as c:
                c.execute("UPDATE uploads SET temp_path=? WHERE id=?", (moved, uid))
            fs.remove(temp)
            temp = moved

        result = "created"
        with self.db.tx() as c:
            if target is not None and if_match and if_match != etag_c(target):
                target, result = None, "conflict"
                name = self._free_conflict_name(c, root, parent, name, caller.device["device_name"])
            existing = c.execute("SELECT * FROM items WHERE root_id=? AND parent_id=? AND"
                                 " name_key=? AND deleted=0",
                                 (root["id"], parent["id"], name_key(name))).fetchone()
            if target is None and existing is not None:
                if (result != "conflict" and replaces == existing["id"] and not existing["is_dir"]
                        and (not if_match or if_match == etag_c(existing))):
                    result = "replaced"
                else:
                    result = "conflict"
                    name = self._free_conflict_name(c, root, parent, name, caller.device["device_name"])
                    existing = None

            dest = join(self._relpath(root, parent), name)
            new_id = target["id"] if target is not None else uuid.uuid4().hex
            undo: list[tuple[str, str]] = []   # NAS renames to reverse if a later step fails
            prev_cv = 0
            try:
                if target is not None:
                    undo.append(self._version_out(c, caller, root, target, new_id))
                    result = "modified"
                elif result == "replaced":
                    undo.append(self._version_out(c, caller, root, existing, new_id))
                    c.execute("UPDATE versions SET item_id=? WHERE item_id=?", (new_id, existing["id"]))
                elif result == "created":
                    cut = time.time() - self.s.safe_save_window_s
                    tr = c.execute("SELECT * FROM trash WHERE root_id=? AND parent_id=? AND name_key=?"
                                   " AND is_dir=0 AND deleted_at>? ORDER BY deleted_at DESC LIMIT 1",
                                   (root["id"], parent["id"], name_key(name), cut)).fetchone()
                    if tr:
                        # Delete-then-create inside the window = a safe-save. The
                        # trashed copy becomes the previous version, not trash.
                        vid = uuid.uuid4().hex
                        vpath = self._meta(root, "versions", vid, tr["path"])
                        fs.makedirs(split(vpath)[0])
                        fs.rename(tr["path"], vpath)
                        undo.append((tr["path"], vpath))
                        prev_cv = tr["content_version"]
                        c.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)",
                                  (vid, new_id, root["id"], tr["content_version"], tr["size"],
                                   tr["mtime_ns"], vpath, time.time()))
                        c.execute("UPDATE versions SET item_id=? WHERE item_id=?",
                                  (new_id, tr["item_id"]))
                        c.execute("UPDATE items SET superseded_by=? WHERE id=?", (new_id, tr["item_id"]))
                        c.execute("DELETE FROM trash WHERE id=?", (tr["id"],))
                        result = "replaced"
                try:
                    fs.rename(temp, dest, replace=False)
                except Exists:
                    raise ApiError(409, "name_exists_on_nas")   # someone wrote it over SMB just now
                st = fs.stat(dest)
                if target is not None:
                    c.execute("UPDATE items SET size=?, mtime_ns=?, file_id=?, sha256=?,"
                              " content_version=content_version+1 WHERE id=?",
                              (st.size, st.mtime_ns, st.file_id, u["sha256"], new_id))
                    self._record(c, root["id"], new_id, "upsert")
                else:
                    if existing is not None:      # replaced: retire the predecessor first
                        self._mark_deleted(c, root["id"], existing["id"], superseded_by=new_id)
                    c.execute("INSERT INTO items (id, root_id, parent_id, name, name_key, is_dir, size,"
                              " mtime_ns, file_id, sha256, content_version) VALUES (?,?,?,?,?,0,?,?,?,?,?)",
                              (new_id, root["id"], parent["id"], name, name_key(name), st.size,
                               st.mtime_ns, st.file_id, u["sha256"],
                               (existing["content_version"] + 1) if existing else prev_cv + 1))
                    self._record(c, root["id"], new_id, "upsert")
            except BaseException:
                # The DB rolls back with the transaction; the NAS does not, so
                # put back what we moved. Best effort: a failure here is logged by
                # the caller's 5xx and the next reconcile re-reads the truth.
                for src, dst in reversed(undo):
                    try:
                        fs.rename(dst, src)
                    except StorageError:
                        pass
                raise
            c.execute("DELETE FROM uploads WHERE id=?", (uid,))
        self._notify()
        self._prune_versions(caller, root, new_id)
        self.db.audit(f"write_{result}", nas_user=caller.user, device_id=caller.device_id,
                      ip=caller.ip, route=caller.route, item=new_id, path=dest)
        it = self.db.one("SELECT * FROM items WHERE id=?", (new_id,))
        return {"result": result, "item": self._json(root, it),
                "replaced_id": existing["id"] if existing is not None else None}

    # ── folders, rename/move, delete ──────────────────────────────────────

    def mkdir(self, caller: Caller, parent_id: str, name: str) -> dict:
        root = self._root(caller)
        parent = self._item(root, parent_id)
        name = unicodedata.normalize("NFC", name or "")
        if not parent["is_dir"]:
            raise ApiError(400, "not_a_folder")
        if root["share"] == ALL_SHARES and parent["id"] == root["id"]:
            raise FixedItem(name)            # shares are made on the NAS, not here
        if not valid_name(name):
            raise ApiError(422, "invalid_or_excluded_name")
        dup = self.db.one("SELECT id FROM items WHERE root_id=? AND parent_id=? AND name_key=?"
                          " AND deleted=0", (root["id"], parent["id"], name_key(name)))
        if dup:
            raise ApiError(409, "name_exists", existing_id=dup["id"])
        path = join(self._relpath(root, parent), name)
        fs = self._fs(root, caller.session)
        fs.makedirs(path)
        st = fs.stat(path)
        with self.db.tx() as c:
            # A reconcile may have indexed the new folder between makedirs and here.
            dup = c.execute("SELECT id, is_dir FROM items WHERE root_id=? AND parent_id=?"
                            " AND name_key=? AND deleted=0",
                            (root["id"], parent["id"], name_key(name))).fetchone()
            if dup and not dup["is_dir"]:
                raise ApiError(409, "name_exists", existing_id=dup["id"])
            iid = dup["id"] if dup else self._new_item(
                c, root["id"], parent["id"], Entry(name, True, 0, st.mtime_ns, st.file_id))
        self._notify()
        return self._json(root, self.db.one("SELECT * FROM items WHERE id=?", (iid,)))

    def patch(self, caller: Caller, item_id: str, name: str | None, parent_id: str | None,
              if_match: str | None) -> dict:
        root = self._root(caller)
        it = self._item(root, item_id)
        if it["parent_id"] is None:
            raise ApiError(400, "cannot_move_root")
        if if_match and if_match != etag_m(it):
            raise ApiError(412, "meta_etag_mismatch", current=item_json(it))
        new_parent = self._item(root, parent_id or it["parent_id"])
        if not new_parent["is_dir"]:
            raise ApiError(400, "not_a_folder")
        # no moving a folder into itself
        cur = new_parent
        while cur["parent_id"] is not None:
            if cur["id"] == it["id"]:
                raise ApiError(400, "move_into_self")
            cur = self.db.one("SELECT * FROM items WHERE id=?", (cur["parent_id"],))
        new_name = unicodedata.normalize("NFC", name) if name else it["name"]
        if not valid_name(new_name):
            raise ApiError(422, "invalid_or_excluded_name")
        dup = self.db.one("SELECT id FROM items WHERE root_id=? AND parent_id=? AND name_key=?"
                          " AND deleted=0 AND id<>?",
                          (root["id"], new_parent["id"], name_key(new_name), it["id"]))
        if dup:
            raise ApiError(409, "name_exists", existing_id=dup["id"])
        src = self._relpath(root, it)
        dst = join(self._relpath(root, new_parent), new_name)
        if src != dst:
            fs = self._fs(root, caller.session)
            try:
                try:
                    fs.rename(src, dst)
                except CrossShare:
                    fs.move_across(src, dst)    # Finder drag from one share to another
            except Exists:
                raise ApiError(409, "name_exists_on_nas")
        with self.db.tx() as c:
            c.execute("UPDATE items SET parent_id=?, name=?, name_key=?, meta_version=meta_version+1"
                      " WHERE id=?", (new_parent["id"], new_name, name_key(new_name), it["id"]))
            self._record(c, root["id"], it["id"], "upsert")
        self._notify()
        self.db.audit("move", nas_user=caller.user, device_id=caller.device_id, ip=caller.ip,
                      route=caller.route, item=it["id"], src=src, dst=dst)
        return self._json(root, self.db.one("SELECT * FROM items WHERE id=?", (it["id"],)))

    def delete(self, caller: Caller, item_id: str, if_match: str | None) -> dict:
        root = self._root(caller)
        it = self._item(root, item_id, live=False)
        if it["deleted"]:
            # Idempotent. A safe-save predecessor lands here when the client's
            # deleteItem(X) arrives after commit(replaces=X) already retired it.
            return {"result": "superseded" if it["superseded_by"] else "already_deleted",
                    "superseded_by": it["superseded_by"]}
        if it["parent_id"] is None:
            raise ApiError(400, "cannot_delete_root")
        if if_match and not it["is_dir"] and if_match != etag_c(it):
            raise ApiError(412, "content_etag_mismatch", current=item_json(it))
        fs, src = self._fs(root, caller.session), self._relpath(root, it)
        if fs.multi and it["parent_id"] == root["id"]:
            raise FixedItem(src)
        tid = uuid.uuid4().hex
        tpath = self._meta(root, "trash", tid, src)
        fs.makedirs(split(tpath)[0])
        try:
            fs.rename(src, tpath)
        except NotFound:
            pass                       # already gone on the NAS; just follow
        with self.db.tx() as c:
            c.execute("INSERT INTO trash VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (tid, it["id"], root["id"], it["parent_id"], it["name"], it["name_key"],
                       it["is_dir"], it["content_version"], it["size"], it["mtime_ns"], tpath,
                       time.time()))
            self._mark_deleted(c, root["id"], it["id"])
        self._notify()
        self.db.audit("delete", nas_user=caller.user, device_id=caller.device_id, ip=caller.ip,
                      route=caller.route, item=it["id"], path=src, trash=tid)
        return {"result": "trashed", "trash_id": tid}

    # ── versions ──────────────────────────────────────────────────────────

    def versions(self, caller: Caller, item_id: str) -> list[dict]:
        root = self._root(caller)
        self._item(root, item_id)
        rows = self.db.all("SELECT id, content_version, size, mtime_ns, created_at FROM versions"
                           " WHERE item_id=? ORDER BY created_at DESC", (item_id,))
        return [dict(r) for r in rows]

    def restore_version(self, caller: Caller, item_id: str, version_id: str) -> dict:
        """Current content becomes a version, the chosen version becomes current.
        Two renames, no copy, nothing lost."""
        root = self._root(caller)
        it = self._item(root, item_id)
        v = self.db.one("SELECT * FROM versions WHERE id=? AND item_id=?", (version_id, item_id))
        if not v:
            raise ApiError(404, "no_such_version")
        fs, path = self._fs(root, caller.session), self._relpath(root, it)
        with self.db.tx() as c:
            self._version_out(c, caller, root, it, it["id"])
            fs.rename(v["path"], path)
            c.execute("DELETE FROM versions WHERE id=?", (version_id,))
            st = fs.stat(path)
            c.execute("UPDATE items SET size=?, mtime_ns=?, file_id=?, sha256=NULL,"
                      " content_version=content_version+1 WHERE id=?",
                      (st.size, st.mtime_ns, st.file_id, it["id"]))
            self._record(c, root["id"], it["id"], "upsert")
        self._notify()
        self.db.audit("restore_version", nas_user=caller.user, device_id=caller.device_id,
                      ip=caller.ip, route=caller.route, item=item_id, version=version_id)
        return self._json(root, self.db.one("SELECT * FROM items WHERE id=?", (item_id,)))

    # ── housekeeping (runs as the user, during their own activity) ────────

    def housekeeping(self, root, session: Session):
        now = time.time()
        fs = self._fs(root, session)
        for t in self.db.all("SELECT * FROM trash WHERE root_id=? AND deleted_at<?",
                             (root["id"], now - self.s.trash_days * 86400)):
            try:
                fs.rmtree(t["path"])
            except (StorageError, Blocked, FixedItem):
                continue
            with self.db.tx() as c:
                c.execute("DELETE FROM trash WHERE id=?", (t["id"],))
        for u in self.db.all("SELECT * FROM uploads WHERE root_id=? AND expires_at<?",
                             (root["id"], now)):
            try:
                fs.remove(u["temp_path"])
            except (StorageError, Blocked, FixedItem):
                pass
            with self.db.tx() as c:
                c.execute("DELETE FROM uploads WHERE id=?", (u["id"],))
        self.agent.prune()
        with self.db.tx() as c:
            old = c.execute("SELECT MAX(seq) s FROM changes WHERE root_id=? AND at<?",
                            (root["id"], now - CHANGES_KEEP_S)).fetchone()["s"]
            if old:
                c.execute("DELETE FROM changes WHERE root_id=? AND seq<=?", (root["id"], old))
                c.execute("UPDATE roots SET min_seq=? WHERE id=?", (old, root["id"]))
