"""Gateway core: devices + sessions, the index, the change feed, and every
write. The HTTP layer (api.py) is a thin shell around this class.

Credentials: crypto.py. Item ids, the change feed and safe-save: below.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime

from . import crypto
from .config import Settings
from .db import DB
from .rootfs import (ALL_SHARES, Blocked, CrossShare, FixedItem, RootFS, is_blocked,
                     is_path_blocked, has_dot_parts)
from .thumbs import Thumbnailer
from .storage.base import (SFM_PRIVATE, AccessDenied, AuthFailed, Backend, Entry, Exists,
                           NotFound, Session, StorageError, join, split, to_nas)

# Names that never sync. The client excludes them too; the gateway
# hides them from listings and refuses uploads of them, so a client bug cannot
# put Office lock files or Finder metadata on the NAS.
EXCLUDED = ["~$*", ".~lock.*#", ".DS_Store", "._*", "Icon\r", "Thumbs.db",
            "desktop.ini", ".loft", ".loft-*"]
CHANGES_KEEP_S = 30 * 86400


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

    # ── devices, tokens, sessions ─────────────────────────────────────────

    def _check_rate(self, user: str, ip: str | None, first: str | None = None):
        cut = time.time() - self.s.login_window_s
        for k, cap in ((first or f"u:{user}", self.s.login_max_fail_user),
                       (f"ip:{ip}", self.s.login_max_fail_ip)):
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
            self._record_failure(f"u:{user}", f"ip:{ip}")
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
                    ip: str | None, route: str, event: str, **detail) -> dict:
        device_id = uuid.uuid4().hex
        tok, secret = crypto.new_token(device_id)
        now = time.time()
        with self.db.tx() as c:
            c.execute("INSERT INTO devices (id, nas_user, device_name, token_hash, wrapped_cred,"
                      " created_at, last_used_at, last_ip, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (device_id, user, device_name[:100] or "Mac", crypto.token_hash(secret),
                       crypto.wrap_credential(self.master, secret, device_id, user, password),
                       now, now, ip, now + self.s.token_ttl_days * 86400))
        self._put_session(device_id, sess)
        self.db.audit(event, nas_user=user, device_id=device_id, ip=ip, route=route,
                      device_name=device_name, **detail)
        return {"device_id": device_id, "token": tok, "nas_user": user,
                "expires_at": now + self.s.token_ttl_days * 86400}

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
            self._record_failure(f"ip:{ip}")
            raise ApiError(401, "invalid_code")
        self._check_rate("", ip, first=f"code:{code_id}")
        row = self.db.one("SELECT * FROM signin_codes WHERE id=?", (code_id,))
        now = time.time()
        if not row or not crypto.hash_matches(csecret, row["code_hash"]):
            self._record_failure(f"code:{code_id}", f"ip:{ip}")
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

    def _put_session(self, device_id: str, sess: Session):
        with self._sess_lock:
            old = self._sessions.pop(device_id, None)
            self._sessions[device_id] = (sess, time.time())
        if old:
            old[0].close()

    def _drop_session(self, device_id: str):
        with self._sess_lock:
            old = self._sessions.pop(device_id, None)
        if old:
            old[0].close()

    def expire_sessions(self):
        cut = time.time() - self.s.session_idle_s
        with self._sess_lock:
            dead = [k for k, (_, t) in self._sessions.items() if t < cut]
            gone = [self._sessions.pop(k)[0] for k in dead]
        for s in gone:
            s.close()

    def authenticate(self, tok: str, ip: str | None, route: str) -> Caller:
        d, secret, blob = self._device_for_token(tok)
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
            self._put_session(d["id"], sess)
        with self.db.tx() as c:
            c.execute("UPDATE devices SET last_used_at=?, last_ip=? WHERE id=?",
                      (time.time(), ip, d["id"]))
        return Caller(d, sess, ip, route)

    def refresh(self, tok: str, ip: str | None, route: str) -> dict:
        d, secret, blob = self._device_for_token(tok)
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
                           " expires_at, revoked_at FROM devices WHERE nas_user=?"
                           " ORDER BY created_at", (user,))
        return [dict(r) for r in rows]

    def revoke(self, device_id: str, *, by_user: str | None = None, ip=None, route=None):
        d = self.db.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if not d or (by_user is not None and d["nas_user"] != by_user):
            raise ApiError(404, "no_such_device")
        with self.db.tx() as c:
            # Wipe the credential, not just flag the row: a revoked device must
            # leave nothing behind that could ever decrypt to the password.
            c.execute("UPDATE devices SET revoked_at=?, wrapped_cred=x'', prev_wrapped_cred=NULL,"
                      " prev_token_hash=NULL, prev_valid_until=NULL WHERE id=?",
                      (time.time(), device_id))
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

    def release_waiters(self):
        """Shutting down: answer every open long-poll now (with whatever it has), so
        uvicorn isn't left waiting up to longpoll_max_s for them to close."""
        self._closing = True
        self._notify()

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
            self._record(c, root_id, iid, "delete")

    # ── reconcile: the NAS is the truth, the index follows ────────────────

    def reconcile(self, root, session: Session, start_id: str | None = None,
                  max_depth: int | None = None) -> int:
        """Walk the NAS under `start_id` (default: root), bring the index in line,
        and record a change for every difference. Returns the number of changes.

        Items that vanished from one path and appear at another with the same
        server file id are MOVES (same item id, meta_version+1), not
        delete+create, so an external rename in Finder-over-SMB keeps the
        File Provider identity and any local state attached to it.
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

        # 1. the NAS side, breadth-first, shallowest first
        found: list[tuple[str, int, Entry]] = []     # (parent rel-path, depth, entry)
        queue = [("", 1)]
        while queue:
            rel, depth = queue.pop(0)
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
                continue
            for e in ents:
                if is_excluded(e.name):
                    continue
                found.append((rel, depth, e))
                if e.is_dir and (max_depth is None or depth < max_depth):
                    queue.append((join(rel, e.name), depth + 1))

        n = 0
        with self.db.tx() as c:
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
            by_fid: dict[tuple, str] = {(r["file_id"], r["is_dir"]): i
                                        for i, r in items.items() if r["file_id"] is not None}
            seen: set[str] = set()
            dir_ids = {"": start_id}

            fresh = c.execute("SELECT id, parent_id, name_key, file_id, is_dir FROM items"
                              " WHERE root_id=? AND updated_seq>?", (root["id"], seq0)).fetchall()
            fresh_ids = {r["id"] for r in fresh}
            fresh_names = {(r["parent_id"], r["name_key"]) for r in fresh}
            fresh_fids = {(r["file_id"], r["is_dir"]) for r in fresh if r["file_id"] is not None}

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
                                    or (e.file_id, int(e.is_dir)) in fresh_fids):
                    continue             # its row moved, was renamed or deleted since
                if cur and bool(cur["is_dir"]) != e.is_dir:
                    cur = None           # file <-> folder: old one goes, new one comes
                if cur is None and e.file_id is not None:
                    mid = by_fid.get((e.file_id, int(e.is_dir)))
                    if (mid and mid not in seen
                            and (old_paths[mid], bool(items[mid]["is_dir"])) not in now_paths):
                        moved = items[mid]
                        by_parent.pop((moved["parent_id"], moved["name_key"]), None)
                        c.execute("UPDATE items SET parent_id=?, name=?, name_key=?,"
                                  " meta_version=meta_version+1 WHERE id=?", (pid, e.name, k, mid))
                        moved.update(parent_id=pid, name=e.name, name_key=k,
                                     meta_version=moved["meta_version"] + 1)
                        by_parent[(pid, k)] = mid
                        self._record(c, root["id"], mid, "upsert")
                        n += 1
                        cur = moved
                if cur is None:
                    iid = self._new_item(c, root["id"], pid, e)
                    items[iid] = dict(c.execute("SELECT * FROM items WHERE id=?",
                                                (iid,)).fetchone())
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
                if e.is_dir:
                    dir_ids[join(rel, e.name)] = iid

            for iid, r in items.items():
                if iid not in seen and not under_fresh(iid):
                    still = c.execute("SELECT deleted FROM items WHERE id=?", (iid,)).fetchone()
                    if still and not still["deleted"]:
                        self._mark_deleted(c, root["id"], iid)
                        n += 1
            if start_id == root["id"] and max_depth is None:
                c.execute("UPDATE roots SET last_scan_at=? WHERE id=?", (time.time(), root["id"]))
        if n:
            self._notify()
        return n

    def maybe_rescan(self, caller: Caller):
        """Full-root rescan in the background if the last one is stale. Runs as
        the calling user, so it never sees more than that user can."""
        root = self._root(caller)
        if not self.s.background_rescan:
            return
        if root["last_scan_at"] and time.time() - root["last_scan_at"] < self.s.rescan_interval_s:
            return
        lock = self._scan_locks.setdefault(root["id"], threading.Lock())
        if not lock.acquire(blocking=False):
            return

        def run():
            try:
                self.reconcile(root, caller.session)
                self.housekeeping(root, caller.session)
            except Exception:  # noqa: BLE001  (next request retries)
                pass
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

    def upload_chunk(self, caller: Caller, uid: str, offset: int, data: bytes,
                     chunk_sha256: str | None) -> dict:
        u = self._upload(caller, uid)
        if offset != u["received"]:
            # Resumable: the client re-reads `received` and continues from there.
            raise ApiError(409, "wrong_offset", received=u["received"])
        if len(data) > self.s.upload_chunk_max or offset + len(data) > u["size"]:
            raise ApiError(413, "chunk_too_large")
        if chunk_sha256 and hashlib.sha256(data).hexdigest() != chunk_sha256.lower():
            raise ApiError(422, "chunk_hash_mismatch")
        root = self._root(caller)
        self._fs(root, caller.session).write_at(u["temp_path"], offset, data)
        with self.db.tx() as c:
            c.execute("UPDATE uploads SET received=? WHERE id=? AND received=?",
                      (offset + len(data), uid, offset))
        return {"upload_id": uid, "received": offset + len(data), "size": u["size"]}

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
        with self.db.tx() as c:
            old = c.execute("SELECT MAX(seq) s FROM changes WHERE root_id=? AND at<?",
                            (root["id"], now - CHANGES_KEEP_S)).fetchone()["s"]
            if old:
                c.execute("DELETE FROM changes WHERE root_id=? AND seq<=?", (root["id"], old))
                c.execute("UPDATE roots SET min_seq=? WHERE id=?", (old, root["id"]))
