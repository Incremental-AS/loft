"""NAS agent (nas_agent.py): changes made on the NAS by anything, not only over SMB.

The agent itself (nas-agent/loft_nas_agent.py) is tested against a real btrfs in
nas-agent/test_btrfs.py; here a test plays the agent: it signs requests like the
agent does and reports what "changed" on the LocalBackend's disk, which no SMB
change notify ever reports (nothing calls backend.emit).
"""
import base64
import hashlib
import hmac
import json
import time

from conftest import all_shares, child, rooted
from test_notify import feed, item_named, latest, on, wait_for, watching

KEY = b"k" * 32


class Agent:
    def __init__(self, env, key=KEY):
        self.env, self.key, self.ts = env, key, int(time.time() * 1000)

    def post(self, path, body, client=None, key=None, ts=None):
        data = json.dumps(body).encode()
        self.ts = ts if ts is not None else max(self.ts + 1, int(time.time() * 1000))
        msg = b"POST\n" + path.encode() + b"\n" + str(self.ts).encode() + b"\n" + data
        sig = hmac.new(key or self.key, msg, hashlib.sha256).hexdigest()
        return (client or self.env.direct).post(
            path, content=data, headers={"Authorization": f"LoftAgent {self.ts}.{sig}",
                                         "Content-Type": "application/json"})

    def sync(self, *shares, gen=100, uuid="u-1", failing=()):
        """shares: "name" or ("name", "user"); failing: names whose rounds fail."""
        body = {"agent": "2", "host": "nas", "shares": []}
        for s in shares:
            name, user = (s, "") if isinstance(s, str) else s
            body["shares"].append({"id": f"{name}\n{user}" if user else name, "share": name,
                                   "user": user, "uuid": uuid, "tree_id": 300,
                                   "generation": gen, "ok": name not in failing})
        r = self.post("/v1/nas-agent/sync", body)
        assert r.status_code == 200, r.text
        return r.json()["cursors"]

    def report(self, sid, since, until, dirs, final=True, overflow=False, uuid="u-1"):
        return self.post("/v1/nas-agent/changes", {"id": sid, "uuid": uuid, "since": since,
                                                   "until": until, "dirs": dirs, "final": final,
                                                   "overflow": overflow})


def agent_on(env, **kw):
    key = env.tmp / "agent.key"
    key.write_bytes(base64.b64encode(KEY))
    env.gw.s.nas_agent_key_path = str(key)
    from loft_gateway.nas_agent import NasAgent
    env.gw.agent = NasAgent(env.gw)
    on(env, **kw)
    return Agent(env)


def root_row(env, rid):
    return env.gw.db.one("SELECT * FROM roots WHERE id=?", (rid,))


# ── the door ──────────────────────────────────────────────────────────────

def test_off_unless_a_key_is_configured(env):
    r = Agent(env).post("/v1/nas-agent/sync", {"shares": []})
    assert r.status_code == 404


def test_direct_listener_only(env):
    a = agent_on(env)
    assert a.post("/v1/nas-agent/sync", {"shares": []}, client=env.public).status_code == 404
    data = b'{"shares": []}'
    r = env.direct.post("/v1/nas-agent/sync", content=data,
                        headers={"CF-Connecting-IP": "1.2.3.4", "Authorization": "x"})
    assert r.status_code == 404


def test_signature_timestamps_and_replay(env):
    a = agent_on(env)
    assert a.post("/v1/nas-agent/sync", {"shares": []}, key=b"x" * 32).status_code == 401
    r = env.direct.post("/v1/nas-agent/sync", content=b"{}")
    assert r.status_code == 401
    old = int(time.time() * 1000) - 3_600_000
    assert a.post("/v1/nas-agent/sync", {"shares": []}, ts=old).status_code == 401
    assert a.post("/v1/nas-agent/sync", {"shares": []}).status_code == 200
    # the same timestamp again (a replay) and an older one are refused
    assert a.post("/v1/nas-agent/sync", {"shares": []}, ts=a.ts).status_code == 401
    assert a.post("/v1/nas-agent/sync", {"shares": []}, ts=a.ts - 5).status_code == 401
    # signed before the gateway started (a replay across a restart): refused
    from loft_gateway.nas_agent import NasAgent
    env.gw.agent = NasAgent(env.gw)
    assert a.post("/v1/nas-agent/sync", {"shares": []}, ts=a.ts + 1).status_code == 401
    assert a.post("/v1/nas-agent/sync", {"shares": []}).status_code == 200
    # the body is signed: the same signature on another body fails
    data = json.dumps({"shares": []}).encode()
    ts = a.ts + 10
    sig = hmac.new(KEY, b"POST\n/v1/nas-agent/sync\n" + str(ts).encode() + b"\n" + data,
                   hashlib.sha256).hexdigest()
    r = env.direct.post("/v1/nas-agent/sync", content=b'{"shares": [1]}',
                        headers={"Authorization": f"LoftAgent {ts}.{sig}"})
    assert r.status_code == 401


# ── cursors ───────────────────────────────────────────────────────────────

def test_the_gateway_keeps_the_cursor(env):
    a = agent_on(env)
    assert a.sync("media", gen=100) == {"media": 100}
    assert a.sync("media", gen=180) == {"media": 100}           # ours, not the agent's
    r = a.report("media", 90, 150, ["Film"])
    assert r.status_code == 409 and r.json()["cursor"] == 100   # the agent starts over
    assert a.report("media", 100, 150, ["Film"], final=False).status_code == 200
    assert a.sync("media") == {"media": 100}                   # moves with the last chunk only
    assert a.report("media", 100, 150, ["Film/x"]).status_code == 200
    assert a.sync("media") == {"media": 150}
    rows = env.gw.db.all("SELECT kind, path FROM nas_changes ORDER BY seq")
    assert [(r["kind"], r["path"]) for r in rows] == [("dir", "Film"), ("dir", "Film/x")]


def test_a_folder_reported_again_moves_to_the_end(env):
    a = agent_on(env)
    a.sync("media")
    a.report("media", 100, 101, ["Film", "Other"])
    a.report("media", 101, 102, ["Film"])
    rows = env.gw.db.all("SELECT path FROM nas_changes ORDER BY seq")
    assert [r["path"] for r in rows] == ["Other", "Film"]


def test_a_recreated_share_gets_a_new_cursor_and_a_rescan(env):
    a = agent_on(env)
    a.sync("media", gen=100)
    assert a.sync("media", gen=7, uuid="u-2") == {"media": 7}
    rows = env.gw.db.all("SELECT kind FROM nas_changes")
    assert [r["kind"] for r in rows] == ["all"]
    assert a.report("media", 7, 8, ["Film"]).status_code == 409   # old uuid
    assert a.report("media", 7, 8, ["Film"], uuid="u-2").status_code == 200


def test_names_come_back_in_the_gateways_spelling(env):
    """On disk a Mac name keeps Samba's private-use characters (U+F022 for ':')."""
    a = agent_on(env)
    a.sync("media")
    a.report("media", 100, 101, ["Film/Week 2"])
    assert env.gw.db.one("SELECT path FROM nas_changes")["path"] == "Film/Week :2"


# ── the promise: a change no SMB notify reports reaches the feed ──────────

def test_a_file_written_on_the_nas_itself_reaches_the_feed(env):
    a = agent_on(env)
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    child(env.public, h, docs["id"], "a.txt")
    watching(env, h, root["id"])
    a.sync("home")
    a0 = latest(env, h)
    (env.nas / "home" / "alice" / "Docs" / "hello.mobileconfig").write_bytes(b"<plist/>")
    t0 = time.time()
    assert a.report("home", 100, 101, ["alice/Docs"]).status_code == 200
    got = feed(env, h, a0, wait=5)
    assert [u["name"] for u in got["updated"]] == ["hello.mobileconfig"]
    assert got["updated"][0]["parent_id"] == docs["id"]
    assert time.time() - t0 < 2
    # applied, and remembered as applied
    seq = env.gw.db.one("SELECT MAX(seq) s FROM nas_changes")["s"]
    assert wait_for(lambda: root_row(env, root["id"])["agent_seq"] == seq)


def test_another_users_folder_and_other_shares_are_left_alone(env):
    a = agent_on(env)
    h, root = rooted(env)                                  # alice: home/alice
    watching(env, h, root["id"])
    a.sync("home", ("Personal-Drive", "bob"))
    (env.nas / "home" / "bob" / "b.txt").write_bytes(b"b")
    a.report("home", 100, 101, ["bob"])                    # beside alice's root folder
    a.report("Personal-Drive\nbob", 100, 101, ["alice"])   # bob's own folder of the share
    seq = env.gw.db.one("SELECT MAX(seq) s FROM nas_changes")["s"]
    assert wait_for(lambda: root_row(env, root["id"])["agent_seq"] == seq)
    assert item_named(env, root["id"], "b.txt") is None


def test_a_personal_share_reaches_its_own_user(env):
    a = agent_on(env)
    h, root = all_shares(env)                               # carol: home, media, Family
    env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=h)
    watching(env, h, root["id"])
    a.sync(("home", "carol"), ("home", "dave"))
    (env.nas / "home" / "bob" / "Privat").mkdir()
    (env.nas / "home" / "bob" / "Privat" / "p.txt").write_bytes(b"p")
    a.report("home\ndave", 100, 101, ["bob/Privat"])
    time.sleep(0.5)
    assert item_named(env, root["id"], "p.txt") is None
    a.report("home\ncarol", 100, 101, ["bob/Privat"])
    assert wait_for(lambda: item_named(env, root["id"], "p.txt"))


def test_a_user_away_catches_up_when_back(env):
    """Reports made while nobody of that account is signed in wait in the log."""
    a = agent_on(env)
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    a.sync("home")
    env.gw.rescan(root_row(env, root["id"]), _session(env, h))
    assert root_row(env, root["id"])["agent_since"] is not None
    (env.nas / "home" / "alice" / "Docs" / "while-away.txt").write_bytes(b"x")
    assert a.report("home", 100, 101, ["alice/Docs"]).status_code == 200
    assert item_named(env, root["id"], "while-away.txt") is None   # no watch running
    watching(env, h, root["id"])                                    # the Mac is back
    assert wait_for(lambda: item_named(env, root["id"], "while-away.txt"))
    assert item_named(env, root["id"], "while-away.txt")["parent_id"] == docs["id"]


def _session(env, h):
    caller = env.gw.authenticate(h["Authorization"][7:], "127.0.0.1", "public")
    return caller.session


def test_overflow_rescans_the_share(env):
    a = agent_on(env)
    h, root = all_shares(env)
    env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=h)
    watching(env, h, root["id"])
    a.sync("media")
    deep = env.nas / "media" / "Film" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "deep.mov").write_bytes(b"m")
    assert a.report("media", 100, 101, [], overflow=True).status_code == 200
    assert wait_for(lambda: item_named(env, root["id"], "deep.mov"))
    seq = env.gw.db.one("SELECT MAX(seq) s FROM nas_changes")["s"]
    assert wait_for(lambda: root_row(env, root["id"])["agent_seq"] == seq)


def test_progress_waits_for_the_folder_read(env):
    """agent_seq never passes a folder that hasn't been read yet (a busy folder
    held back, a slow listing): after a restart the log is applied from there."""
    a = agent_on(env, notify_debounce_s=1.0)
    h, root = rooted(env)
    watching(env, h, root["id"])
    a.sync("home")
    a.report("home", 100, 101, ["alice/Docs"])
    time.sleep(0.3)                                        # pulled, not read yet (debounce)
    assert root_row(env, root["id"])["agent_seq"] == 0
    seq = env.gw.db.one("SELECT MAX(seq) s FROM nas_changes")["s"]
    assert wait_for(lambda: root_row(env, root["id"])["agent_seq"] == seq)


# ── coverage: no periodic full pass while the log is complete ─────────────

def test_covered_after_a_full_pass_while_the_agent_reports(env):
    a = agent_on(env, rescan_interval_watched_s=6 * 3600)
    h, root = rooted(env)
    watching(env, h, root["id"])
    s = env.gw.s
    assert env.gw.rescan_interval(root_row(env, root["id"])) == s.rescan_interval_s
    a.sync("home")
    # the agent began after the last pass: not yet
    assert env.gw.rescan_interval(root_row(env, root["id"])) == s.rescan_interval_s
    env.gw.rescan(root_row(env, root["id"]), env.gw.watches.get(root["id"]).session)
    assert env.gw.rescan_interval(root_row(env, root["id"])) == float("inf")
    s.rescan_interval_agent_s = 7 * 86400
    assert env.gw.rescan_interval(root_row(env, root["id"])) == 7 * 86400
    # not heard from: the old intervals until it is back (nothing it reports is lost)
    env.gw.agent.seen_at = time.time() - s.nas_agent_stale_s - 1
    assert env.gw.rescan_interval(root_row(env, root["id"])) == s.rescan_interval_watched_s
    a.sync("home")
    assert env.gw.rescan_interval(root_row(env, root["id"])) == 7 * 86400


def test_not_covered_while_a_share_is_unreported(env):
    a = agent_on(env)
    h, root = all_shares(env)
    env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=h)
    watching(env, h, root["id"])
    a.sync("home", "media")                                 # Family missing
    env.gw.rescan(root_row(env, root["id"]), env.gw.watches.get(root["id"]).session)
    assert env.gw.rescan_interval(root_row(env, root["id"])) == env.gw.s.rescan_interval_s
    a.sync("home", "media", "Family")                       # added later: rescanned alone,
    assert env.gw.rescan_interval(root_row(env, root["id"])) == float("inf")   # covered
    assert [r["kind"] for r in env.gw.db.all("SELECT kind FROM nas_changes")] == ["all"]


def test_a_root_away_longer_than_the_log_keeps_is_not_covered(env):
    a = agent_on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    a.sync("home")
    env.gw.rescan(root_row(env, root["id"]), env.gw.watches.get(root["id"]).session)
    assert env.gw.rescan_interval(root_row(env, root["id"])) == float("inf")
    with env.gw.db.tx() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES ('nas_changes_floor', '999')")
    assert env.gw.rescan_interval(root_row(env, root["id"])) == env.gw.s.rescan_interval_s


def test_the_log_is_pruned_after_30_days(env):
    a = agent_on(env)
    a.sync("media")
    a.report("media", 100, 101, ["Film", "Other"])
    with env.gw.db.tx() as c:
        c.execute("UPDATE nas_changes SET at=0 WHERE path='Film'")
    env.gw.agent.prune()
    assert [r["path"] for r in env.gw.db.all("SELECT path FROM nas_changes")] == ["Other"]
    assert env.gw.agent.floor() == 1


def test_a_share_gone_for_a_week_is_forgotten(env):
    a = agent_on(env)
    a.sync("media", "old", "quiet")
    with env.gw.db.tx() as c:
        c.execute("UPDATE agent_shares SET seen_at=? WHERE id='old'", (time.time() - 8 * 86400,))
        c.execute("UPDATE agent_shares SET seen_at=? WHERE id='quiet'", (time.time() - 6 * 86400,))
    env.gw.agent.prune()
    assert sorted(r["id"] for r in env.gw.db.all("SELECT id FROM agent_shares")) == ["media", "quiet"]


def _covered_alice(env, a):
    """alice's root with Docs/{a.txt, b.txt, c.txt}, covered by the agent."""
    for n in ("b.txt", "c.txt"):
        (env.nas / "home" / "alice" / "Docs" / n).write_bytes(b"")
    h, root = rooted(env)
    docs = child(env.public, h, root["root_item_id"], "Docs")
    watching(env, h, root["id"])
    a.sync("home")
    env.gw.rescan(root_row(env, root["id"]), env.gw.watches.get(root["id"]).session)
    assert env.gw.rescan_interval(root_row(env, root["id"])) == float("inf")
    return h, root, docs


def test_a_full_pass_on_a_covered_root_logs_each_correction(env, caplog):
    """What notify and the agent missed is logged item by item: the row before, the
    NAS now, and the agent's last report of the folder (F1, review_maja_ba880e0)."""
    import logging
    import os
    a = agent_on(env)
    h, root, docs = _covered_alice(env, a)
    d = env.nas / "home" / "alice" / "Docs"
    # reported by the agent and applied, then changed again without a report
    a.report("home", 100, 101, ["alice/Docs"])
    seq = env.gw.db.one("SELECT MAX(seq) s FROM nas_changes")["s"]
    assert wait_for(lambda: root_row(env, root["id"])["agent_seq"] == seq)
    (d / "a.txt").write_bytes(b"longer than before")
    os.utime(d / "a.txt", ns=(1_762_736_414_067_000_000, 1_762_736_414_067_000_000))
    (d / "new.txt").write_bytes(b"x")                       # before the unlink: a new inode
    (d / "b.txt").unlink()
    (env.nas / "home" / "alice" / "Other").mkdir()
    caplog.set_level(logging.INFO, logger="loft.rescan")
    st = env.gw.rescan(root_row(env, root["id"]), env.gw.watches.get(root["id"]).session)
    lines = [r.getMessage() for r in caplog.records if "full pass corrected" in r.getMessage()]
    assert st["corrected"] == 4 == len(lines), lines
    mod = next(x for x in lines if "modified alice/Docs/a.txt" in x)
    assert "size 5->18" in mod and "->2025-11-10T01:00:14.067Z" in mod and "file_id" in mod
    assert "row written 20" in mod
    assert "agent last reported alice/Docs at" in mod and f"log seq {seq}, applied" in mod
    assert any("deleted alice/Docs/b.txt: was size 0" in x for x in lines)
    assert any("new alice/Docs/new.txt: size 1" in x for x in lines)
    other = next(x for x in lines if "new alice/Other" in x)
    assert "agent never reported alice" in other


def test_a_full_pass_on_an_uncovered_root_logs_no_corrections(env, caplog):
    import logging
    a = agent_on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    a.sync("home")                                          # began after the last pass
    (env.nas / "home" / "alice" / "new.txt").write_bytes(b"x")
    caplog.set_level(logging.INFO, logger="loft.rescan")
    st = env.gw.rescan(root_row(env, root["id"]), env.gw.watches.get(root["id"]).session)
    assert st["changes"] >= 1 and "corrected" not in st
    assert not [r for r in caplog.records if "full pass corrected" in r.getMessage()]


def test_a_slow_folder_read_is_logged_with_its_path(env, caplog, monkeypatch):
    """F2 (review_maja_ba880e0): which folder held the root's queue, and where the
    time went."""
    import logging
    import loft_gateway.service as service
    a = agent_on(env)
    h, root, docs = _covered_alice(env, a)
    monkeypatch.setattr(service, "SLOW_READ_S", -1.0)
    caplog.set_level(logging.INFO)
    (env.nas / "home" / "alice" / "Docs" / "slow.txt").write_bytes(b"x")
    a.report("home", 100, 101, ["alice/Docs"])
    assert wait_for(lambda: item_named(env, root["id"], "slow.txt"))
    assert wait_for(lambda: any("re-read of Docs took" in r.getMessage() for r in caplog.records))
    msg = next(r.getMessage() for r in caplog.records if "re-read of Docs took" in r.getMessage())
    assert "4 entries; listing" in msg and "waiting for the index" in msg and "1 change(s)" in msg
    env.gw.rescan(root_row(env, root["id"]), env.gw.watches.get(root["id"]).session)
    assert any("folder alice/Docs took" in r.getMessage() for r in caplog.records)


def test_housekeeping_runs_without_full_passes(env):
    """Trash and uploads expire on their own clock, not after each full pass."""
    agent_on(env)
    env.gw.s.background_rescan = True
    h, root = rooted(env)
    calls = []
    env.gw.housekeeping = lambda r, s: calls.append(r["id"])
    env.gw.rescan = lambda *a, **k: calls.append("rescan")
    with env.gw.db.tx() as c:
        c.execute("UPDATE roots SET last_scan_at=? WHERE id=?", (time.time(), root["id"]))
    feed(env, h, latest(env, h))
    assert wait_for(lambda: calls == [root["id"]])          # no pass due, housekeeping is
    feed(env, h, latest(env, h))
    time.sleep(0.2)
    assert calls == [root["id"]]                            # once an hour


def test_a_share_new_to_an_all_shares_root_is_read(env):
    """No full pass will find it once the agent covers the root."""
    agent_on(env)
    h, root = all_shares(env)
    env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=h)
    watching(env, h, root["id"])
    (env.nas / "Projects" / "Q4").mkdir(parents=True)
    (env.nas / "Projects" / "Q4" / "plan.md").write_bytes(b"p")
    env.gw.backend.users["carol"]["shares"].append("Projects")
    env.gw._share_cache.clear()
    env.gw.watches._checked.clear()                        # the next feed request lists again
    feed(env, h, latest(env, h))
    assert wait_for(lambda: item_named(env, root["id"], "Projects"))
    assert wait_for(lambda: item_named(env, root["id"], "Q4"))


def test_a_gateway_restart_gives_the_agent_time_to_reconnect(env):
    """The first request after a restart must not start a full pass just because
    the agent hasn't reconnected yet."""
    a = agent_on(env)
    h, root = rooted(env)
    watching(env, h, root["id"])
    a.sync("home")
    env.gw.rescan(root_row(env, root["id"]), env.gw.watches.get(root["id"]).session)
    from loft_gateway.nas_agent import NasAgent
    env.gw.agent = NasAgent(env.gw)                         # as after a restart
    assert env.gw.rescan_interval(root_row(env, root["id"])) == float("inf")
    env.gw.agent.seen_at -= env.gw.s.nas_agent_stale_s + 1  # it never came back
    assert env.gw.rescan_interval(root_row(env, root["id"])) == env.gw.s.rescan_interval_s


def _covered_root(env, a, *shares):
    h, root = all_shares(env)
    env.public.get(f"/v1/items/{root['root_item_id']}/children", headers=h)
    watching(env, h, root["id"])
    a.sync(*shares)
    env.gw.rescan(root_row(env, root["id"]), env.gw.watches.get(root["id"]).session)
    assert env.gw.rescan_interval(root_row(env, root["id"])) == float("inf")
    return root


def _age_shares(env, *names, by=None):
    by = by or env.gw.s.nas_agent_stale_s + 1
    with env.gw.db.tx() as c:
        for n in names:
            c.execute("UPDATE agent_shares SET seen_at=seen_at-? WHERE id=?", (by, n))
    env.gw.agent.started -= by


def test_a_share_the_agent_stopped_watching_is_not_reported(env):
    """The agent still syncs, but one share dropped out of what it watches (its
    volume was gone when it looked): that share's roots go back to full passes."""
    a = agent_on(env)
    root = _covered_root(env, a, "home", "media", "Family")
    _age_shares(env, "home", "media", "Family")
    a.sync("home", "media")                                 # Family no longer watched
    assert env.gw.rescan_interval(root_row(env, root["id"])) == env.gw.s.rescan_interval_s
    a.sync("home", "media", "Family")                       # back: covered again
    assert env.gw.rescan_interval(root_row(env, root["id"])) == float("inf")


def test_a_share_whose_rounds_fail_is_not_reported(env):
    """The agent is alive but can't read one share: it says so, and that share's
    roots go back to full passes until its rounds go through again."""
    a = agent_on(env)
    root = _covered_root(env, a, "home", "media", "Family")
    _age_shares(env, "home", "media", "Family")
    a.sync("home", "media", "Family", failing=("media",))
    assert env.gw.rescan_interval(root_row(env, root["id"])) == env.gw.s.rescan_interval_s
    a.sync("home", "media", "Family")
    assert env.gw.rescan_interval(root_row(env, root["id"])) == float("inf")


def test_each_share_gets_the_restart_grace(env):
    a = agent_on(env)
    root = _covered_root(env, a, "home", "media", "Family")
    with env.gw.db.tx() as c:                               # last heard of long ago
        c.execute("UPDATE agent_shares SET seen_at=seen_at-?", (env.gw.s.nas_agent_stale_s + 1,))
    from loft_gateway.nas_agent import NasAgent
    env.gw.agent = NasAgent(env.gw)                         # a restart
    assert env.gw.rescan_interval(root_row(env, root["id"])) == float("inf")
