"""SMB3 backend (smbprotocol, user space). One connection cache per NAS user
session, so sessions never mix and the NAS applies that user's permissions.

The password lives in this object's memory for as long as the session is
cached (Settings.session_idle_s). It is never written anywhere.

Checked against a Samba-based NAS and the Samba conformance run
(conformance/samba/): SMB 3.1.1 encrypted, server file id stable across rename,
case-insensitive names.
"""
from __future__ import annotations

import errno
import stat as _s
from typing import Iterator

import smbclient
import smbclient.shutil
from smbclient._pool import get_smb_tree
from smbprotocol.exceptions import LogonFailure, SMBAuthenticationError, SMBException
from smbprotocol.header import NtStatus

from .base import (AccessDenied, AuthFailed, Entry, Exists, NotFound, StorageError, from_nas,
                   refuse_short_names, to_nas)
from .srvsvc import RpcError, list_disk_shares


def _map(e: Exception, path: str) -> Exception:
    # smbclient raises SMBOSError: a plain OSError carrying an errno, never the
    # FileNotFoundError/FileExistsError subclasses, and STATUS_ACCESS_DENIED has
    # no errno at all (only .ntstatus). Measured against Samba.
    err = getattr(e, "errno", None)
    status = getattr(e, "ntstatus", None) or getattr(e, "status", None)
    if isinstance(e, FileNotFoundError) or err == errno.ENOENT:
        return NotFound(path)
    if isinstance(e, FileExistsError) or err == errno.EEXIST:
        return Exists(path)
    if isinstance(e, PermissionError) or status == NtStatus.STATUS_ACCESS_DENIED:
        return AccessDenied(path)
    return StorageError(f"{path}: {e}")


class SMBSession:
    def __init__(self, server: str, user: str, password: str, encrypt: bool = True):
        self.server, self.user = server, user
        self._cache: dict = {}
        # encrypt=True is smbprotocol's Session(require_encryption=True): every
        # message is SMB3-encrypted whatever the server prefers, and a server
        # that cannot (SMB < 3, guest mapping) fails the connect. Many servers don't
        # force encryption themselves, so this is where it is enforced.
        # smb_encryption = "server" passes encrypt=None, NOT False: the server or the share
        # decides (session/share ENCRYPT_DATA flags). smbclient checks `encrypt` again on
        # every call, and False on a session the server chose to encrypt (Samba's
        # "desired", the default on many NAS boxes) raises "Cannot disable encryption on an
        # already negotiated session" on every request after sign-in (Samba CI, 2026-10-30).
        self._kw = dict(username=user, password=password, encrypt=True if encrypt else None,
                        connection_cache=self._cache)
        try:
            smbclient.register_session(server, **self._kw)
        except (LogonFailure, SMBAuthenticationError) as e:
            self.close()
            raise AuthFailed(user) from e
        except (SMBException, OSError, ValueError) as e:
            self.close()
            raise StorageError(f"SMB connect to {server} failed: {e}") from e

    def _unc(self, share: str, path: str = "") -> str:
        path = to_nas(path)
        p = path.strip("/").replace("/", "\\")
        # Last guard: smbclient resolves '..' itself (stopping at the share root), so
        # a climbing path would open something the gateway's block checks never saw.
        if any(x in (".", "..", "") for x in share.split("\\")) or any(
                x in (".", "..") for x in p.split("\\")):
            raise NotFound(path)
        base = f"\\\\{self.server}\\{share}"
        if "~" in p:
            refuse_short_names(path.replace("\\", "/"), lambda parent, part: smbclient.listdir(
                base + "".join("\\" + x for x in parent), search_pattern=part, **self._kw))
        return base + (f"\\{p}" if p else "")

    def _call(self, fn, share, path, *a, **k):
        try:
            return fn(self._unc(share, path), *a, **self._kw, **k)
        except (OSError, SMBException) as e:
            raise _map(e, f"{share}/{path}") from e

    def can_access(self, share):
        try:
            next(iter(self._call(smbclient.scandir, share, "")), None)
            return True
        except StorageError:
            return False

    def list_shares(self):
        """Disk shares this user can see (SRVSVC over the encrypted IPC$ tree)."""
        try:
            tree, _ = get_smb_tree(self._unc("IPC$"), **self._kw)
            return list_disk_shares(tree, self.server)
        except (OSError, SMBException, RpcError) as e:
            raise _map(e, "IPC$/srvsvc") from e

    @staticmethod
    def _entry(name, st, is_dir, ino) -> Entry:
        return Entry(name, is_dir, 0 if is_dir else st.st_size, st.st_mtime_ns, ino)

    def stat(self, share, path):
        st = self._call(smbclient.stat, share, path)
        return self._entry(path.rsplit("/", 1)[-1], st, _s.S_ISDIR(st.st_mode), st.st_ino)

    def scandir(self, share, path):
        out = []
        for de in self._call(smbclient.scandir, share, path):
            st = de.stat()
            out.append(self._entry(from_nas(de.name), st, de.is_dir(), de.inode()))
        return out

    def read(self, share, path, offset=0, length=None, block=4 << 20) -> Iterator[bytes]:
        try:
            with smbclient.open_file(self._unc(share, path), "rb", **self._kw) as f:
                f.seek(offset)
                left = length
                while left is None or left > 0:
                    chunk = f.read(block if left is None else min(block, left))
                    if not chunk:
                        return
                    if left is not None:
                        left -= len(chunk)
                    yield chunk
        except (OSError, SMBException) as e:
            raise _map(e, f"{share}/{path}") from e

    def write_at(self, share, path, offset, data):
        try:
            with smbclient.open_file(self._unc(share, path), "r+b" if offset else "wb",
                                     **self._kw) as f:
                f.seek(offset)
                f.write(data)
        except (OSError, SMBException) as e:
            raise _map(e, f"{share}/{path}") from e

    def create_empty(self, share, path):
        self.write_at(share, path, 0, b"")

    def makedirs(self, share, path):
        self._call(smbclient.makedirs, share, path, exist_ok=True)

    def rename(self, share, src, dst, replace=False):
        fn = smbclient.replace if replace else smbclient.rename
        try:
            fn(self._unc(share, src), self._unc(share, dst), **self._kw)
        except (OSError, SMBException) as e:
            raise _map(e, f"{share}/{src} -> {dst}") from e

    def remove(self, share, path):
        self._call(smbclient.remove, share, path)

    def rmtree(self, share, path):
        self._call(smbclient.shutil.rmtree, share, path)

    def set_mtime(self, share, path, mtime_ns):
        self._call(smbclient.utime, share, path, ns=(mtime_ns, mtime_ns))

    def close(self):
        try:
            smbclient.reset_connection_cache(fail_on_error=False, connection_cache=self._cache)
        except Exception:  # noqa: BLE001
            pass
        self._kw["password"] = None


class SMBBackend:
    def __init__(self, server: str, encrypt: bool = True):
        self.server, self.encrypt = server, encrypt

    def authenticate(self, user: str, password: str) -> SMBSession:
        return SMBSession(self.server, user, password, self.encrypt)
