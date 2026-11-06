"""Change notify (watch.py): the NAS reports a change, the gateway re-reads only
that folder, and the change feed has it within about a second. Measured need,
In testing, one full rescan of a large all-shares root took over an hour, so a
file saved on the NAS could take over an hour to reach a Mac.

The LocalBackend's watch reports nothing by itself: each test says what the NAS
reported (backend.emit / backend.overflow), so every case is deterministic.
"""
import time

from conftest import all_shares, child, rooted


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    return fn()


def on(env, **kw):
    s = env.gw.s
    s.notify, s.notify_debounce_s, s.notify_folder_min_s = True, 0.05, 0.0
    s.notify_move_grace_s = 0.3
    for k, v in kw.items():
        setattr(s, k, v)


def latest(env, h):
    return env.public.get("/v1/changes/latest", headers=h).json()["anchor"]


def feed(env, h, anchor, wait=0):
    r = env.public.get("/v1/changes", params={"anchor": anchor, "wait": wait}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def watching(env, h, root_id):
    """A feed request starts the watches; wait until they are all up."""
    feed(env, h, latest(env, h))
    assert wait_for(lambda: env.gw.watches.since(root_id)), "watches never came up"


def listed_shares(env):
    """All-shares root with its share list read once (as the Mac does on first open)."""
    h, root = all_shares(env)
    env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=h)
    return h, root


def item_named(env, root_id, name):
    return env.gw.db.one("SELECT * FROM items WHERE root_id=? AND name=? AND deleted=0",
                         (root_id, name))


class Counting:
    """Wraps a session; counts listings per path."""

    def __init__(self, inner):
        self.inner, self.lists = inner, []

    def scandir(self, share, path):
        self.lists.append(f"{share}/{path}".rstrip("/"))
        return self.inner.scandir(share, path)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def counting(env, root_id):
    rw = env.gw.watches.get(root_id)
    rw.session = Counting(rw.session)
    return rw.session


# ── the basic promise ─────────────────────────────────────────────────────

def test_new_file_reaches_the_feed_without_a_rescan(env):
    on(env)
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    child(env.public, h, docs["id"], "a.txt")             # Docs listed once, as Finder does
    watching(env, h, root["id"])
    a0 = latest(env, h)
    (env.nas / "home" / "alice" / "Docs" / "new.txt").write_bytes(b"from the NAS")
    t0 = time.time()
    env.gw.backend.emit("home", "alice/Docs/new.txt")
    got = feed(env, h, a0, wait=5)
    assert [u["name"] for u in got["updated"]] == ["new.txt"]
    assert got["updated"][0]["parent_id"] == docs["id"]
    assert time.time() - t0 < 2
    assert env.gw.db.one("SELECT last_scan_at FROM roots WHERE id=?",
                         (root["id"],))["last_scan_at"] is None     # no rescan ran


def test_folders_new_to_the_index_are_reached_through_their_ancestors(env):
    """A whole tree copied in over SMB: every level gets indexed, shallowest first,
    even where the NAS reported only the deepest file."""
    on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    deep = env.nas / "home" / "alice" / "Docs" / "x" / "y" / "z"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_bytes(b"d")
    env.gw.backend.emit("home", "alice/Docs/x/y/z/deep.txt")
    assert wait_for(lambda: item_named(env, root["id"], "deep.txt"))
    z = item_named(env, root["id"], "z")
    assert item_named(env, root["id"], "deep.txt")["parent_id"] == z["id"]


def test_delete_on_the_nas(env):
    on(env)
    h, root = rooted(env)
    a = item_named(env, root["id"], "a.txt") or child(
        env.public, h, child(env.public, h, root["root_item_id"], "Docs")["id"], "a.txt")
    watching(env, h, root["id"])
    a0 = latest(env, h)
    (env.nas / "home" / "alice" / "Docs" / "a.txt").unlink()
    env.gw.backend.emit("home", "alice/Docs/a.txt")
    got = feed(env, h, a0, wait=5)
    assert got["deleted"] == [a["id"]]


def test_move_between_folders_keeps_the_item_id(env):
    """Both folders are reported; whichever is read first, the file is the same item
    afterwards (missing items are only deleted once the whole batch is read)."""
    on(env, notify_debounce_s=0.3)
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    (env.nas / "home" / "alice" / "Other").mkdir()
    child(env.public, h, root["root_item_id"], "Other")
    watching(env, h, root["id"])
    (env.nas / "home" / "alice" / "Docs" / "a.txt").rename(env.nas / "home" / "alice" / "Other" / "a.txt")
    env.gw.backend.emit("home", "alice/Docs/a.txt", "alice/Other/a.txt")
    assert wait_for(lambda: (r := env.gw.db.one("SELECT * FROM items WHERE id=?", (a["id"],)))
                    and r["parent_id"] != docs["id"])
    r = env.gw.db.one("SELECT * FROM items WHERE id=?", (a["id"],))
    assert not r["deleted"] and r["name"] == "a.txt"
    assert len(env.gw.db.all("SELECT 1 FROM items WHERE name='a.txt' AND deleted=0")) == 1


def test_a_burst_is_one_listing_per_folder(env):
    on(env, notify_debounce_s=0.3)
    h, root = rooted(env)
    watching(env, h, root["id"])
    sess = counting(env, root["id"])
    for i in range(50):
        (env.nas / "home" / "alice" / "Docs" / f"f{i}.txt").write_bytes(b"x")
        env.gw.backend.emit("home", f"alice/Docs/f{i}.txt")
    assert wait_for(lambda: item_named(env, root["id"], "f49.txt"))
    time.sleep(0.4)
    assert sess.lists.count("home/alice/Docs") == 1


def test_a_quiet_folder_never_waits(env):
    """Smoke on a real NAS, first version: every re-read within 10 s waited, so a
    folder made 2 s after a file in the same parent took 9.8 s to show."""
    on(env, notify_folder_min_s=5.0)
    h, root = rooted(env)
    watching(env, h, root["id"])
    for name in ("one.txt", "two.txt"):
        (env.nas / "home" / "alice" / "Docs" / name).write_bytes(b"1")
        t0 = time.time()
        env.gw.backend.emit("home", f"alice/Docs/{name}")
        assert wait_for(lambda: item_named(env, root["id"], name))
        assert time.time() - t0 < 1


def test_a_busy_folder_is_re_read_at_most_every_notify_folder_min_s(env):
    """A Time Machine backup rewrites one folder many times a second: after
    BUSY_READS quick re-reads the folder waits notify_folder_min_s."""
    from loft_gateway.watch import BUSY_READS
    on(env, notify_folder_min_s=1.0)
    h, root = rooted(env)
    watching(env, h, root["id"])
    sess = counting(env, root["id"])
    for i in range(BUSY_READS):
        (env.nas / "home" / "alice" / "Docs" / f"q{i}.txt").write_bytes(b"1")
        env.gw.backend.emit("home", f"alice/Docs/q{i}.txt")
        assert wait_for(lambda: item_named(env, root["id"], f"q{i}.txt"))
    t1 = time.time()
    (env.nas / "home" / "alice" / "Docs" / "held.txt").write_bytes(b"2")
    env.gw.backend.emit("home", "alice/Docs/held.txt")
    time.sleep(0.3)
    assert not item_named(env, root["id"], "held.txt")       # held back, not lost
    assert wait_for(lambda: item_named(env, root["id"], "held.txt"))
    assert time.time() - t1 >= 0.5
    assert sess.lists.count("home/alice/Docs") == BUSY_READS + 1


def test_move_whose_halves_arrive_in_separate_batches_keeps_the_item_id(env):
    """Smoke on a real NAS, first version: the source folder was read in one batch,
    the destination in the next, and the file came back as a new item."""
    on(env, notify_move_grace_s=1.0)
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    (env.nas / "home" / "alice" / "Other").mkdir()
    child(env.public, h, root["root_item_id"], "Other")
    watching(env, h, root["id"])
    (env.nas / "home" / "alice" / "Docs" / "a.txt").rename(env.nas / "home" / "alice" / "Other" / "a.txt")
    env.gw.backend.emit("home", "alice/Docs/a.txt")
    time.sleep(0.3)                                   # source read on its own
    assert not env.gw.db.one("SELECT deleted FROM items WHERE id=?", (a["id"],))["deleted"]
    env.gw.backend.emit("home", "alice/Other/a.txt")
    assert wait_for(lambda: env.gw.db.one("SELECT parent_id FROM items WHERE id=?",
                                          (a["id"],))["parent_id"] != docs["id"])
    time.sleep(1.2)
    r = env.gw.db.one("SELECT * FROM items WHERE id=?", (a["id"],))
    assert not r["deleted"]
    assert len(env.gw.db.all("SELECT 1 FROM items WHERE name='a.txt' AND deleted=0")) == 1


def test_gone_and_back_unchanged_is_not_deleted(env):
    on(env, notify_move_grace_s=0.5)
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a = child(env.public, h, docs["id"], "a.txt")
    watching(env, h, root["id"])
    f = env.nas / "home" / "alice" / "Docs" / "a.txt"
    f.rename(env.nas / "home" / "alice" / "a.txt.away")
    env.gw.backend.emit("home", "alice/Docs/a.txt")
    time.sleep(0.2)
    (env.nas / "home" / "alice" / "a.txt.away").rename(f)          # straight back, unchanged
    env.gw.backend.emit("home", "alice/Docs/a.txt")
    time.sleep(1.0)
    assert not env.gw.db.one("SELECT deleted FROM items WHERE id=?", (a["id"],))["deleted"]


def test_excluded_names_and_the_meta_area_are_not_re_read(env):
    on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    sess = counting(env, root["id"])
    env.gw.backend.emit("home", "alice/Docs/.DS_Store", "alice/Docs/~$report.docx",
                        "alice/.loft/uploads/u1", "alice/Docs/._a.txt")
    time.sleep(0.4)
    assert sess.lists == []


def test_reports_outside_a_one_folder_root_are_ignored(env):
    on(env)
    h, root = rooted(env)                         # root = home/alice
    watching(env, h, root["id"])
    sess = counting(env, root["id"])
    (env.nas / "home" / "bob" / "b.txt").write_bytes(b"b")
    env.gw.backend.emit("home", "bob/b.txt")
    time.sleep(0.4)
    assert sess.lists == []
    assert not item_named(env, root["id"], "b.txt")


def test_blocked_paths_are_never_read_even_when_reported(env):
    on(env)
    h, root = all_shares(env)
    watching(env, h, root["id"])
    assert {w.share for w in env.gw.watches.get(root["id"]).watches.values()} == {
        "Family", "home", "media"}           # backup is a blocked share: no watch
    sess = counting(env, root["id"])
    creds = env.nas / "Family" / "Passwords" / "vault"
    (creds / "new_key").write_bytes(b"K")
    env.gw.backend.emit("Family", "Passwords/vault/new_key",
                        "Admin/router-backups/router.tar")
    time.sleep(0.4)
    assert sess.lists == []
    assert not item_named(env, root["id"], "new_key")
    # the folder holding a blocked path is read; the blocked entry stays out
    (env.nas / "Family" / "visible.txt").write_bytes(b"v")
    env.gw.backend.emit("Family", "visible.txt", "Passwords")
    assert wait_for(lambda: item_named(env, root["id"], "visible.txt"))
    assert not item_named(env, root["id"], "Passwords")


def test_all_shares_root_one_watch_per_share(env):
    on(env)
    h, root = all_shares(env)
    watching(env, h, root["id"])
    (env.nas / "media" / "Film" / "trailer.mov").write_bytes(b"t")
    env.gw.backend.emit("media", "Film/trailer.mov")
    assert wait_for(lambda: item_named(env, root["id"], "trailer.mov"))
    film = item_named(env, root["id"], "Film")
    assert item_named(env, root["id"], "trailer.mov")["parent_id"] == film["id"]


# ── overflow, lost watches, sessions ──────────────────────────────────────

def test_overflow_rescans_that_share(env):
    on(env)
    h, root = listed_shares(env)
    watching(env, h, root["id"])
    (env.nas / "media" / "Film" / "unreported.mov").write_bytes(b"u")
    (env.nas / "home" / "unreported_too.txt").write_bytes(b"u")
    env.gw.backend.overflow("media")
    assert wait_for(lambda: item_named(env, root["id"], "unreported.mov"))
    time.sleep(0.2)
    assert not item_named(env, root["id"], "unreported_too.txt")    # only that share


def test_overflow_rescans_are_rate_limited(env):
    on(env, notify_overflow_rescan_s=0.6)
    h, root = listed_shares(env)
    watching(env, h, root["id"])
    env.gw.backend.overflow("media")
    time.sleep(0.2)
    (env.nas / "media" / "late.mov").write_bytes(b"l")
    env.gw.backend.overflow("media")
    time.sleep(0.2)
    assert not item_named(env, root["id"], "late.mov")             # waits its turn
    assert wait_for(lambda: item_named(env, root["id"], "late.mov"))


def test_overflow_rescans_never_pile_up(env):
    """Review F1 (her repro): a share rescan slower than the limit while the
    share keeps overflowing used to queue one more whole-share rescan per period
    (9 for one share in 3 s). Now: one running, one waiting, limit from the end."""
    import threading
    on(env, notify_overflow_rescan_s=0.3)
    h, root = listed_shares(env)
    watching(env, h, root["id"])
    real, calls = env.gw.rescan, []

    def slow(*a, **k):
        calls.append(time.time())
        time.sleep(1.0)
        return real(*a, **k)
    env.gw.rescan = slow
    peak, end = 0, time.time() + 3.0
    while time.time() < end:
        env.gw.backend.overflow("media")
        peak = max(peak, len([t for t in threading.enumerate()
                              if t.name.startswith("notify-over-")]))
        time.sleep(0.1)
    assert peak == 1
    assert len(calls) <= 3                             # 1.0 s each + 0.3 s after each end
    assert all(b - a >= 1.25 for a, b in zip(calls, calls[1:]))


def test_overflow_rescan_waits_for_the_full_pass_and_is_dropped_if_covered(env, monkeypatch):
    import threading
    from loft_gateway import watch
    monkeypatch.setattr(watch, "OVER_RETRY_S", 0.2)
    on(env)
    h, root = listed_shares(env)
    watching(env, h, root["id"])
    calls = []
    real = env.gw.rescan
    env.gw.rescan = lambda *a, **k: calls.append(k.get("start_id")) or real(*a, **k)
    lock = env.gw._scan_locks.setdefault(root["id"], threading.Lock())
    # 1. a full pass running: the overflow rescan waits, and runs after it
    lock.acquire()
    env.gw.backend.overflow("media")
    time.sleep(0.5)
    assert calls == []
    lock.release()
    assert wait_for(lambda: len(calls) == 1)
    # 2. a whole pass STARTED after the overflow: nothing left to do
    time.sleep(0.1)
    lock.acquire()
    env.gw.backend.overflow("home")
    time.sleep(0.1)
    env.gw._full_start[root["id"]] = time.time()      # the pass that held the lock
    lock.release()
    time.sleep(0.6)
    assert len(calls) == 1


def test_an_expensive_busy_folder_waits_in_proportion_to_its_read(env):
    """Review F2: a 65k-entry maildir under a mail sync would be listed
    every notify_folder_min_s; its floor is now COST_FACTOR x its read time."""
    from loft_gateway.watch import BUSY_READS, COST_FACTOR
    on(env, notify_folder_min_s=0.2)
    h, root = rooted(env)
    watching(env, h, root["id"])
    real = env.gw.reconcile

    def slow(*a, **k):
        time.sleep(0.05)                              # floor: 20 x 0.05 = 1.0 s, not 0.2
        return real(*a, **k)
    env.gw.reconcile = slow
    for i in range(BUSY_READS):
        (env.nas / "home" / "alice" / "Docs" / f"q{i}.txt").write_bytes(b"1")
        env.gw.backend.emit("home", f"alice/Docs/q{i}.txt")
        assert wait_for(lambda: item_named(env, root["id"], f"q{i}.txt"))
    t1 = time.time()
    (env.nas / "home" / "alice" / "Docs" / "held.txt").write_bytes(b"2")
    env.gw.backend.emit("home", "alice/Docs/held.txt")
    time.sleep(0.5)
    assert not item_named(env, root["id"], "held.txt")
    assert wait_for(lambda: item_named(env, root["id"], "held.txt"))
    assert time.time() - t1 >= COST_FACTOR * 0.05 * 0.7


def test_long_interval_only_while_watched_and_after_a_pass(env):
    on(env, rescan_interval_watched_s=6 * 3600)
    h, root = rooted(env)
    row = lambda: env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))  # noqa: E731
    s = env.gw.s
    assert env.gw.rescan_interval(row()) == s.rescan_interval_s     # nothing watched yet
    watching(env, h, root["id"])
    assert env.gw.rescan_interval(row()) == s.rescan_interval_s     # no pass since the watch
    env.gw.rescan(row(), env.gw.watches.get(root["id"]).session)
    assert env.gw.rescan_interval(row()) == s.rescan_interval_watched_s
    env.gw.backend.drop_watches()                                   # connection dropped
    assert wait_for(lambda: env.gw.watches.since(root["id"]) is None)
    assert env.gw.rescan_interval(row()) == s.rescan_interval_s
    # back up (after RETRY_S; forced here): a gap, so a pass must run again first
    env.gw.watches.get(root["id"]).retry_at.clear()
    env.gw.watches._checked.clear()
    watching(env, h, root["id"])
    assert env.gw.rescan_interval(row()) == s.rescan_interval_s


def test_a_subtree_rescan_is_not_a_full_pass(env):
    on(env)
    h, root = listed_shares(env)
    watching(env, h, root["id"])
    r = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    media = item_named(env, root["id"], "media")
    env.gw.rescan(r, env.gw.watches.get(root["id"]).session, start_id=media["id"])
    r = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    assert r["last_scan_at"] is None
    assert env.gw.rescan_interval(r) == env.gw.s.rescan_interval_s


def test_nas_refusing_notify_falls_back_to_the_rescan(env):
    on(env)
    env.gw.backend.watch_error = "STATUS_NOT_SUPPORTED"
    h, root = rooted(env)
    feed(env, h, latest(env, h))
    time.sleep(0.2)
    row = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    assert env.gw.watches.since(root["id"]) is None
    assert env.gw.rescan_interval(row) == env.gw.s.rescan_interval_s


def test_a_watch_never_outlives_its_session(env):
    on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    rw = env.gw.watches.get(root["id"])
    dev = env.public.get("/v1/me", headers=h).json()["device_id"]
    env.gw.expire_sessions()                         # not idle yet: kept
    assert env.gw.watches.get(root["id"]) is rw
    env.gw._drop_session(dev)                        # e.g. revoke, or idle expiry
    assert env.gw.watches.get(root["id"]) is None
    assert rw.stopping                                # at once; the opens close in the background
    assert wait_for(lambda: env.gw.backend.watches == [])
    # the next request (a new session) brings them back
    watching(env, h, root["id"])
    assert env.gw.watches.get(root["id"]) is not rw


def test_revoked_device_stops_its_watches(env):
    on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    dev = env.public.get("/v1/me", headers=h).json()["device_id"]
    env.gw.revoke(dev, by_user="alice")
    assert env.gw.watches.get(root["id"]) is None
    assert wait_for(lambda: env.gw.backend.watches == [])


def test_a_revoke_from_another_process_stops_the_watches(env):
    """Review of bef7c4a, N1: `loft-admin revoke` and `loft-gateway mcp revoke`
    run in their own process, so only the row changes. The janitor's next pass
    (expire_sessions, every minute) drops the session and its watches."""
    on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    dev = env.public.get("/v1/me", headers=h).json()["device_id"]
    with env.gw.db.tx() as c:                        # what admin.py's revoke does
        c.execute("UPDATE devices SET revoked_at=?, wrapped_cred=x'' WHERE id=?",
                  (time.time(), dev))
    assert env.gw.watches.get(root["id"]) is not None
    env.gw.expire_sessions()
    assert env.gw.watches.get(root["id"]) is None
    assert dev not in env.gw._sessions
    assert wait_for(lambda: env.gw.backend.watches == [])
    assert env.public.get("/v1/me", headers=h).status_code == 401


def test_the_janitor_keeps_live_sessions(env):
    on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    rw = env.gw.watches.get(root["id"])
    dev = env.public.get("/v1/me", headers=h).json()["device_id"]
    env.gw.expire_sessions()
    assert dev in env.gw._sessions and env.gw.watches.get(root["id"]) is rw


def test_shutdown_stops_the_watches(env):
    on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    env.gw.release_waiters()
    assert wait_for(lambda: env.gw.backend.watches == [])


def test_a_failed_read_does_not_set_the_busy_floor(env):
    """review of 3027669, n1: one SMB timeout gave the folder 20 x the timeout."""
    on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    rw = env.gw.watches.get(root["id"])
    real = env.gw.reconcile

    def fail(*a, **k):
        time.sleep(0.2)
        from loft_gateway.storage.base import StorageError
        raise StorageError("timed out")
    env.gw.reconcile = fail
    env.gw.backend.emit("home", "alice/Docs/x.txt")
    assert wait_for(lambda: rw.reads.get(("alice", "docs")) or rw.reads.get(("Docs",))
                    or any(rw.reads.values()))
    assert all(c < 0.1 for c in rw.cost.values())
    env.gw.reconcile = real


def test_a_rescan_stops_when_its_session_is_closed(env):
    """review of 3027669, n2: a long pass kept going (and warning) on a dead session."""
    h, root = rooted(env)
    dev = env.public.get("/v1/me", headers=h).json()["device_id"]
    sess = env.gw._sessions[dev][0]
    row = env.gw.db.one("SELECT * FROM roots WHERE id=?", (root["id"],))
    real, n = env.gw.reconcile, []

    def count(*a, **k):
        n.append(1)
        if len(n) == 1:
            env.gw._drop_session(dev)                 # signed out mid-pass
        return real(*a, **k)
    env.gw.reconcile = count
    env.gw.rescan(row, sess)
    assert len(n) == 1
