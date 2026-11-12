"""The NAS agent's side of the gateway: what changed on the NAS, from any source.

SMB change notify (watch.py) only hears what goes through Samba. The NAS agent
(nas-agent/loft_nas_agent.py) runs on the NAS and reads the filesystem's own record
instead: btrfs stamps every inode it writes with the transaction id, so "every
folder written since transaction N" is complete whatever wrote it (the NAS's own
web and phone apps, jobs on the NAS, NFS, rsync) and survives restarts on both sides.

The gateway keeps each share's cursor (agent_shares) and hands it out on /sync; the
agent sends the folders changed since then on /changes, and the cursor moves only
with the last chunk of a report, so nothing is lost if either side stops halfway.
Reported folders go into one log (nas_changes, one row per folder, a folder
reported again moves to the end). Each root applies the log through its own
change watch (watch.RootWatch.agent_*), with the signed-in user's own SMB session,
so it never sees more than that user can, and remembers how far it got
(roots.agent_seq): a root whose user is away catches up when the user is back.

A root is COVERED when the agent was heard from recently, every share in the root
is reported, and a full pass has started since the agent began reporting them
(roots.agent_since): from then on the log is the complete record and the periodic
full pass stops (rescan_interval_agent_s, default never). A share whose subvolume
was recreated, or a report too big to list, rescans that share alone.

The agent dials out; the NAS listens on nothing. Requests come only on the direct
listener, TLS with the pinned key, each signed with the key both sides have:
Authorization: LoftAgent <ms timestamp>.<hex HMAC-SHA256 of
"POST\\n<path>\\n<timestamp>\\n<body>">, timestamps strictly increasing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .storage.base import from_nas

if TYPE_CHECKING:
    from .service import Gateway

log = logging.getLogger("loft.agent")

AGENT_BODY_MAX = 4 << 20    # one chunk of a report (MAX_DIRS_PER_REQUEST long paths)
CLOCK_SKEW_MS = 300_000
MAX_DIRS_PER_REQUEST = 10_000
MAX_PATH = 4096
KEEP_S = 30 * 86400          # the log, like the change feed
SHARE_KEEP_S = 7 * 86400     # a share the agent stopped mentioning (N3 of review_maja_ba880e0)


class AgentAuthError(Exception):
    pass


class AgentBadRequest(Exception):
    pass


class CursorMoved(Exception):
    def __init__(self, cursor):
        super().__init__("cursor_moved")
        self.cursor = cursor


def _ukey(user: str) -> str:
    from .service import name_key
    return name_key(user or "")


def nas_path(p: str) -> str:
    """A path as the agent saw it on disk -> as the gateway names it (the SMB
    private-use spelling of Mac names, like every listing)."""
    return "/".join(from_nas(x) for x in p.split("/") if x)


class NasAgent:
    def __init__(self, gw: "Gateway"):
        self.gw = gw
        self.key: bytes | None = None
        # Nothing signed before this start is taken (closes the replay window a
        # restart would otherwise open; the agent's next request is newer anyway).
        self._last_ts = int(time.time() * 1000)
        self._lock = threading.Lock()
        self.seen_at = 0.0
        self._uncovered_logged: dict[str, str] = {}
        self._failing: set[str] = set()             # shares the agent says aren't going through
        if gw.s.nas_agent_key_path:
            raw = Path(gw.s.nas_agent_key_path).read_bytes().strip()
            self.key = base64.b64decode(raw)
            if len(self.key) < 32:
                raise ValueError("nas_agent_key_path: need base64 of 32 bytes or more")
        # An agent that reported before this start gets nas_agent_stale_s to reconnect
        # (it backs off up to a minute) before the roots it covers count as uncovered:
        # otherwise the first request after every restart would start a full pass.
        r = gw.db.one("SELECT MAX(seen_at) t FROM agent_shares")
        self.seen_at = time.time() if r and r["t"] else 0.0
        self.started = time.time()                  # the same grace for each share

    @property
    def enabled(self) -> bool:
        return self.key is not None

    # ── requests ─────────────────────────────────────────────────────────

    def verify(self, method: str, path: str, authorization: str | None, body: bytes):
        if not self.enabled:
            raise AgentAuthError("off")
        if not authorization or not authorization.startswith("LoftAgent "):
            raise AgentAuthError("missing")
        ts_s, _, sig = authorization[len("LoftAgent "):].strip().partition(".")
        if not ts_s.isdigit() or not sig:
            raise AgentAuthError("malformed")
        ts = int(ts_s)
        msg = method.encode() + b"\n" + path.encode() + b"\n" + ts_s.encode() + b"\n" + body
        want = hmac.new(self.key, msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, sig):
            raise AgentAuthError("bad signature")
        with self._lock:
            if abs(ts - time.time() * 1000) > CLOCK_SKEW_MS:
                raise AgentAuthError("clock")
            if ts <= self._last_ts:
                raise AgentAuthError("replay")
            self._last_ts = ts

    def sync(self, body: dict) -> dict:
        """The agent says what it watches; it gets the cursor of each."""
        shares = body.get("shares")
        if not isinstance(shares, list):
            raise AgentBadRequest("shares")
        from .service import name_key
        now = time.time()
        out = {}
        new_shares, recreated, failing = [], [], []
        with self.gw.db.tx() as c:
            first = c.execute("SELECT COUNT(*) n FROM agent_shares").fetchone()["n"] == 0
            for sh in shares:
                try:
                    sid, share = str(sh["id"]), str(sh["share"])
                    user, uuid = str(sh.get("user") or ""), str(sh["uuid"])
                    tree_id, gen = int(sh["tree_id"]), int(sh["generation"])
                    ok = bool(sh.get("ok", True))
                except (KeyError, TypeError, ValueError) as e:
                    raise AgentBadRequest(f"share: {e}") from e
                sk, uk = name_key(share), _ukey(user)
                row = c.execute("SELECT * FROM agent_shares WHERE id=?", (sid,)).fetchone()
                if row is None:
                    # The very first contact: nothing is covered until a full pass has run
                    # since now. A share added later is rescanned on its own instead.
                    c.execute("INSERT INTO agent_shares VALUES (?,?,?,?,?,?,?,?)",
                              (sid, sk, uk, uuid, tree_id, gen, now if first else 0.0,
                               now if ok else 0.0))
                    if not first:
                        self._log_all(c, sk, uk, now)
                    new_shares.append(sid)
                    cur = gen
                elif row["uuid"] != uuid or row["tree_id"] != tree_id:
                    c.execute("UPDATE agent_shares SET uuid=?, tree_id=?, cursor=?, seen_at=?,"
                              " share_key=?, user_key=? WHERE id=?",
                              (uuid, tree_id, gen, now, sk, uk, sid))
                    self._log_all(c, sk, uk, now)
                    recreated.append(sid)
                    cur = gen
                else:
                    # seen_at says the share is being reported: not while its rounds fail
                    if ok:
                        c.execute("UPDATE agent_shares SET seen_at=? WHERE id=?", (now, sid))
                    cur = row["cursor"]
                out[sid] = cur
                if not ok and sid not in self._failing:
                    failing.append(sid)
                    self._failing.add(sid)
                elif ok and sid in self._failing:
                    self._failing.discard(sid)
                    log.info("NAS agent: %s is reported again", sid.replace("\n", " for "))
        self.seen_at = now
        for sid in new_shares:
            log.info("NAS agent: reporting %s from now on", sid.replace("\n", " for "))
        for sid in recreated:
            log.warning("NAS agent: %s is another subvolume than before (recreated?);"
                        " rescanning it", sid.replace("\n", " for "))
        for sid in failing:
            log.warning("NAS agent: %s can't be read on the NAS (see the agent's log);"
                        " its roots fall back to full passes", sid.replace("\n", " for "))
        if new_shares or recreated:
            self.gw.watches.agent_wake()
        return {"cursors": out}

    def changes(self, body: dict) -> dict:
        """One chunk of a report. The cursor moves with the last one (final)."""
        try:
            sid, uuid = str(body["id"]), str(body["uuid"])
            since, until = int(body["since"]), int(body["until"])
            dirs = body.get("dirs") or []
            final, overflow = bool(body.get("final")), bool(body.get("overflow"))
        except (KeyError, TypeError, ValueError) as e:
            raise AgentBadRequest(str(e)) from e
        if (not isinstance(dirs, list) or len(dirs) > MAX_DIRS_PER_REQUEST or until < since
                or any(not isinstance(d, str) or len(d) > MAX_PATH for d in dirs)):
            raise AgentBadRequest("dirs")
        now = time.time()
        with self.gw.db.tx() as c:
            row = c.execute("SELECT * FROM agent_shares WHERE id=?", (sid,)).fetchone()
            if row is None or row["uuid"] != uuid:
                raise CursorMoved(None)
            if row["cursor"] != since:
                raise CursorMoved(row["cursor"])
            sk, uk = row["share_key"], row["user_key"]
            if overflow:
                self._log_all(c, sk, uk, now)
            for d in dirs:
                p = nas_path(d)
                c.execute("INSERT OR REPLACE INTO nas_changes (share_key, user_key, kind, path,"
                          " at) VALUES (?,?,'dir',?,?)", (sk, uk, p, now))
            c.execute("UPDATE agent_shares SET seen_at=?" + (", cursor=?" if final else "")
                      + " WHERE id=?", (now, until, sid) if final else (now, sid))
        self.seen_at = now
        if dirs or overflow:
            if overflow:
                log.warning("NAS agent: too many changes in %s to list; rescanning it",
                            sid.replace("\n", " for "))
            self.gw.watches.agent_wake()
        return {"ok": True}

    @staticmethod
    def _log_all(c, sk: str, uk: str, now: float):
        c.execute("INSERT OR REPLACE INTO nas_changes (share_key, user_key, kind, path, at)"
                  " VALUES (?,?,'all','',?)", (sk, uk, now))

    # ── for the roots ────────────────────────────────────────────────────

    def max_seq(self) -> int:
        r = self.gw.db.one("SELECT MAX(seq) s FROM nas_changes")
        return (r["s"] or 0) if r else 0

    def after(self, seq: int, limit: int = 5000) -> list:
        return self.gw.db.all("SELECT * FROM nas_changes WHERE seq>? ORDER BY seq LIMIT ?",
                              (seq, limit))

    def floor(self) -> int:
        r = self.gw.db.one("SELECT v FROM meta WHERE k='nas_changes_floor'")
        return int(r["v"]) if r else 0

    def fresh(self) -> bool:
        return self.enabled and time.time() - self.seen_at < self.gw.s.nas_agent_stale_s

    def covers(self, root, share_keys: set[str] | None) -> bool:
        """Whether the agent's log is the complete record of this root (see the
        module docstring). share_keys: name_keys of the root's shares (None:
        not known yet)."""
        why = self._uncovered(root, share_keys)
        rid = root["id"]
        if why and self._uncovered_logged.get(rid) != why and self.enabled:
            log.info("NAS agent: root %s (%s) not covered: %s", rid[:8], root["nas_user"], why)
        if why:
            self._uncovered_logged[rid] = why
        elif self._uncovered_logged.pop(rid, None) is not None:
            log.info("NAS agent: root %s (%s) covered: no more periodic full passes",
                     rid[:8], root["nas_user"])
        return not why

    def _uncovered(self, root, share_keys) -> str:
        if not self.enabled:
            return "off"
        if not self.fresh():
            return "the agent is not reporting"
        since = root["agent_since"]
        if since is None:
            return "no full pass since the agent began"
        if root["agent_seq"] < self.floor():
            return "away longer than the log keeps"
        if share_keys is None:
            return "share list not read yet"
        uk = _ukey(root["nas_user"])
        rows = self.gw.db.all("SELECT share_key, user_key, since_at, seen_at FROM agent_shares")
        stale = time.time() - self.gw.s.nas_agent_stale_s
        for sk in share_keys:
            mine = [r for r in rows if r["share_key"] == sk and r["user_key"] in ("", uk)]
            if not mine:
                return f"share {sk!r} not reported"
            if min(r["since_at"] for r in mine) > since:
                return f"share {sk!r} reported only since after the last full pass"
            # the agent stopped watching it, or can't read it: not reported now
            if max(min(r["seen_at"] for r in mine), self.started) < stale:
                return f"share {sk!r} not reported lately"
        return ""

    def prune(self):
        cut = time.time() - KEEP_S
        with self.gw.db.tx() as c:
            r = c.execute("SELECT MAX(seq) s FROM nas_changes WHERE at<?", (cut,)).fetchone()
            if r and r["s"]:
                c.execute("DELETE FROM nas_changes WHERE at<?", (cut,))
                c.execute("INSERT OR REPLACE INTO meta VALUES ('nas_changes_floor', ?)",
                          (str(r["s"]),))
            # a share the agent hasn't mentioned for a week is gone from the NAS (or
            # excluded); if it comes back it is new again, and rescanned on its own.
            # Fails safe: a root that still lists it counts it as not reported.
            gone = c.execute("DELETE FROM agent_shares WHERE seen_at<? AND seen_at>0",
                             (time.time() - SHARE_KEEP_S,)).rowcount
        if gone:
            log.info("NAS agent: forgot %d share(s) not reported for %d days", gone,
                     SHARE_KEEP_S // 86400)
