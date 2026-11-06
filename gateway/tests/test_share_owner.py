"""The share owner check (no NAS needed). On 2026-11-05 the UNAS served admin's
Personal-Drive to ohh and ohh's to admin; both indexes took the other account's
files in. Each account's share now keeps the owner of its root as first seen, and
a different owner later is refused before anything is listed, read or written."""
import time

import pytest

from loft_gateway import admin
from loft_gateway.storage import smb
from loft_gateway.storage.base import StorageError, WrongShareOwner

from conftest import rooted
from test_rescan import names, setup

OHH, ADMIN = "S-1-5-21-1-2-3-1000", "S-1-5-21-1-2-3-1004"


def test_first_owner_is_kept_and_another_refused(env):
    g = env.gw.check_share_owner
    g("ohh", "Personal-Drive", OHH)                       # first sight: noted
    g("OHH", "personal-drive", OHH)                       # same account and share
    g("admin", "Personal-Drive", ADMIN)                   # each account its own
    with pytest.raises(WrongShareOwner):
        g("ohh", "Personal-Drive", ADMIN)                 # the swap
    with pytest.raises(WrongShareOwner):
        g("admin", "Personal-Drive", OHH)
    a = [r for r in env.gw.db.all("SELECT * FROM audit WHERE event='share_owner_mismatch'")]
    assert [r["nas_user"] for r in a] == ["ohh", "admin"]
    assert env.gw.db.one("SELECT owner_sid FROM share_owners WHERE nas_user='ohh'")[0] == OHH


def test_unreadable_owner_is_let_through(env):
    env.gw.check_share_owner("ohh", "media", None)
    assert env.gw.db.one("SELECT COUNT(*) n FROM share_owners")["n"] == 0


def test_unreadable_owner_after_a_readable_one_is_refused(env):
    """Review of 5fd6e34, S1."""
    env.gw.check_share_owner("ohh", "Personal-Drive", OHH)
    with pytest.raises(WrongShareOwner):
        env.gw.check_share_owner("ohh", "Personal-Drive", None)
    r = env.gw.db.one("SELECT detail FROM audit WHERE event='share_owner_mismatch'")
    assert "unreadable" in r["detail"]


def test_a_mismatch_inside_a_rolled_back_tx_is_still_audited(env):
    """An upload commit calls the session inside db.tx; the refusal rolls that back."""
    env.gw.check_share_owner("ohh", "Personal-Drive", OHH)
    with pytest.raises(WrongShareOwner):
        with env.gw.db.tx():
            env.gw.check_share_owner("ohh", "Personal-Drive", ADMIN)
    n = lambda: env.gw.db.one("SELECT COUNT(*) n FROM audit WHERE event='share_owner_mismatch'")["n"]
    assert n() == 0
    env.gw.expire_sessions()                              # the janitor, every minute
    assert n() == 1
    env.gw.expire_sessions()
    assert n() == 1


def test_a_refused_share_keeps_its_index_rows(env):
    """WrongShareOwner is a StorageError: the rescan skips the folder, deletes nothing."""
    class Swapped:
        def __init__(self, inner):
            self.inner = inner

        def scandir(self, share, path):
            raise WrongShareOwner(f"{share}: the NAS serves another account's folder")

        def __getattr__(self, name):
            return getattr(self.inner, name)
    h, root, root_row, sess = setup(env)
    env.gw.rescan(root_row, sess)
    before = names(env, root["id"])
    assert before
    with pytest.raises(StorageError):
        env.gw.rescan(root_row, Swapped(sess))
    assert names(env, root["id"]) == before


class _Tree:
    pass


@pytest.fixture
def bare(monkeypatch):
    """An SMBSession without a NAS: get_smb_tree hands out _Tree objects and
    share_root_owner reads the owner from `owners`."""
    trees, owners, calls = {}, {"Personal-Drive": OHH}, []
    monkeypatch.setattr(smb, "get_smb_tree",
                        lambda unc, **k: (trees.setdefault(unc.rsplit("\\", 1)[-1], _Tree()), ""))

    def owner(tree):
        share = next(k for k, t in trees.items() if t is tree)
        calls.append(share)
        return owners[share]
    monkeypatch.setattr(smb, "share_root_owner", owner)
    seen = {}

    def guard(user, share, sid):
        if seen.setdefault((user, share), sid) != sid:
            raise WrongShareOwner(share)
    s = object.__new__(smb.SMBSession)
    s.server, s.user, s._kw = "nas", "ohh", {}
    s._guard, s._trees = guard, smb.weakref.WeakKeyDictionary()
    s._guard_lock = smb.threading.Lock()
    return type("Bare", (), dict(s=s, trees=trees, owners=owners, calls=calls))


def test_each_tree_connect_is_checked_once(bare):
    bare.s._unc("Personal-Drive", "a")
    bare.s._unc("Personal-Drive", "b")
    bare.s._unc("IPC$")                                    # share list: never checked
    assert bare.calls == ["Personal-Drive"]


def test_a_reconnect_is_checked_again_and_a_swap_refused(bare):
    bare.s._unc("Personal-Drive", "a")
    bare.trees.pop("Personal-Drive")                       # reconnect: a new tree connect
    bare.owners["Personal-Drive"] = ADMIN                  # ...served admin's home
    for _ in range(2):                                     # refused, and stays refused
        with pytest.raises(WrongShareOwner):
            bare.s._unc("Personal-Drive", "a")
    assert bare.calls == ["Personal-Drive", "Personal-Drive"]


def test_nas_trouble_while_checking_is_not_a_pass(bare, monkeypatch):
    from smbprotocol.exceptions import SMBOSError
    from smbprotocol.header import NtStatus
    calls = []

    def flaky(tree):
        calls.append(1)
        if len(calls) == 1:
            raise SMBOSError(NtStatus.STATUS_IO_TIMEOUT, "\\\\nas\\Personal-Drive")
        return ADMIN
    monkeypatch.setattr(smb, "share_root_owner", flaky)
    bare.s._guard("ohh", "Personal-Drive", OHH)          # noted earlier
    with pytest.raises(StorageError) as e:
        bare.s._unc("Personal-Drive", "a")
    assert not isinstance(e.value, WrongShareOwner)
    with pytest.raises(WrongShareOwner):                  # checked again, not let through
        bare.s._unc("Personal-Drive", "a")


def test_no_read_control_is_left_to_the_guard(bare, monkeypatch):
    from smbprotocol.exceptions import SMBOSError
    from smbprotocol.header import NtStatus

    def denied(tree):
        raise SMBOSError(NtStatus.STATUS_ACCESS_DENIED, "\\\\nas\\Personal-Drive")
    monkeypatch.setattr(smb, "share_root_owner", denied)
    got = []
    bare.s._guard = lambda user, share, sid: got.append(sid)
    bare.s._unc("Personal-Drive", "a")
    assert got == [None]


def test_admin_forget_lets_a_new_owner_be_noted(env, tmp_path, capsys):
    env.gw.check_share_owner("ohh", "Personal-Drive", OHH)
    cfg = tmp_path / "gw.toml"
    cfg.write_text(f'db_path = "{env.gw.s.db_path}"\n')
    admin.main(["--config", str(cfg), "share-owners"])
    assert OHH in capsys.readouterr().out
    admin.main(["--config", str(cfg), "share-owner-forget", "OHH", "Personal-Drive"])
    env.gw.check_share_owner("ohh", "Personal-Drive", ADMIN)   # noted, not refused
    with pytest.raises(SystemExit):
        admin.main(["--config", str(cfg), "share-owner-forget", "nobody", "x"])


def test_a_session_with_a_rescan_running_outlives_session_idle_s(env):
    """ohh's full pass takes ~75 min; with session_idle_s (30 min) as the only limit
    it never finished on an idle Mac (2026-11-06)."""
    h, root = rooted(env)
    dev = env.public.get("/v1/me", headers=h).json()["device_id"]
    sess, _ = env.gw._sessions[dev]
    idle = time.time() - env.gw.s.session_idle_s - 60
    env.gw._sessions[dev] = (sess, idle)
    env.gw._scanning[sess] = 1                             # a pass is running on it
    env.gw.expire_sessions()
    assert dev in env.gw._sessions                         # kept
    env.gw._sessions[dev] = (sess, time.time() - env.gw.s.rescan_keeps_session_s - 60)
    env.gw.expire_sessions()
    assert dev not in env.gw._sessions                     # but not past the cap
    assert sess in env.gw._closed_sessions                 # so the pass stops


def test_an_idle_session_without_a_rescan_still_goes(env):
    h, root = rooted(env)
    dev = env.public.get("/v1/me", headers=h).json()["device_id"]
    sess, _ = env.gw._sessions[dev]
    env.gw._sessions[dev] = (sess, time.time() - env.gw.s.session_idle_s - 60)
    env.gw.expire_sessions()
    assert dev not in env.gw._sessions


def test_rescan_marks_its_session_only_while_it_runs(env):
    h, root, root_row, sess = setup(env)
    seen = []

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        def scandir(self, share, path):
            seen.append(env.gw._scanning.get(self))
            return self.inner.scandir(share, path)

        def __getattr__(self, name):
            return getattr(self.inner, name)
    spy = Spy(sess)
    env.gw.rescan(root_row, spy)
    assert seen and all(n == 1 for n in seen)
    assert spy not in env.gw._scanning


def test_a_failed_audit_write_keeps_the_queue(env, monkeypatch):
    env.gw._owner_audit = [dict(nas_user="ohh", share="a", expected=OHH, got=ADMIN),
                           dict(nas_user="ohh", share="b", expected=OHH, got=ADMIN)]
    real, calls = env.gw.db.audit, []

    def flaky(event, **kw):
        calls.append(kw["share"])
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real(event, **kw)
    monkeypatch.setattr(env.gw.db, "audit", flaky)
    env.gw._flush_owner_audit()
    assert len(env.gw._owner_audit) == 2
    env.gw._flush_owner_audit()
    assert env.gw._owner_audit == []
    rows = env.gw.db.all("SELECT detail FROM audit WHERE event='share_owner_mismatch'")
    assert len(rows) == 2


def test_shutdown_writes_the_queued_mismatches(env):
    env.gw._owner_audit = [dict(nas_user="ohh", share="a", expected=OHH, got=ADMIN)]
    env.gw.release_waiters()
    n = lambda: env.gw.db.one("SELECT COUNT(*) n FROM audit WHERE event='share_owner_mismatch'")["n"]
    for _ in range(50):
        if n():
            break
        time.sleep(0.05)
    assert n() == 1
