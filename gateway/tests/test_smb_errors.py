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
