"""SMB error mapping, with the exception objects smbclient really raises (no NAS needed)."""
import pytest
from smbprotocol.exceptions import SMBOSError, SMBResponseException
from smbprotocol.header import NtStatus, SMB2HeaderResponse

from loft_gateway.storage.base import AccessDenied, Exists, NotFound, StorageError
from loft_gateway.storage.smb import _map


@pytest.mark.parametrize("status,want", [
    (NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, NotFound),
    (NtStatus.STATUS_OBJECT_PATH_NOT_FOUND, NotFound),
    (NtStatus.STATUS_OBJECT_NAME_COLLISION, Exists),
    (NtStatus.STATUS_ACCESS_DENIED, AccessDenied),
    (NtStatus.STATUS_SHARING_VIOLATION, StorageError),   # a lock, not a permission
    (NtStatus.STATUS_DIRECTORY_NOT_EMPTY, StorageError),
])
def test_smb_os_error(status, want):
    assert type(_map(SMBOSError(status, r"\\nas\loft\x"), "loft/x")) is want


def test_response_access_denied():
    h = SMB2HeaderResponse()
    h["status"] = NtStatus.STATUS_ACCESS_DENIED
    assert type(_map(SMBResponseException(h), "loft/x")) is AccessDenied


@pytest.mark.parametrize("required,want", [(True, True), (False, None)])
def test_encrypt_flag_per_mode(monkeypatch, required, want):
    """smb_encryption = "server" must hand smbclient encrypt=None (server decides), never
    False: False fails every call once the server chose to encrypt the session."""
    import smbclient
    from loft_gateway.storage.smb import SMBSession
    seen = []
    monkeypatch.setattr(smbclient, "register_session", lambda server, **kw: seen.append(kw))
    s = SMBSession("nas.example.org", "alice", "pw", encrypt=required)
    assert seen[0]["encrypt"] is want and s._kw["encrypt"] is want


def _session(monkeypatch):
    import smbclient
    from loft_gateway.storage.smb import SMBSession
    monkeypatch.setattr(smbclient, "register_session", lambda server, **kw: None)
    return SMBSession("nas.example.org", "alice", "pw")


def test_read_opens_shared_and_before_the_first_byte(monkeypatch):
    """A download opens the file with shared read/write/delete (two Range slices at once,
    or a file someone has open on the NAS) and before the response starts."""
    import io
    import smbclient
    opened = []

    def fake_open(path, mode="r", share_access=None, **kw):
        opened.append((path, mode, share_access))
        return io.BytesIO(b"0123456789")
    monkeypatch.setattr(smbclient, "open_file", fake_open)
    s = _session(monkeypatch)
    it = s.read("loft", "a/b.bin", 2, 5, block=2)
    assert opened == [(r"\\nas.example.org\loft\a\b.bin", "rb", "rwd")]   # before iterating
    assert b"".join(it) == b"23456"


def test_read_error_at_open_is_raised_before_streaming(monkeypatch):
    import smbclient

    def busy(*a, **kw):
        raise SMBOSError(NtStatus.STATUS_SHARING_VIOLATION, r"\\nas\loft\x")
    monkeypatch.setattr(smbclient, "open_file", busy)
    with pytest.raises(StorageError):
        _session(monkeypatch).read("loft", "x")


def test_read_waits_for_credits_and_retries_the_same_request(monkeypatch):
    """smbprotocol raises before sending when the connection is out of credits; the raw
    read is retried, so no byte is lost or repeated."""
    import io
    import smbclient
    from smbprotocol.exceptions import SMBException
    from loft_gateway.storage import smb as S
    monkeypatch.setattr(S.time, "sleep", lambda s: None)

    class Starved(io.BytesIO):
        fails = {"read": 2, "close": 1}

        def read(self, n=-1):
            if self.fails["read"]:
                self.fails["read"] -= 1
                raise SMBException("Request requires 64 credits but only 3 credits are available")
            return super().read(n)

        def close(self):
            if self.fails["close"]:
                self.fails["close"] -= 1
                raise SMBException("Request requires 1 credits but only 0 credits are available")
            super().close()
    f = Starved(b"0123456789")
    monkeypatch.setattr(smbclient, "open_file", lambda *a, **kw: f)
    assert b"".join(_session(monkeypatch).read("loft", "x", 0, 10, block=4)) == b"0123456789"
    assert f.closed


def test_other_smb_errors_are_not_retried(monkeypatch):
    from smbprotocol.exceptions import SMBException
    from loft_gateway.storage import smb as S
    calls = []

    def boom():
        calls.append(1)
        raise SMBException("Connection reset")
    with pytest.raises(SMBException):
        S._when_credits([], boom)
    assert calls == [1]


def test_new_connection_asks_for_credits(monkeypatch):
    import uuid
    from loft_gateway.storage import smb as S
    asked = []

    class Conn:
        def __init__(self, guid, server, port, require_signing):
            self.server_name = server
            self.transport = type("T", (), {"connected": True})()

        def connect(self, timeout):
            pass

        def echo(self, timeout, credit_request):
            asked.append(credit_request)
            return credit_request
    monkeypatch.setattr(S, "_Connection", Conn)
    cache = S._AccountConnections(uuid.uuid4())
    c = cache.get("nas.example.org:445")
    assert asked == [512] and cache.get("nas.example.org:445") is c and asked == [512]


# --- review of bc130a0: S1 (handles of downloads that never stream), S2 (credits) ---

class _Handle:
    """A file the way SMBRawIO looks to read(): records which thread closed it."""
    def __init__(self, data=b"0123456789"):
        import io
        import threading
        self._b, self.closed_by, self._lock = io.BytesIO(data), [], threading.Lock()
        self.closed_evt = threading.Event()

    def seek(self, n):
        self._b.seek(n)

    def read(self, n):
        return self._b.read(n)

    def close(self):
        import threading
        with self._lock:
            self.closed_by.append(threading.current_thread().name)
        self.closed_evt.set()


def _opened(monkeypatch):
    import smbclient
    h = _Handle()
    monkeypatch.setattr(smbclient, "open_file", lambda *a, **kw: h)
    return h, _session(monkeypatch)


def test_download_never_started_still_closes_its_handle(monkeypatch):
    """The client left before the body began: the generator is dropped unstarted, no
    finally runs. The handle is closed anyway, once, and not in the dropping thread."""
    import gc
    h, s = _opened(monkeypatch)
    it = s.read("loft", "x", 0, 10)
    del it
    gc.collect()
    assert h.closed_evt.wait(5)
    assert h.closed_by == ["loft-smb-close"]


def test_download_abandoned_midway_closes_in_its_own_thread(monkeypatch):
    h, s = _opened(monkeypatch)
    it = s.read("loft", "x", 0, 10, block=2)
    assert next(it) == b"01"
    it.close()                                  # what GC does to a dropped stream
    del it
    assert h.closed_evt.wait(5)
    assert h.closed_by == ["loft-smb-close"]


def test_download_run_to_the_end_closes_inline_once(monkeypatch):
    import gc
    import threading
    h, s = _opened(monkeypatch)
    it = s.read("loft", "x", 0, 10, block=4)
    assert b"".join(it) == b"0123456789"
    del it
    gc.collect()
    assert h.closed_by == [threading.current_thread().name]


def _compound_conn(window):
    import uuid
    from smbprotocol.open import SMB2CloseRequest, SMB2CreateRequest
    from smbprotocol.open import SMB2QueryInfoRequest
    from loft_gateway.storage import smb as S
    c = S._Connection(uuid.uuid4(), "nas.example.org", 445, require_signing=False)
    c.supports_multi_credit = True
    c.sequence_window = dict(low=100, high=100 + window)
    sent = []
    c.transport = type("T", (), {"send": staticmethod(sent.append), "connected": True})()
    c._check_worker_running = lambda: None
    return c, [SMB2CreateRequest(), SMB2QueryInfoRequest(), SMB2CloseRequest()], sent


def test_compound_short_on_credits_takes_no_ids():
    """Stock smbprotocol took ids for the first messages and then raised: ids lost for
    good. _Connection checks the whole compound first."""
    from smbprotocol.exceptions import SMBException
    from loft_gateway.storage import smb as S
    c, msgs, sent = _compound_conn(2)
    with pytest.raises(SMBException, match=S._NO_CREDITS):
        c._send(msgs, related=True)
    assert c.sequence_window == dict(low=100, high=102)
    assert not c.outstanding_requests and not sent


def test_compound_with_enough_credits_is_sent_once():
    from smbprotocol.connection import SMB2Echo
    c, _, sent = _compound_conn(3)
    reqs = c._send([SMB2Echo(), SMB2Echo(), SMB2Echo()])   # packs without a file name          # re-enters the sequence lock: no deadlock
    assert len(reqs) == 3 and len(sent) == 1
    assert c.sequence_window["low"] == 103 and len(c.outstanding_requests) == 3


class _WedgeConn:
    server_name = "nas.example.org"

    def __init__(self):
        self.sequence_window = dict(low=7, high=7)
        self.transport = type("T", (), {"connected": True})()
        self.dropped = []

    def disconnect(self, close=True):
        self.dropped.append(close)
        self.transport.connected = False


@pytest.mark.parametrize("credit_came_back", [False, True])
def test_wedged_connection_is_dropped_after_the_wait(monkeypatch, caplog, credit_came_back):
    """No credit back during the whole wait: the window is wedged; drop the connection so
    the next request reconnects. A credit that did come back means it's only busy."""
    from smbprotocol.exceptions import SMBException
    from loft_gateway.storage import smb as S
    monkeypatch.setattr(S, "_CREDIT_WAIT_S", 0.05)
    c = _WedgeConn()

    def starved():
        if credit_came_back:
            c.sequence_window["high"] += 1
            c.sequence_window["low"] += 1          # and used at once by someone else
        raise SMBException("Request requires 1 credits but only 0 credits are available")
    with pytest.raises(SMBException):
        S._when_credits([c, None], starved)
    if credit_came_back:
        assert c.dropped == [] and "wedged" not in caplog.text
    else:
        assert c.dropped == [False] and "credit window wedged" in caplog.text


def test_stat_waits_for_credits(monkeypatch):
    import os
    import smbclient
    from smbprotocol.exceptions import SMBException
    from loft_gateway.storage import smb as S
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    tries = []

    def stat(path, **kw):
        tries.append(path)
        if len(tries) < 3:
            raise SMBException("Request requires 7 credits but only 2 credits are available")
        return os.stat_result((0o100644, 5, 0, 1, 0, 0, 10, 0, 0, 0))
    monkeypatch.setattr(smbclient, "stat", stat)
    e = _session(monkeypatch).stat("loft", "a/b.txt")
    assert len(tries) == 3 and e.size == 10 and e.name == "b.txt"
