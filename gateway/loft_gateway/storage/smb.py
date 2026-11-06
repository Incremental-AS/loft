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
import logging
import stat as _s
import struct
import threading
import time
import uuid
import weakref
from typing import Callable, Iterator

import smbclient
import smbclient.shutil
from smbclient._io import SMBDirectoryIO
from smbclient._pool import get_smb_tree
from smbprotocol.change_notify import ChangeNotifyFlags, CompletionFilter, FileSystemWatcher
from smbprotocol.connection import Connection
from smbprotocol.exceptions import (LogonFailure, NoMoreFiles, NoSuchFile, SMBAuthenticationError,
                                    SMBException)
from smbprotocol.file_info import FileInformationClass, InfoType
from smbprotocol.header import Commands, NtStatus
from smbprotocol.open import (CreateDisposition, CreateOptions, DirectoryAccessMask, FileAttributes,
                              ImpersonationLevel, Open, QueryDirectoryFlags, ShareAccess,
                              SMB2QueryDirectoryResponse, SMB2QueryInfoRequest,
                              SMB2QueryInfoResponse)
from smbprotocol.security_descriptor import SMB2CreateSDBuffer

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


# FILE_ID_FULL_DIRECTORY_INFORMATION ([MS-FSCC] 2.4.18), read straight from the reply.
# smbprotocol turns the times into datetimes, which drops the last digit of the
# 100 ns FILETIME; stat() keeps it, and commit's check compares stat() with the
# index, so a listing must give exactly the value stat() would.
_DIRINFO = struct.Struct("<IIqqqqqqIIIIQ")     # .. file_id; the name follows at 80
_EPOCH_FT = 116444736000000000
_ATTR_DIR, _ATTR_REPARSE = 0x10, 0x400


def _dir_entries(buf: bytes):
    """(name, attributes, size, mtime_ns, file_id) for every entry in one reply.
    A reply that doesn't parse (a name that isn't UTF-16, an offset past the end)
    is a StorageError, so reconcile skips that folder instead of ending the pass."""
    try:
        yield from _parse_dir_entries(buf)
    except (struct.error, ValueError) as e:            # UnicodeDecodeError is a ValueError
        raise StorageError(f"unreadable listing: {e}") from e


def _parse_dir_entries(buf: bytes):
    off = 0
    while True:
        (nxt, _idx, _ct, _at, wt, _cht, eof, _alloc, attrs, nlen, _ea, _r,
         fid) = _DIRINFO.unpack_from(buf, off)
        name = buf[off + 80:off + 80 + nlen].decode("utf-16-le")
        if name not in (".", ".."):
            yield name, attrs, eof, (wt - _EPOCH_FT) * 100, fid
        if not nxt:
            return
        off += nxt


_NOTIFY_FILTER = (CompletionFilter.FILE_NOTIFY_CHANGE_FILE_NAME
                  | CompletionFilter.FILE_NOTIFY_CHANGE_DIR_NAME
                  | CompletionFilter.FILE_NOTIFY_CHANGE_SIZE
                  | CompletionFilter.FILE_NOTIFY_CHANGE_LAST_WRITE)
_NOTIFY_BUF = 64 * 1024           # one credit; ~600 changes per reply


class SMBWatch:
    """CHANGE_NOTIFY with WATCH_TREE on one folder, over the session's own
    encrypted connection (one more open handle, no extra login). Measured on a
    Samba NAS: creates three folders deep reported within ~35 ms, paths
    relative to the watched folder. Samba queues what changes between two
    replies on the handle, so re-arming loses nothing; more than fits in one
    reply comes back as STATUS_NOTIFY_ENUM_DIR (smbprotocol: an empty list)."""

    def __init__(self, unc: str, kw: dict):
        try:
            tree, rel = get_smb_tree(unc, **kw)
            self._open = Open(tree, rel or "")
            self._open.create(ImpersonationLevel.Impersonation,
                              DirectoryAccessMask.FILE_LIST_DIRECTORY,
                              FileAttributes.FILE_ATTRIBUTE_DIRECTORY,
                              ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE
                              | ShareAccess.FILE_SHARE_DELETE,
                              CreateDisposition.FILE_OPEN, CreateOptions.FILE_DIRECTORY_FILE)
        except (OSError, SMBException) as e:
            raise _map(e, unc) from e
        self._unc = unc
        self._lock = threading.Lock()
        self._w: FileSystemWatcher | None = None
        self._closed = False

    def next(self) -> list[str] | None:
        with self._lock:
            if self._closed:
                raise StorageError(f"{self._unc}: watch closed")
            self._w = w = FileSystemWatcher(self._open)
        try:
            w.start(_NOTIFY_FILTER, flags=ChangeNotifyFlags.SMB2_WATCH_TREE,
                    output_buffer_length=_NOTIFY_BUF)
            res = w.wait()
        except (OSError, SMBException) as e:
            raise StorageError(f"{self._unc}: watch lost: {e}") from e
        if self._closed or res is None:          # closed, or cancelled under us
            raise StorageError(f"{self._unc}: watch closed")
        if not res:
            return None                          # overflow: too many to list
        return ["/".join(from_nas(x) for x in r["file_name"].get_value().split("\\") if x)
                for r in res]

    def close(self):
        with self._lock:
            self._closed = True
            w = self._w
        try:
            if w is not None and w.result is None and not w.cancelled:
                w.cancel()
        except Exception:  # noqa: BLE001  (already answered, or the connection is gone)
            pass
        try:
            self._open.close()
        except Exception:  # noqa: BLE001
            pass


# One SMB ClientGUID per NAS account, never one for the whole gateway. Samba hands a
# TCP connection whose ClientGUID it already knows to the smbd process that has it, so
# with smbclient's process-wide GUID every account lands in one smbd. Config that NAS
# resolves per user (%U, %H, an `include` named after the user) is then the config of
# whoever signed in LAST in that process, not of the session asking. UniFi Drive's
# Personal-Drive is such a share (path set by `include = shadow-share-%U.conf`): after a
# restart, with every account signing in within a second, a gateway served one account's home to
# another and back, writable (2026-11-05, seen on a UniFi NAS). Same account, same
# GUID (its sessions may share a process); two accounts never share one. The namespace
# is new per gateway process, like smbclient's own GUID.
_GUID_NS = uuid.uuid4()
_CONNECT_TIMEOUT_S = 60                         # smbclient's register_session default

log = logging.getLogger("loft.smb")

# SMB credits: how many requests (one per 64 KiB) a connection may have in flight. A new
# connection has 1, 64 after sign-in; one 4 MiB read costs 64, and every change-notify
# watch holds one. smbprotocol doesn't wait for credits: it raises "Request requires N
# credits but only M credits are available" before sending anything. So a device's
# parallel Range slices on a cold connection (after a restart, idle or a reconnect)
# failed mid-stream (2026-11-06). One ECHO asking for 512 right after connect gets them
# (the UNAS grants all 512, measured); reads also wait briefly for credits to come back.
_CREDITS_WANTED = 512
_CREDIT_WAIT_S = 10.0
_NO_CREDITS = "credits are available"


def _ask_credits(conn: Connection):
    try:
        conn.echo(timeout=_CONNECT_TIMEOUT_S, credit_request=_CREDITS_WANTED)
    except (SMBException, OSError) as e:          # still usable, just with fewer credits
        log.warning("SMB credit request to %s failed: %s", conn.server_name, e)


def _when_credits(conns, fn, *a, **k):
    """fn(...), again while the connection is out of credits. Only for requests that
    raise before ANY message is sent: one SMB request (open, a raw read, close), or a
    compound on a _Connection (stat). A retry is then exact.

    conns: the connection(s) fn may use. If no credit came back on one of them during
    the whole wait, it is wedged (ids lost without a response, review of
    bc130a0, S2): it is disconnected so the next request reconnects with fresh credits,
    instead of every request of that device failing until a restart. "Nothing
    outstanding" can't be the test: a device's change-notify watch is always pending."""
    conns = [c for c in conns if c is not None]
    high0 = [c.sequence_window["high"] for c in conns]
    deadline, delay = time.monotonic() + _CREDIT_WAIT_S, 0.01
    while True:
        try:
            return fn(*a, **k)
        except SMBException as e:
            if _NO_CREDITS not in str(e):
                raise
            if time.monotonic() > deadline:
                for c, h in zip(conns, high0):
                    if c.sequence_window["high"] == h and c.transport.connected:
                        log.warning("credit window wedged on %s (%d credits, none came back "
                                    "in %.0f s), reconnecting", c.server_name,
                                    c.sequence_window["high"] - c.sequence_window["low"],
                                    _CREDIT_WAIT_S)
                        try:
                            c.disconnect(close=False)
                        except Exception:  # noqa: BLE001
                            pass
                raise
        time.sleep(delay)
        delay = min(delay * 2, 0.2)


class _Connection(Connection):
    """smbprotocol's _send takes a message id per message of a compound and checks
    credits per message: a stat (CREATE + QUERY_INFO + CLOSE, 7 credits on Samba) met by
    a window of 1-6 took the first ids, raised, and sent nothing. Those ids never come
    back: with no credits left the connection is dead at once, otherwise Samba stalls
    ~8,191 requests later (review of bc130a0, S2, probe compound_fix.py). Here a
    compound's whole charge is checked first, under the (now re-entrant) sequence lock,
    so it raises before taking any id, with the same "credits are available" text."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sequence_lock = threading.RLock()        # super()._send takes it again

    def _send(self, messages, *a, **k):
        if len(messages) < 2:
            return super()._send(messages, *a, **k)
        with self.sequence_lock:
            # as _send counts: a charge of 0 still takes one id; a CANCEL takes none
            need = sum(max(self._calculate_credit_charge(m), 1) for m in messages
                       if m.COMMAND != Commands.SMB2_CANCEL)
            have = self.sequence_window["high"] - self.sequence_window["low"]
            if need > have:
                raise SMBException(f"Request requires {need} credits but only {have} "
                                   f"{_NO_CREDITS}")
            return super()._send(messages, *a, **k)


class _Closer:
    """Closes a download's file once. In a request's worker thread, when the stream ran
    to the end: inline, waiting for credits. Anywhere else (the stream was abandoned or
    never started, so GC got it): in a thread of its own. GC may run on the event loop
    (must not sleep up to 10 s) or on smbprotocol's receive thread (a close there waits
    for its own reply: deadlock, why smbclient's __del__ only warns). Without this, a
    download whose client left before the body started kept its handle open on the NAS
    until the session closed (review of bc130a0, S1: 20 of 20 leaked)."""

    def __init__(self, f, conns):
        self.f, self.conns = f, conns
        self._lock = threading.Lock()
        self._done = False

    def _claim(self) -> bool:
        with self._lock:
            done, self._done = self._done, True
            return not done

    def now(self):
        if self._claim():
            _when_credits(self.conns, self.f.close)

    def later(self):
        if self._claim():
            threading.Thread(target=self._quiet, name="loft-smb-close", daemon=True).start()

    def _quiet(self):
        try:
            _when_credits(self.conns, self.f.close)
        except Exception as e:  # noqa: BLE001
            log.info("closing an abandoned download failed: %s", e)


def client_guid(server: str, user: str) -> uuid.UUID:
    return uuid.uuid5(_GUID_NS, f"{server.lower()}\0{user.casefold()}")


class _AccountConnections(dict):
    """smbclient's connection cache for one SMBSession. register_session() looks the
    connection up with .get() and, when it's missing or has dropped, opens its own with
    the process-wide ClientGUID. Handing it a connected one with this account's GUID
    from .get() covers the first connect and every reconnect.

    The lock is held through connect(), up to _CONNECT_TIMEOUT_S while the NAS is
    down: on purpose, so one account's threads wait for one connect instead of each
    opening its own; every one of them would time out the same way (review of
    6786c9f, note 1). smbclient.delete_session() would connect through .get() only to
    disconnect again; the gateway never calls it (close() resets the cache), note 2."""

    def __init__(self, guid: uuid.UUID):
        super().__init__()
        self.guid = guid
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            conn = super().get(key)
            if conn is None or not conn.transport.connected:
                server, _, port = key.rpartition(":")
                conn = _Connection(self.guid, server, int(port), require_signing=True)
                conn.connect(timeout=_CONNECT_TIMEOUT_S)
                _ask_credits(conn)
                self[key] = conn
            return conn


_OWNER_SECURITY_INFORMATION = 0x1
# (user, share, owner SID or None if unreadable) -> None, or raises to refuse the share
ShareGuard = Callable[[str, str, "str | None"], None]


def share_root_owner(tree) -> str:
    """Owner SID ('S-1-...') of the share's root folder, as this tree connect sees it."""
    o = Open(tree, "")
    o.create(ImpersonationLevel.Impersonation, DirectoryAccessMask.READ_CONTROL,
             FileAttributes.FILE_ATTRIBUTE_DIRECTORY,
             ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE | ShareAccess.FILE_SHARE_DELETE,
             CreateDisposition.FILE_OPEN, CreateOptions.FILE_DIRECTORY_FILE)
    try:
        req = SMB2QueryInfoRequest()
        req["info_type"] = InfoType.SMB2_0_INFO_SECURITY
        req["additional_information"] = _OWNER_SECURITY_INFORMATION
        req["output_buffer_length"] = 4096
        req["file_id"] = o.file_id
        res = o.connection.receive(o.connection.send(req, tree.session.session_id,
                                                     tree.tree_connect_id))
        reply = SMB2QueryInfoResponse()
        reply.unpack(res["data"].get_value())
        sd = SMB2CreateSDBuffer()
        sd.unpack(reply["buffer"].get_value())
        return str(sd.get_owner())
    finally:
        o.close()


class SMBSession:
    _guard: ShareGuard | None = None

    def __init__(self, server: str, user: str, password: str, encrypt: bool = True,
                 share_guard: ShareGuard | None = None):
        self.server, self.user = server, user
        self._cache: dict = _AccountConnections(client_guid(server, user))
        # Every new tree connect to a share (first use, and again after a reconnect)
        # reads the owner of the share's root and asks share_guard (Gateway: the owner
        # this account's share had before). A NAS that hands this account another
        # account's folder (the 2026-11-05 Personal-Drive swap) is refused before any
        # call goes through. Per tree object: None = checked, an exception = refused.
        # _guard_lock covers only that cache, never the owner read or the guard: the
        # guard takes the DB lock, and a request holding it (upload commit,
        # restore_version) may be waiting on this session (review of 5fd6e34, B1).
        self._guard = share_guard
        self._trees: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._guard_lock = threading.Lock()
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
        self._check_share(base, share)
        if "~" in p:
            refuse_short_names(path.replace("\\", "/"), lambda parent, part: smbclient.listdir(
                base + "".join("\\" + x for x in parent), search_pattern=part, **self._kw))
        return base + (f"\\{p}" if p else "")

    def _check_share(self, base: str, share: str):
        if self._guard is None or share.upper() == "IPC$":
            return
        try:
            tree, _ = get_smb_tree(base, **self._kw)
        except (OSError, SMBException) as e:
            raise _map(e, share) from e
        with self._guard_lock:
            known = tree in self._trees
            refused = self._trees.get(tree)
        if not known:
            # No lock held from here: the guard takes the DB lock, and a request may
            # already hold that lock and be waiting to use this session (review of 5fd6e34).
            # Two threads checking the same new tree at once is harmless.
            try:
                sid = share_root_owner(tree)
            except (OSError, SMBException) as e:
                m = _map(e, share)
                if not isinstance(m, AccessDenied):
                    raise m from e
                sid = None
            try:
                self._guard(self.user, share, sid)
                refused = None
            except StorageError as e:
                refused = e
            with self._guard_lock:
                self._trees[tree] = refused
        if refused is not None:
            raise type(refused)(*refused.args)

    def _call(self, fn, share, path, *a, **k):
        try:
            return fn(self._unc(share, path), *a, **self._kw, **k)
        except (OSError, SMBException) as e:
            raise _map(e, f"{share}/{path}") from e

    def can_access(self, share):
        try:
            next(iter(self._call(smbclient.scandir, share, "")), None)
            return True
        except (StorageError, OSError, SMBException):   # the listing is lazy: errors come from next()
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

    def _conns(self):
        return list(dict.values(self._cache))          # dict's own: .get() would connect

    def stat(self, share, path):
        # a compound, retried only on a _Connection (it raises before sending anything)
        st = self._call(lambda *a, **k: _when_credits(self._conns(), smbclient.stat, *a, **k),
                        share, path)
        return self._entry(path.rsplit("/", 1)[-1], st, _s.S_ISDIR(st.st_mode), st.st_ino)

    def scandir(self, share, path):
        """One folder, from the listing alone: no stat per entry (that was one more
        round trip for every file, and a 300,000-item root took over half an hour).
        Every error, also one halfway through the listing, comes out mapped."""
        unc = self._unc(share, path)
        out = []
        try:
            with SMBDirectoryIO(unc, share_access="rwd", **self._kw) as d:
                fd, flags = d.fd, QueryDirectoryFlags.SMB2_RESTART_SCANS
                while True:
                    msg, _ = fd.query_directory("*", FileInformationClass.FILE_ID_FULL_DIRECTORY_INFORMATION,
                                                flags=flags, send=False)
                    flags = 0
                    req = fd.connection.send(msg, fd.tree_connect.session.session_id,
                                             fd.tree_connect.tree_connect_id)
                    try:
                        resp = fd.connection.receive(req)
                    except (NoMoreFiles, NoSuchFile):
                        break
                    r = SMB2QueryDirectoryResponse()
                    r.unpack(resp["data"].get_value())
                    for name, attrs, size, mtime_ns, fid in _dir_entries(r["buffer"].get_value()):
                        if attrs & _ATTR_REPARSE:      # a link: what it points at, as before
                            e = self._link_entry(unc, name, fid)
                            if e:
                                out.append(e)
                            continue
                        is_dir = bool(attrs & _ATTR_DIR)
                        out.append(Entry(from_nas(name), is_dir, 0 if is_dir else size, mtime_ns, fid))
        except (OSError, SMBException) as e:
            raise _map(e, f"{share}/{path}") from e
        except StorageError as e:                    # from _dir_entries: add where
            raise StorageError(f"{share}/{path}: {e}") from e
        return out

    def _link_entry(self, unc, name, fid):
        try:
            st = smbclient.stat(f"{unc}\\{name}", **self._kw)
        except (OSError, SMBException):
            return None                         # dangling, or gone since the listing
        return self._entry(from_nas(name), st, _s.S_ISDIR(st.st_mode), fid)

    def read(self, share, path, offset=0, length=None, block=4 << 20) -> Iterator[bytes]:
        # Opened here, not inside the generator: a NAS error then comes back as a proper
        # error response before any byte is sent, instead of a stream cut off after a 206.
        # Shared read/write/delete, like an SMB mount or Finder: open with no share access
        # (smbclient's default) refused a second reader of the same file (two Range slices
        # at once, two devices) and anyone who had it open on the NAS, with
        # STATUS_SHARING_VIOLATION, and blocked their saves while we read.
        unc = self._unc(share, path)
        try:
            # buffering=0: the raw file, so one read() is one SMB READ and can be retried
            f = _when_credits(self._conns(), smbclient.open_file, unc, "rb", buffering=0,
                              share_access="rwd", **self._kw)
        except (OSError, SMBException) as e:
            raise _map(e, f"{share}/{path}") from e
        fd = getattr(f, "fd", None)
        closer = _Closer(f, [getattr(fd, "connection", None)])
        try:
            f.seek(offset)
        except (OSError, SMBException) as e:
            closer.later()
            raise _map(e, f"{share}/{path}") from e
        gen = self._stream(f, closer, f"{share}/{path}", length, block)
        weakref.finalize(gen, closer.later)       # never started: no finally will run
        return gen

    @staticmethod
    def _stream(f, closer, where, length, block) -> Iterator[bytes]:
        try:
            done = False
            try:
                left = length
                while left is None or left > 0:
                    # a raw read may return less than asked (read size, credits): loop on
                    chunk = _when_credits(closer.conns, f.read,
                                          block if left is None else min(block, left))
                    if not chunk:
                        break
                    if left is not None:
                        left -= len(chunk)
                    yield chunk
                done = True
            finally:
                # ran to the end: here, in the worker thread. Abandoned or failed: GC or
                # whichever thread dropped it, so not here.
                closer.now() if done else closer.later()
        except (OSError, SMBException) as e:
            raise _map(e, where) from e

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

    def watch(self, share, path) -> SMBWatch:
        return SMBWatch(self._unc(share, path), self._kw)

    def close(self):
        try:
            smbclient.reset_connection_cache(fail_on_error=False, connection_cache=self._cache)
        except Exception:  # noqa: BLE001
            pass
        self._kw["password"] = None


class SMBBackend:
    def __init__(self, server: str, encrypt: bool = True):
        self.server, self.encrypt = server, encrypt
        self.share_guard: ShareGuard | None = None      # set by the Gateway

    def authenticate(self, user: str, password: str) -> SMBSession:
        return SMBSession(self.server, user, password, self.encrypt, self.share_guard)
