"""Review of 5fd6e34, B1 (the review's probe, kept as a test): _guard_lock -> db.lock in the guard vs db.lock -> _guard_lock
when a request calls the session inside db.tx() (upload commit, restore_version)."""
import threading, time
from loft_gateway.storage import smb

OHH = "S-1-5-21-1-2-3-1000"


class _Tree:
    pass


def test_guard_vs_tx_lock_order(env, monkeypatch):
    trees = {}
    monkeypatch.setattr(smb, "get_smb_tree",
                        lambda unc, **k: (trees.setdefault(unc.split("\\")[3], _Tree()), ""))
    b_in_owner_read = threading.Event()
    a_in_tx = threading.Event()

    def owner(tree):                     # B: a fresh tree connect, NAS round trip
        b_in_owner_read.set()
        a_in_tx.wait(2)
        time.sleep(0.2)                   # A is now inside tx, about to touch the session
        return OHH
    monkeypatch.setattr(smb, "share_root_owner", owner)
    s = object.__new__(smb.SMBSession)
    s.server, s.user, s._kw = "nas", "ohh", {}
    s._guard, s._trees = env.gw.check_share_owner, smb.weakref.WeakKeyDictionary()
    s._guard_lock = smb.threading.Lock()

    def b():
        s._unc("Personal-Drive", "x")

    def a():
        b_in_owner_read.wait(2)
        with env.gw.db.tx():
            a_in_tx.set()
            s._unc("Personal-Drive", "y")   # e.g. fs.rename inside the commit tx
    tb, ta = threading.Thread(target=b, daemon=True), threading.Thread(target=a, daemon=True)
    tb.start(); ta.start()
    tb.join(5); ta.join(5)
    assert not tb.is_alive() and not ta.is_alive(), "DEADLOCK: both threads stuck"
