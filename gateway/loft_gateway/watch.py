"""NAS change notify: the NAS says what changed, so a file saved on the NAS reaches
the Macs in a second or two instead of at the next full rescan (one pass of a
large all-shares root took over an hour in testing).

One SMB2 CHANGE_NOTIFY with WATCH_TREE per share (for a one-share root: on the
root folder), opened through a signed-in device's own session. A watch therefore
sees what that account sees, and lives only as long as that session
(session_idle_s, or rescan_keeps_session_s while a rescan runs on it): a watch keeps
no password in memory longer than the session already does. Measured on
a Samba NAS: changes made over SMB anywhere in a share's tree arrive within
~35 ms, as paths relative to the watched folder.

Every reported path marks its folder dirty. A worker re-reads dirty folders
(reconcile, max_depth=1) shallowest first, notify_debounce_s after the first
report, so a burst becomes one listing per folder. A BUSY folder (re-read
BUSY_READS times within its floor: a Time Machine backup rewrites the same folder
many times a second) waits its floor after its last read; a quiet one never
waits. The floor is notify_folder_min_s, or COST_FACTOR x the folder's last read
time if that is longer (a 65k-entry maildir under a mail sync: review F2). A folder the index doesn't know yet is reached through its
deepest known ancestor, one level per listing. A move between two folders stays a
move (same item id): an item missing from its folder is only deleted
notify_move_grace_s later, if no folder read meanwhile turned out to be where it
went (rescan() does the same within one pass).

Overflow (STATUS_NOTIFY_ENUM_DIR: more changes than the NAS could list) rescans
that share. At most one such rescan runs per root, at most one more waits per
share, and a share's next one starts notify_overflow_rescan_s after its last one
ENDED (a rescan of a very large share can take longer than that: review F1). It holds
the root's scan lock, so it never runs alongside the full pass; one that finds
the full pass running waits for it, and is dropped if a whole pass started after
the overflow.

The full rescan stays as the safety net, for what Samba doesn't report (files
written on the NAS itself rather than over SMB) and for gaps (gateway restart, a
watch lost). While every watch of a root is up and a full pass has started since,
the pass runs every rescan_interval_watched_s instead of rescan_interval_s.

The NAS agent (nas_agent.py) fills that gap: its log of changed folders reaches
every root through the same worker (agent_*), with the folder's lowest log seq
held until the folder has been read, so roots.agent_seq only ever says "applied
up to here". A root covered by the agent runs no periodic full pass at all.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

from .rootfs import ALL_SHARES, Blocked, FixedItem
from .storage.base import NotFound, Session, StorageError, join

if TYPE_CHECKING:
    from .service import Gateway

log = logging.getLogger("loft.notify")

AGENT_PAGE = 5000         # log rows read at once
NEW_SHARE_RESCAN_S = 10   # a share new to an all-shares root: its top first, then all
RETRY_S = 60              # a watch that failed to start is retried at most this often
BUSY_READS = 3            # this many re-reads within the folder's floor = a busy folder
COST_FACTOR = 20          # a busy folder's floor is at least 20 x its last read time
COST_CAP_S = 300.0        # ... but never more than this
OVER_RETRY_S = 30         # an overflow rescan that found the full pass running retries
READ_LOG_MIN = 1          # log a batch at INFO once it changed at least this many items


def _key(parts) -> tuple:
    from .service import name_key
    return tuple(name_key(p) for p in parts)


class _ShareWatch:
    """One watch thread: blocks on the NAS, hands every report to its RootWatch."""

    def __init__(self, rw: "RootWatch", share: str, base: str, prefix: tuple):
        self.rw, self.share, self.base, self.prefix = rw, share, base, prefix
        self.ready_at: float | None = None
        self.dead = False
        self._stopped = False
        self._w = None
        self._lock = threading.Lock()
        self.t = threading.Thread(target=self._run, daemon=True,
                                  name=f"notify-{rw.root_id[:8]}-{share}")
        self.t.start()

    @property
    def up(self) -> bool:
        return not self.dead and self.ready_at is not None

    def _run(self):
        try:
            w = self.rw.session.watch(self.share, self.base)
        except Exception as e:  # noqa: BLE001
            log.warning("notify %s (%s): can't watch %s: %s", self.rw.root_id[:8],
                        self.rw.user, self.share, e)
            self.dead = True
            return
        with self._lock:
            self._w = w
            stopped = self._stopped
        if stopped:
            w.close()
            self.dead = True
            return
        self.ready_at = time.time()
        log.info("notify %s (%s): watching %s", self.rw.root_id[:8], self.rw.user,
                 join(self.share, self.base) or self.share)
        try:
            while not self._stopped:
                paths = w.next()
                if paths is None:
                    self.rw.overflow(self.prefix)
                else:
                    self.rw.changed(self.prefix, paths)
        except StorageError as e:
            if not self._stopped:
                log.warning("notify %s (%s): watch on %s lost: %s", self.rw.root_id[:8],
                            self.rw.user, self.share, e)
        except Exception:  # noqa: BLE001
            # not when stopped: a watch cancelled at shutdown makes smbprotocol parse the
            # cancel reply as a change list (struct.error), harmless (bc130a0 review)
            if not self._stopped:
                log.exception("notify %s (%s): watch on %s failed", self.rw.root_id[:8],
                              self.rw.user, self.share)
        finally:
            self.dead = True
            w.close()

    def stop(self):
        with self._lock:
            self._stopped = True
            w = self._w
        if w is not None:
            w.close()


class RootWatch:
    """Every watch of one root, plus the worker that re-reads what they report."""

    def __init__(self, gw: "Gateway", root, session: Session):
        self.gw, self.session = gw, session
        self.root_id, self.user = root["id"], root["nas_user"]
        self.multi = root["share"] == ALL_SHARES
        self.share, self.path = root["share"], root["path"]
        self.watches: dict[tuple, _ShareWatch] = {}
        self.retry_at: dict[tuple, float] = {}
        self.cv = threading.Condition()
        self.dirty: dict[tuple, tuple[float, tuple]] = {}    # folder key -> (due, parts)
        self.over: dict[tuple, tuple[float, tuple]] = {}     # share key -> (due, prefix)
        self.reads: dict[tuple, deque] = {}                   # folder key -> last re-reads
        self.pending: dict[str, tuple] = {}                   # missing items, as rescan's defer
        self.pending_due: dict[str, float] = {}               # ... deleted at this time
        self.cost: dict[tuple, float] = {}                    # folder key -> last read, s
        self.last_over: dict[tuple, float] = {}               # share key -> last rescan END
        self.over_at: dict[tuple, float] = {}                 # share key -> first report (wall)
        self.over_running = False                             # one overflow rescan per root
        # NAS agent: log read up to agent_upto; agent_done (= roots.agent_seq) only up
        # to below the lowest seq still waiting on a folder read or a share rescan
        self.agent_upto = int(root["agent_seq"] or 0)
        if root["agent_since"] is None and gw.agent.enabled:
            # not covered: a full pass reads everything before it anyway
            self.agent_upto = max(self.agent_upto, gw.agent.max_seq())
        self.agent_done = self.agent_upto
        self.agent_new = True                                 # read the log (catch up)
        self.agent_hold: dict[tuple, int] = {}                # folder key -> lowest seq
        self.over_hold: dict[tuple, int] = {}                 # share key -> lowest seq
        self.inflight_hold: list[int] = []                    # popped, being read
        self.over_running_hold: int | None = None
        self.progress_due = False
        from .service import name_key
        self.share_names: dict[str, str] | None = (           # name_key -> share name
            None if self.multi else {name_key(self.share): self.share})
        self.stopping = False
        self._refresh_lock = threading.Lock()
        self.worker = threading.Thread(target=self._work, daemon=True,
                                       name=f"notify-{self.root_id[:8]}")
        self.worker.start()

    # ── watches ───────────────────────────────────────────────────────────

    def refresh(self, shares: list[str] | None):
        """Watch exactly `shares` (all-shares root) or the root folder: start the
        missing ones, restart dead ones (at most every RETRY_S), stop the rest."""
        if self.multi:
            want = {_key([s]): (s, "", (s,)) for s in shares or []}
            self._shares_listed(shares or [])
        else:
            want = {(): (self.share, self.path, ())}
        with self._refresh_lock:
            if self.stopping:
                return
            for k in [k for k in self.watches if k not in want]:
                self.watches.pop(k).stop()
            now = time.monotonic()
            for k, (share, base, prefix) in want.items():
                w = self.watches.get(k)
                if w is not None and not w.dead:
                    continue
                if now < self.retry_at.get(k, 0):
                    continue
                self.retry_at[k] = now + RETRY_S
                self.watches[k] = _ShareWatch(self, share, base, prefix)

    def _shares_listed(self, shares: list[str]):
        """The all-shares root's share list. A share new since the last list (the
        account was given one) is read top first, then rescanned: no full pass will
        (the agent's coverage stops them), and SMB notify only reports what changes."""
        from .service import name_key
        names = {name_key(s): s for s in shares}
        with self.cv:
            old, self.share_names = self.share_names, names
            if old is None:
                self.agent_new = True            # the log can be applied now
            else:
                now = time.monotonic()
                for k in names.keys() - old.keys():
                    log.info("notify %s (%s): new share %s", self.root_id[:8], self.user,
                             names[k])
                    self._mark((names[k],), now)
                    self._over_add((names[k],), now + NEW_SHARE_RESCAN_S)
            self.cv.notify_all()

    def since(self) -> float | None:
        """Since when every watch has been up without a break (None: not all up)."""
        with self._refresh_lock:
            ws = list(self.watches.values())
        if not ws or not all(w.up for w in ws):
            return None
        return max(w.ready_at for w in ws)

    def all_up(self) -> bool:
        return self.since() is not None

    def stop(self, background: bool = False):
        """background: close the opens in a daemon thread. Closing is a round trip
        per share and, on a half-open connection, waits for smbprotocol's keepalive
        (up to ~20 min): not in a request or the expiry path (review N1)."""
        with self._refresh_lock:
            self.stopping = True
            ws = list(self.watches.values())
            self.watches.clear()
        for w in ws:
            with w._lock:
                w._stopped = True                # no "watch lost" warning from here on
        with self.cv:
            self.cv.notify_all()

        def close():
            for w in ws:
                w.stop()
        if background:
            threading.Thread(target=close, daemon=True,
                             name=f"notify-close-{self.root_id[:8]}").start()
        else:
            close()

    # ── reports from the watch threads ────────────────────────────────────

    def changed(self, prefix: tuple, paths: list[str]):
        from .service import is_excluded
        s = self.gw.s
        now = time.monotonic()
        with self.cv:
            for p in paths:
                parts = prefix + tuple(x for x in p.split("/") if x)
                if len(parts) <= len(prefix) or any(is_excluded(x) for x in parts):
                    continue                     # .loft, .DS_Store, Office lock files ...
                self._mark(parts[:-1], now)
            self.cv.notify_all()

    def _mark(self, folder: tuple, now: float, seq: int = 0):
        """Folder due for a re-read (under self.cv). seq: the agent log row asking."""
        s = self.gw.s
        k = _key(folder)
        if seq:
            self.agent_hold[k] = min(self.agent_hold.get(k, seq), seq)
        if k in self.dirty:
            return                               # already due; the listing will see this too
        due = now + s.notify_debounce_s
        rd = self.reads.get(k)
        floor = max(s.notify_folder_min_s, COST_FACTOR * self.cost.get(k, 0.0))
        if rd and len(rd) >= BUSY_READS and now - rd[0] < floor:
            due = max(due, rd[-1] + floor)                             # busy: hold it back
        self.dirty[k] = (due, folder)

    def overflow(self, prefix: tuple):
        with self.cv:
            self._over_add(prefix, time.monotonic())
            self.cv.notify_all()

    def _over_add(self, prefix: tuple, now: float, seq: int = 0):
        """Share due for a rescan (under self.cv)."""
        k = _key(prefix)
        if seq:
            self.over_hold[k] = min(self.over_hold.get(k, seq), seq)
        self.over_at.setdefault(k, time.time())
        if k not in self.over:                   # at most one waiting per share
            due = max(now, self.last_over.get(k, float("-inf"))
                      + self.gw.s.notify_overflow_rescan_s)
            self.over[k] = (due, prefix)

    # ── the NAS agent's log ───────────────────────────────────────────────

    def agent_wake(self):
        with self.cv:
            self.agent_new = True
            self.cv.notify_all()

    def _agent_parts(self, r) -> tuple | None:
        """Root-relative folder of a log row, None if it isn't this root's."""
        from .service import name_key
        if r["user_key"] and r["user_key"] != name_key(self.user):
            return None                          # another account's personal folder
        names = self.share_names or {}
        if r["share_key"] not in names:
            return None
        parts = tuple(x for x in r["path"].split("/") if x)
        if self.multi:
            return (names[r["share_key"]],) + parts
        base = tuple(name_key(x) for x in self.path.split("/") if x)
        if tuple(name_key(x) for x in parts[:len(base)]) != base:
            # above or beside the root folder: only a whole-share rescan matters here
            return () if r["kind"] == "all" else None
        return parts[len(base):]

    def _agent_pull(self):
        rows = self.gw.agent.after(self.agent_upto, AGENT_PAGE)
        if not rows:
            return
        from .service import is_excluded
        now = time.monotonic()
        with self.cv:
            for r in rows:
                parts = self._agent_parts(r)
                if parts is None or any(is_excluded(x) for x in parts):
                    continue
                if r["kind"] == "all":
                    self._over_add(parts[:1] if self.multi else (), now, r["seq"])
                else:
                    self._mark(parts, now, r["seq"])
            self.agent_upto = rows[-1]["seq"]
            if len(rows) == AGENT_PAGE:
                self.agent_new = True            # more after these
            self.progress_due = True
            self.cv.notify_all()

    def _agent_progress(self):
        """Persist how far the log is applied: below the lowest seq still waiting."""
        with self.cv:
            holds = (list(self.agent_hold.values()) + list(self.over_hold.values())
                     + self.inflight_hold)
            if self.over_running_hold is not None:
                holds.append(self.over_running_hold)
            done = min(holds) - 1 if holds else self.agent_upto
            self.progress_due = False
        if done > self.agent_done:
            self.agent_done = done
            with self.gw.db.tx() as c:
                c.execute("UPDATE roots SET agent_seq=MAX(agent_seq, ?) WHERE id=?",
                          (done, self.root_id))

    # ── the worker ────────────────────────────────────────────────────────

    def _work(self):
        while True:
            with self.cv:
                while True:
                    if self.stopping:
                        return
                    now = time.monotonic()
                    due_d = [k for k, (d, _) in self.dirty.items() if d <= now]
                    over = {} if self.over_running else self.over   # its end wakes us
                    due_o = [k for k, (d, _) in over.items() if d <= now][:1]
                    due_p = any(d <= now for d in self.pending_due.values())
                    pull = (self.agent_new and self.share_names is not None
                            and self.gw.agent.enabled)
                    if due_d or due_o or due_p or pull or self.progress_due:
                        break
                    nxt = min([d for d, _ in self.dirty.values()]
                              + [d for d, _ in over.values()]
                              + list(self.pending_due.values()), default=None)
                    self.cv.wait(timeout=None if nxt is None else max(0.01, nxt - now))
                if pull:
                    self.agent_new = False
                batch = []
                for k in due_d:
                    batch.append(self.dirty.pop(k)[1])
                    h = self.agent_hold.pop(k, None)
                    if h is not None:
                        self.inflight_hold.append(h)
                obatch = [self.over.pop(k)[1] for k in due_o]
                if obatch:
                    self.over_running = True
                    self.over_running_hold = self.over_hold.pop(due_o[0], None)
            try:
                if pull:
                    self._agent_pull()
                for prefix in obatch:
                    self._overflow_rescan(prefix)
                if batch:
                    self.read(batch)
                if due_p or self.pending:
                    self._finish_pending()
            except Exception:  # noqa: BLE001  (the next report or rescan retries)
                log.exception("notify %s (%s): re-read failed", self.root_id[:8], self.user)
            finally:
                with self.cv:
                    self.inflight_hold.clear()
            try:
                self._agent_progress()
            except Exception:  # noqa: BLE001  (the next round writes it)
                log.exception("notify %s (%s): saving the agent's progress failed",
                              self.root_id[:8], self.user)

    def _root(self):
        return self.gw.db.one("SELECT * FROM roots WHERE id=?", (self.root_id,))

    def _resolve(self, parts: tuple) -> tuple[str, int]:
        """(item id, depth) of the deepest folder of `parts` the index knows."""
        from .service import name_key
        cur = self.root_id
        for i, part in enumerate(parts):
            r = self.gw.db.one("SELECT id, is_dir FROM items WHERE parent_id=? AND name_key=?"
                               " AND deleted=0", (cur, name_key(part)))
            if not r or not r["is_dir"]:
                return cur, i
            cur = r["id"]
        return cur, len(parts)

    def _allowed(self, fs, parts: tuple) -> bool:
        vpath = join(*parts) if self.multi else join(self.path, *parts)
        try:
            fs.nas(vpath)
        except (Blocked, NotFound, FixedItem):
            return False                         # blocked share or path: never read
        return True

    def read(self, batch: list[tuple]) -> int:
        """Re-read these folders (root-relative name parts), shallowest first."""
        from .service import SLOW_READ_S, read_cost
        root = self._root()
        if not root:
            return 0
        fs = self.gw._fs(root, self.session)
        t0 = time.time()
        defer = self.pending                     # shared across batches: see the docstring
        before = set(defer)
        done: set[str] = set()
        n = folders = 0
        for parts in sorted(batch, key=len):
            if not self._allowed(fs, parts):
                continue
            fid, depth = self._resolve(parts)
            while True:
                if fid not in done:
                    done.add(fid)
                    t_read = time.monotonic()
                    try:
                        fst: dict = {}
                        nf = self.gw.reconcile(root, self.session, fid, max_depth=1, defer=defer,
                                               stats=fst)
                        n += nf
                        folders += 1
                        secs = time.monotonic() - t_read
                        # only a good read sets the floor: one SMB timeout must not hold
                        # the folder back for 20 x the timeout (review of 3027669, n1)
                        self.cost[_key(parts[:depth])] = min(COST_CAP_S, secs)
                        if secs > SLOW_READ_S:
                            # one folder holds the root's whole queue this long (F2 of
                            # review_maja_ba880e0): which one, and where the time went
                            log.warning("notify %s (%s): re-read of %s took %.0f s (%s)",
                                        self.root_id[:8], self.user,
                                        "/".join(parts[:depth]) or "(root)", secs,
                                        read_cost(fst, nf))
                    except (StorageError, Blocked, FixedItem) as e:
                        log.warning("notify %s (%s): %s not re-read: %s", self.root_id[:8],
                                    self.user, "/".join(parts[:depth]) or "(root)", e)
                        break
                    finally:
                        k = _key(parts[:depth])
                        rd = self.reads.get(k)
                        if rd is None:
                            rd = self.reads[k] = deque(maxlen=BUSY_READS)
                        rd.append(time.monotonic())
                if depth == len(parts):
                    break
                nfid, ndepth = self._resolve(parts)
                if ndepth <= depth:
                    break                        # the next level isn't there (gone, or a file)
                fid, depth = nfid, ndepth
        due = time.monotonic() + self.gw.s.notify_move_grace_s
        for iid in set(defer) - before:
            self.pending_due[iid] = due
        for iid in before - set(defer):          # found where it went: a move
            self.pending_due.pop(iid, None)
        self._prune()
        if n >= READ_LOG_MIN:
            log.info("notify %s (%s): %d folder(s) re-read, %d change(s), %.2f s",
                     self.root_id[:8], self.user, folders, n, time.time() - t0)
        return n

    def _finish_pending(self) -> int:
        """Delete the missing items whose grace is over (none of the folders read
        since was where they went)."""
        now = time.monotonic()
        due = {i: self.pending.pop(i) for i, d in list(self.pending_due.items())
               if d <= now and i in self.pending}
        for i in [i for i, d in self.pending_due.items() if d <= now or i not in self.pending]:
            self.pending_due.pop(i)
        if not due:
            return 0
        root = self._root()
        if not root:
            return 0
        # Still there after all (moved out and straight back, deleted and put back
        # unchanged): a later listing saw it, but an unchanged row records nothing,
        # so only the NAS can say. One stat per missing item, never per descendant.
        fs = self.gw._fs(root, self.session)
        for iid in list(due):
            it = self.gw.db.one("SELECT * FROM items WHERE id=? AND deleted=0", (iid,))
            if not it:
                due.pop(iid)
                continue
            try:
                fs.stat(self.gw._relpath(root, it))
            except NotFound:
                continue                         # really gone
            except (StorageError, Blocked, FixedItem):
                pass                             # can't tell: the next rescan decides
            due.pop(iid)
        n = self.gw.finish_deferred(root, due)
        if n:
            log.info("notify %s (%s): %d item(s) gone from the NAS", self.root_id[:8], self.user, n)
        return n

    def _prune(self):
        if len(self.reads) > 20000:
            cut = time.monotonic() - self.gw.s.notify_folder_min_s
            self.reads = {k: d for k, d in self.reads.items() if d and d[-1] > cut}
            self.cost = {k: c for k, c in self.cost.items() if k in self.reads}

    def _overflow_rescan(self, prefix: tuple):
        """Rescan one share after an overflow, in its own thread (it can take many
        minutes). The worker has set over_running; this clears it when done."""
        k = _key(prefix)
        lock = self.gw._scan_locks.setdefault(self.root_id, threading.Lock())

        def run():
            ran = False
            try:
                if not lock.acquire(blocking=False):
                    with self.cv:                # the full pass is running: after it
                        if k not in self.over:
                            self.over[k] = (time.monotonic() + OVER_RETRY_S, prefix)
                        if self.over_running_hold is not None:
                            h = self.over_running_hold
                            self.over_hold[k] = min(self.over_hold.get(k, h), h)
                            self.over_running_hold = None
                    return
                try:
                    with self.cv:
                        at = self.over_at.pop(k, time.time())
                    ran = True
                    root = self._root()
                    if not root or self.stopping:
                        return
                    if self.gw._full_start.get(self.root_id, 0) >= at:
                        log.info("notify %s (%s): overflow in %s already covered by a full"
                                 " pass", self.root_id[:8], self.user, "/".join(prefix))
                        return
                    fid, depth = self._resolve(prefix)
                    if depth != len(prefix):
                        return                   # share not indexed yet: the full pass will
                    log.warning("notify %s (%s): the NAS had more changes than it could list"
                                " in %s; rescanning it", self.root_id[:8], self.user,
                                "/".join(prefix) or "the root")
                    self.gw.rescan(root, self.session, start_id=fid)
                finally:
                    lock.release()
            except Exception:  # noqa: BLE001
                log.exception("notify %s (%s): overflow rescan failed", self.root_id[:8],
                              self.user)
            finally:
                with self.cv:
                    self.over_running = False
                    self.over_running_hold = None
                    self.progress_due = True
                    if ran:
                        end = time.monotonic()
                        self.last_over[k] = end
                        if k in self.over:       # reported again meanwhile: from the end
                            d, pfx = self.over[k]
                            self.over[k] = (max(d, end + self.gw.s.notify_overflow_rescan_s), pfx)
                    self.cv.notify_all()
        try:
            threading.Thread(target=run, daemon=True,
                             name=f"notify-over-{self.root_id[:8]}").start()
        except Exception:
            with self.cv:
                self.over_running = False
            raise


class Watches:
    """Every root's RootWatch. ensure() runs on each feed request (maybe_rescan)."""

    CHECK_S = 30

    def __init__(self, gw: "Gateway"):
        self.gw = gw
        self.roots: dict[str, RootWatch] = {}
        self._checked: dict[str, float] = {}
        self.lock = threading.Lock()

    def ensure(self, root, session: Session):
        now = time.monotonic()
        old = None
        with self.lock:
            rw = self.roots.get(root["id"])
            if rw is not None and not self.gw.session_live(rw.session):
                old, rw = rw, None                # its session was closed: take this one
            if rw is None:
                rw = self.roots[root["id"]] = RootWatch(self.gw, root, session)
                self._checked.pop(root["id"], None)
            elif now < self._checked.get(root["id"], 0) and rw.all_up():
                return
            self._checked[root["id"]] = now + self.CHECK_S
        if old is not None:
            old.stop(background=True)             # a round trip per share: not in a request
        shares = None
        if rw.multi:
            try:
                shares = self.gw._fs(root, rw.session).shares()
            except StorageError as e:
                log.warning("notify %s (%s): share list failed: %s", root["id"][:8],
                            root["nas_user"], e)
                return
        rw.refresh(shares)

    def since(self, root_id: str) -> float | None:
        rw = self.roots.get(root_id)
        return rw.since() if rw is not None else None

    def agent_wake(self):
        with self.lock:
            rws = list(self.roots.values())
        for rw in rws:
            rw.agent_wake()

    def get(self, root_id: str) -> RootWatch | None:
        return self.roots.get(root_id)

    def forget(self, session: Session):
        """`session` is being closed: stop every watch that runs on it."""
        with self.lock:
            gone = [k for k, rw in self.roots.items() if rw.session is session]
            rws = [self.roots.pop(k) for k in gone]
        for rw in rws:
            rw.stop(background=True)             # sign-in, revoke, expiry: never wait on I/O

    def stop_all(self):
        with self.lock:
            rws = list(self.roots.values())
            self.roots.clear()
        for rw in rws:
            rw.stop()
