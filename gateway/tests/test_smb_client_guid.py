"""One SMB ClientGUID per NAS account (no NAS needed). With one GUID for the whole
gateway, Samba served every account from one smbd and resolved UniFi Drive's per-user
Personal-Drive for whoever signed in last: a gateway gave one account another's home (2026-11-05)."""
import smbclient._pool
from smbclient import ClientConfig

from loft_gateway.storage import smb


class FakeTransport:
    def __init__(self):
        self.connected = False


class FakeConnection:
    made: list = []

    def __init__(self, guid, server, port, require_signing=True):
        self.guid, self.server, self.port, self.require_signing = guid, server, port, require_signing
        self.transport = FakeTransport()
        self.session_table = {}
        FakeConnection.made.append(self)

    def connect(self, timeout=60):
        self.transport.connected = True

    def echo(self, timeout=60, credit_request=1):    # the credit request after connect
        return credit_request

    def disconnect(self):
        self.transport.connected = False


def _patch(monkeypatch):
    FakeConnection.made = []
    monkeypatch.setattr(smb, "_Connection", FakeConnection)


def test_guid_per_account():
    g = smb.client_guid
    assert g("nas", "ohh") == g("NAS", "OHH")                   # same account, same GUID
    assert g("nas", "ohh") != g("nas", "admin")
    assert g("nas", "ohh") != g("nas2", "ohh")
    assert g("nas", "ohh") != ClientConfig().client_guid      # never smbclient's shared one


def test_cache_connects_with_the_account_guid_and_reconnects_with_it(monkeypatch):
    _patch(monkeypatch)
    cache = smb._AccountConnections(smb.client_guid("nas", "admin"))
    c1 = cache.get("nas:445")
    assert c1.transport.connected and c1.guid == smb.client_guid("nas", "admin")
    assert (c1.server, c1.port) == ("nas", 445)
    assert cache.get("nas:445") is c1                          # reused while connected
    c1.transport.connected = False                             # dropped: a new one, same GUID
    c2 = cache.get("nas:445")
    assert c2 is not c1 and c2.guid == c1.guid and cache["nas:445"] is c2


def test_register_session_never_makes_its_own_connection(monkeypatch):
    """register_session() must take the cache's connection, not open one with the
    process-wide GUID: that is the whole fix."""
    _patch(monkeypatch)

    def no_shared_guid(*a, **k):
        raise AssertionError("smbclient opened a connection with the process-wide GUID")
    monkeypatch.setattr(smbclient._pool, "Connection", no_shared_guid)

    class FakeSession:
        def __init__(self, connection, username=None, **k):
            self.username, self.session_id = username, 1
            connection.session_table[1] = self

        def connect(self):
            pass
    monkeypatch.setattr(smbclient._pool, "Session", FakeSession)
    for user in ("ohh", "admin"):
        s = smb.SMBSession("nas", user, "pw", encrypt=False)
        (conn,) = s._cache.values()
        assert conn.guid == smb.client_guid("nas", user)
    assert FakeConnection.made[0].guid != FakeConnection.made[1].guid
