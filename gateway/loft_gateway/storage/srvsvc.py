"""List the disk shares a NAS user can see: SRVSVC NetrShareEnum (opnum 15,
level 1) over the IPC$ named pipe, as `smbclient -L` does.

smbprotocol has no DCE/RPC layer, so this is the minimum by hand: one BIND,
one REQUEST, NDR in and out. With Samba's access-based share enumeration the
answer is already only the shares this user may open.
"""
from __future__ import annotations

import struct
import uuid

from smbprotocol.open import (CreateDisposition, CreateOptions, FilePipePrinterAccessMask,
                              ImpersonationLevel, Open, ShareAccess)

SRVSVC = (uuid.UUID("4b324fc8-1670-01d3-1278-5a47bf6ee188"), 3, 0)
NDR = (uuid.UUID("8a885d04-1ceb-11c9-9fe8-08002b104860"), 2, 0)
MAX_FRAG = 4280
STYPE_DISKTREE, STYPE_SPECIAL = 0, 0x80000000


class RpcError(Exception):
    pass


def _hdr(ptype: int, body: bytes, call_id: int) -> bytes:
    # v5.0, PFC_FIRST_FRAG|PFC_LAST_FRAG, little-endian/ASCII/IEEE, no auth
    return struct.pack("<BBBBIHHI", 5, 0, ptype, 3, 0x10, 16 + len(body), 0, call_id) + body


def bind_pdu() -> bytes:
    def syntax(s):
        return s[0].bytes_le + struct.pack("<HH", s[1], s[2])
    body = struct.pack("<HHIB3x", MAX_FRAG, MAX_FRAG, 0, 1)
    body += struct.pack("<HBx", 0, 1) + syntax(SRVSVC) + syntax(NDR)
    return _hdr(11, body, 1)


def _wstr(s: str) -> bytes:
    """Conformant varying wide string, NUL-terminated, padded to 4."""
    raw = (s + "\0").encode("utf-16-le")
    n = len(raw) // 2
    out = struct.pack("<III", n, 0, n) + raw
    return out + b"\0" * (-len(out) % 4)


def enum_request_pdu(server: str) -> bytes:
    stub = struct.pack("<I", 0x20000) + _wstr("\\\\" + server)   # ServerName (unique ptr)
    stub += struct.pack("<III", 1, 1, 0x20004)                  # level 1, union tag 1, container ptr
    stub += struct.pack("<II", 0, 0)                            # EntriesRead 0, Buffer NULL
    stub += struct.pack("<I", 0xFFFFFFFF)                       # PreferedMaximumLength
    stub += struct.pack("<II", 0x20008, 0)                      # ResumeHandle -> 0
    body = struct.pack("<IHH", len(stub), 0, 15) + stub         # alloc hint, ctx 0, opnum 15
    return _hdr(0, body, 2)


class _Reader:
    def __init__(self, b: bytes):
        self.b, self.o = b, 0

    def u32(self) -> int:
        self.align(4)
        v = struct.unpack_from("<I", self.b, self.o)[0]
        self.o += 4
        return v

    def align(self, n: int):
        self.o += -self.o % n

    def wstr(self) -> str:
        _max, _off, n = self.u32(), self.u32(), self.u32()
        s = self.b[self.o:self.o + 2 * n].decode("utf-16-le")
        self.o += 2 * n
        return s.rstrip("\0")


def parse_enum_stub(stub: bytes) -> list[tuple[str, int]]:
    """NetrShareEnum response stub -> [(name, type)]."""
    r = _Reader(stub)
    _level, _tag, ptr = r.u32(), r.u32(), r.u32()
    if not ptr:
        return []
    count, arr = r.u32(), r.u32()
    if not arr:
        return []
    r.u32()                                                     # conformance (max count)
    fixed = [(r.u32(), r.u32(), r.u32()) for _ in range(count)]  # (name ptr, type, remark ptr)
    out = []
    for name_p, typ, remark_p in fixed:
        name = r.wstr() if name_p else ""
        if remark_p:
            r.wstr()
        out.append((name, typ))
    # trailer: TotalEntries, ResumeHandle ptr (+value), WERROR
    r.u32()
    if r.u32():
        r.u32()
    status = r.u32()
    if status:
        raise RpcError(f"NetrShareEnum WERROR 0x{status:08x}")
    return out


def _pdu_parts(pdu: bytes) -> tuple[int, int, bytes]:
    ver, _minor, ptype, flags, _rep, frag, auth, _cid = struct.unpack_from("<BBBBIHHI", pdu)
    if ver != 5 or len(pdu) < frag:
        raise RpcError("short or bad DCE/RPC PDU")
    return ptype, flags, pdu[16:frag - auth - (8 if auth else 0)]


def list_disk_shares(tree, server: str) -> list[str]:
    """`tree` = a TreeConnect to IPC$ on an authenticated (encrypted) session."""
    pipe = Open(tree, "srvsvc")
    pipe.create(ImpersonationLevel.Impersonation,
                FilePipePrinterAccessMask.FILE_READ_DATA | FilePipePrinterAccessMask.FILE_WRITE_DATA,
                0, ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE,
                CreateDisposition.FILE_OPEN, CreateOptions.FILE_NON_DIRECTORY_FILE)
    try:
        pipe.write(bind_pdu())
        ptype, _, _ = _pdu_parts(pipe.read(0, MAX_FRAG))
        if ptype != 12:                                         # bind_ack
            raise RpcError(f"srvsvc bind refused (ptype {ptype})")
        pipe.write(enum_request_pdu(server))
        stub = b""
        while True:
            ptype, flags, body = _pdu_parts(pipe.read(0, MAX_FRAG))
            if ptype == 3:                                      # fault
                raise RpcError(f"srvsvc fault 0x{struct.unpack_from('<I', body, 8)[0]:08x}")
            if ptype != 2:
                raise RpcError(f"unexpected PDU type {ptype}")
            stub += body[8:]                                    # skip alloc hint, ctx, cancel count
            if flags & 2:                                       # PFC_LAST_FRAG
                break
    finally:
        pipe.close()
    return sorted(n for n, t in parse_enum_stub(stub)
                  if (t & 0xFF) == STYPE_DISKTREE and not t & STYPE_SPECIAL)
