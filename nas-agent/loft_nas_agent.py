#!/usr/bin/env python3
"""Loft NAS agent: tells the Loft gateway which folders changed on a btrfs NAS.

SMB change notify only reports what goes through Samba. Files that reach the
disk any other way (the NAS's own web or phone app, jobs running on the NAS,
NFS, rsync over ssh, a trash restore) stay invisible to the gateway until its
next full rescan, which takes over an hour on a large NAS. This agent closes
that gap from the NAS side, using the filesystem itself as the record of what
changed:

* Each share lives on a btrfs subvolume. Every inode btrfs writes carries the
  id of the transaction that wrote it (inode_item.transid), and a tree search
  with min_transid returns only the leaves written since then. So "every inode
  written since transaction N" is complete, survives restarts on both sides,
  and costs milliseconds even on a share with millions of files.
* A changed folder is reported as itself (entries added, removed, renamed);
  a changed file as its parent folder (written in place). Paths only, relative
  to the share root: never names of shares or folders the config excludes,
  never file contents, never metadata beyond the path.
* The gateway keeps the cursor (the transaction id each share was reported
  up to) and hands it out; the agent keeps nothing on disk. A share whose
  subvolume was recreated (another UUID) gets a fresh cursor, and the gateway
  rescans it once.
* No listener: the agent dials out to the gateway's direct listener over TLS,
  pinned to the gateway's key (the same SPKI pins the apps use), and signs
  every request with an HMAC key the two share.

Standard library only (Python 3.11+). Needs CAP_SYS_ADMIN for the btrfs tree
search and inode lookup ioctls, and read access to the share roots.

Config: TOML (default /data/loft-agent/agent.toml); see agent.toml.example.
"""
from __future__ import annotations

import base64
import fcntl
import fnmatch
import glob
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import signal
import socket
import ssl
import stat
import struct
import sys
import time
import tomllib
import uuid as uuidlib
from dataclasses import dataclass, field

VERSION = "2"
log = logging.getLogger("loft-nas-agent")

# ── btrfs ioctls (linux/btrfs.h) ──────────────────────────────────────────────


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (0x94 << 8) | nr


_READ, _RW = 2, 3
TREE_SEARCH = _ioc(_RW, 17, 4096)          # struct btrfs_ioctl_search_args
INO_LOOKUP = _ioc(_RW, 18, 4096)           # struct btrfs_ioctl_ino_lookup_args
GET_SUBVOL_INFO = _ioc(_READ, 60, 504)     # struct btrfs_ioctl_get_subvol_info_args
U64 = 0xFFFFFFFFFFFFFFFF
FIRST_FREE_OBJECTID = 256                  # the subvolume's top directory
INODE_ITEM, INODE_REF, INODE_EXTREF = 1, 12, 13
_SEARCH_KEY = struct.Struct("=QQQQQQQIIII4Q")   # btrfs_ioctl_search_key, 104 bytes
_SEARCH_HDR = struct.Struct("=QQQII")           # btrfs_ioctl_search_header, 32 bytes


@dataclass
class SubvolInfo:
    tree_id: int
    uuid: str
    generation: int        # last committed transaction that changed this subvolume


def subvol_info(fd: int) -> SubvolInfo:
    buf = bytearray(504)
    fcntl.ioctl(fd, GET_SUBVOL_INFO, buf, True)
    tree_id = struct.unpack_from("=Q", buf, 0)[0]
    generation = struct.unpack_from("=Q", buf, 280)[0]   # after name[256], parent_id, dirid
    return SubvolInfo(tree_id, str(uuidlib.UUID(bytes=bytes(buf[296:312]))), generation)


def tree_search(fd: int, min_transid: int):
    """Every item of the fd's subvolume tree (objectid >= 256) in a leaf written in
    transaction >= min_transid: yields (objectid, type, offset, data)."""
    oid, typ, off = FIRST_FREE_OBJECTID, 0, 0
    while True:
        key = _SEARCH_KEY.pack(0, oid, U64, off, U64, min_transid, U64, typ, 255, 4096,
                               0, 0, 0, 0, 0)
        buf = bytearray(key + bytes(4096 - len(key)))
        fcntl.ioctl(fd, TREE_SEARCH, buf, True)
        nr = struct.unpack_from("=I", buf, 64)[0]          # nr_items, filled in
        if nr == 0:
            return
        pos = _SEARCH_KEY.size
        for _ in range(nr):
            _t, oid, off, typ, ln = _SEARCH_HDR.unpack_from(buf, pos)
            pos += _SEARCH_HDR.size
            yield oid, typ, off, bytes(buf[pos:pos + ln])
            pos += ln
        # continue right after the last key returned
        if off < U64:
            off += 1
        elif typ < 255:
            typ, off = typ + 1, 0
        elif oid < U64:
            oid, typ, off = oid + 1, 0, 0
        else:
            return


def ino_path(fd: int, ino: int) -> bytes | None:
    """Path of inode `ino` relative to the subvolume's top, b"" for the top itself.
    None: no longer there."""
    if ino == FIRST_FREE_OBJECTID:
        return b""
    buf = bytearray(struct.pack("=QQ", 0, ino) + bytes(4080))
    try:
        fcntl.ioctl(fd, INO_LOOKUP, buf, True)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (2, 116):                            # ENOENT, ESTALE: gone meanwhile
            return None
        raise
    return bytes(buf[16:]).split(b"\0", 1)[0].rstrip(b"/")


# inode_item: generation@0 transid@8 size@16 nbytes@24 block_group@32 nlink@40 uid@44
# gid@48 mode@52 rdev@56 flags@64 sequence@72 reserved@80 atime@112 ctime@124
# mtime@136 otime@148. What a change looks like: everything but transid and atime
# (a read that updates atime is not a change worth a listing).
def _inode_state(data: bytes) -> bytes:
    return data[16:112] + data[124:148]


CTIME_SLACK_S = 300   # a transaction stays open ~30 s; far longer only under heavy load


def changed_inodes(fd: int, since: int, seen: dict[int, tuple[int, bytes]],
                   min_ctime: float = 0):
    """(dirs, file_parents, file_inos_without_ref) written since transaction `since`
    whose state differs from what `seen` last recorded. Updates `seen`.

    min_ctime: skip inodes whose ctime is older. A read that updates atime rewrites
    the inode (a new transid) and nothing else; every real change (content, size,
    rename, entries added or removed, chmod) sets ctime to the time of the change,
    and userspace can't set ctime back."""
    dirs: set[int] = set()
    files: dict[int, int] = {}                 # file ino -> its transid
    refs: dict[int, set[int]] = {}             # ino -> parent inos (INODE_REF in the result)
    for oid, typ, off, data in tree_search(fd, since):
        if typ == INODE_ITEM and len(data) >= 160:
            transid = struct.unpack_from("=Q", data, 8)[0]
            if transid < since:
                continue                       # an old inode in a rewritten leaf
            state = _inode_state(data)
            prev = seen.get(oid)
            seen[oid] = (transid, state)
            if prev is not None and prev[1] == state:
                continue                       # reported already, unchanged since
            if struct.unpack_from("=Q", data, 124)[0] < min_ctime:
                continue                       # only atime: a read
            mode = struct.unpack_from("=I", data, 52)[0]
            if stat.S_ISDIR(mode):
                dirs.add(oid)
            else:
                files[oid] = transid
        elif typ == INODE_REF:
            refs.setdefault(oid, set()).add(off)
        elif typ == INODE_EXTREF and len(data) >= 8:
            refs.setdefault(oid, set()).add(struct.unpack_from("=Q", data, 0)[0])
    parents: set[int] = set()
    lone: set[int] = set()
    for ino in files:
        p = refs.get(ino)
        if p:
            parents |= p
        else:
            lone.add(ino)                      # its ref is in an older leaf: look it up
    return dirs, parents, lone


# ── shares ────────────────────────────────────────────────────────────────────


@dataclass
class Share:
    name: str                 # the SMB share name
    path: str                 # its root folder on the NAS
    user: str = ""            # one account's folder of a per-account share ("" = everyone's)

    @property
    def id(self) -> str:
        return f"{self.name}\n{self.user}" if self.user else self.name


_SUBST = re.compile(r"%[A-Za-z]")
_NOT_SHARES = {"global", "homes", "printers", "print$", "ipc$"}


class SambaConfig:
    """Shares from Samba's config files, read the way smbd reads them: sections,
    `include =` files inline, and an include with %U in a share's section (a
    per-account share, e.g. `include = /etc/samba/userconfig.d/share-%U.conf`)
    expanded into one share per account file that exists, each with that file's
    settings on top. testparm can't do the last (it renders %U as empty).

    Shares whose path still has a substitution in it (%H, %U ...) are left out:
    only the per-account files say where those are."""

    def __init__(self, path: str):
        self.path = path
        self._sig: tuple | None = None
        self.shares: list[Share] = []

    def _signature(self) -> tuple:
        out = []
        for p in self._watched:
            try:
                out.append((p, os.stat(p).st_mtime_ns))
            except OSError:
                out.append((p, None))
        return tuple(out)

    def read(self) -> tuple[list[Share], bool]:
        """(shares, changed since the last read). Files are read again only when one
        of them, or a folder an include pattern lists, has changed."""
        if self._sig is not None and self._signature() == self._sig:
            return self.shares, False
        self._watched: list[str] = []
        sections: dict[str, dict] = {}
        variants: dict[tuple[str, str], dict] = {}
        self._parse(self.path, sections, variants, None, None, 0)
        shares = []
        for name, kv in sections.items():
            if name.lower() in _NOT_SHARES or kv.get("available", "yes").lower() in ("no", "false", "0"):
                continue
            path = kv.get("path", "")
            if path and not _SUBST.search(path):
                shares.append(Share(name, path))
        for (name, user), kv in variants.items():
            path = {**sections.get(name, {}), **kv}.get("path", "")
            if path and not _SUBST.search(path):
                shares.append(Share(name, path, user))
        self.shares = shares
        self._sig = self._signature()
        return shares, True

    def _parse(self, path, sections, variants, section, user, depth):
        if depth > 8:
            return
        self._watched.append(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as e:
            log.warning("%s: %s", path, e)
            return
        cur = section                       # an included file goes on in its includer's section
        for raw in lines:
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            m = re.fullmatch(r"\[(.+)\]", line)
            if m:
                cur, user = m.group(1), None   # a section in a per-account file is a share of its own
                sections.setdefault(cur, {})
                continue
            k, eq, v = line.partition("=")
            if not eq or cur is None:
                continue
            k, v = " ".join(k.lower().split()), v.strip()
            if k == "include":
                if "%U" in v and cur and cur.lower() != "global" and user is None:
                    pat = v.replace("%U", "*")
                    self._watched.append(os.path.dirname(pat) or ".")
                    pre, _, post = v.partition("%U")
                    for f in sorted(glob.glob(pat)):
                        u = f[len(pre):len(f) - len(post)] if post else f[len(pre):]
                        if u and "/" not in u:
                            kv = variants.setdefault((cur, u), {})
                            self._parse_into(f, kv, sections, variants, depth + 1)
                elif not _SUBST.search(v):
                    self._parse(v, sections, variants, cur, user, depth + 1)
                continue
            if user is not None:
                variants.setdefault((cur, user), {})[k] = v
            else:
                sections.setdefault(cur, {})[k] = v

    def _parse_into(self, path, kv, sections, variants, depth):
        """A per-account file: its settings go to that account's variant of the share,
        until the file starts a section of its own."""
        self._watched.append(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as e:
            log.warning("%s: %s", path, e)
            return
        cur = None
        for raw in lines:
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            m = re.fullmatch(r"\[(.+)\]", line)
            if m:
                cur = m.group(1)
                sections.setdefault(cur, {})
                continue
            k, eq, v = line.partition("=")
            if not eq:
                continue
            k, v = " ".join(k.lower().split()), v.strip()
            if k == "include":
                continue                    # not nested any deeper on purpose
            (sections[cur] if cur else kv)[k] = v


# ── config ────────────────────────────────────────────────────────────────────


@dataclass
class Config:
    gateway: str                               # https://host:port of the direct listener
    pins: list[str]                            # base64 sha256 SPKI, current + standby
    key_file: str
    poll_s: float = 1.5
    heartbeat_s: float = 30.0
    discover_s: float = 60.0
    smb_conf: str = "/etc/samba/smb.conf"      # shares from Samba's config ("" = only [[share]])
    shares: list[Share] = field(default_factory=list)
    # never watched or reported: "share", "share/folder", globs allowed ("svc-*"), any case
    exclude: list[str] = field(default_factory=list)
    # per-account folders of these accounts are left out (globs, any case)
    exclude_users: list[str] = field(default_factory=list)
    max_dirs: int = 200_000                    # more in one round: the share is rescanned
    chunk: int = 2000                          # folders per request
    timeout_s: float = 20.0


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    shares = [Share(s["name"], s["path"], s.get("user", "")) for s in raw.pop("share", [])]
    cfg = Config(shares=shares, **raw)
    if not cfg.gateway.startswith("https://"):
        raise ValueError("gateway must be https://host:port (the direct listener)")
    if not cfg.pins:
        raise ValueError("pins: the gateway's SPKI pins are required")
    return cfg


# ── the gateway connection ────────────────────────────────────────────────────


def _der_next(b: bytes, i: int) -> tuple[int, int, int]:
    """(tag, start of content, end) of the DER element at b[i]."""
    tag, ln = b[i], b[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(b[i:i + n], "big")
        i += n
    return tag, i, i + ln


def spki_pin(cert_der: bytes) -> str:
    """base64(sha256(SubjectPublicKeyInfo)) of a DER certificate."""
    _t, s, _e = _der_next(cert_der, 0)              # Certificate
    _t, s, _e = _der_next(cert_der, s)              # tbsCertificate
    i, fields = s, []
    while len(fields) < 7:
        t, cs, ce = _der_next(cert_der, i)
        fields.append((t, i, ce))
        i = ce
    if fields[0][0] != 0xA0:                        # no [0] version: v1 certificate
        fields.insert(0, (0xA0, 0, 0))
    _t, start, end = fields[6]                      # version serial sig issuer validity subject SPKI
    return base64.b64encode(hashlib.sha256(cert_der[start:end]).digest()).decode()


class GatewayError(Exception):
    def __init__(self, status: int, body: dict | str):
        super().__init__(f"{status}: {body}")
        self.status, self.body = status, body


class GatewayDown(Exception):
    """The gateway can't be reached (network, TLS): nothing can be reported."""


GONE = (2, 116)              # ENOENT, ESTALE: the subvolume behind an open fd was deleted


class Gateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        u = cfg.gateway[len("https://"):].rstrip("/")
        host, _, port = u.rpartition(":") if re.search(r":\d+$", u) else (u, "", "443")
        self.host, self.port = host.strip("[]"), int(port)
        with open(cfg.key_file, "rb") as f:
            self.key = base64.b64decode(f.read().strip())
        if len(self.key) < 32:
            raise ValueError("key_file: need a base64 key of at least 32 bytes")
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE      # the pin below is the check
        self._last_ts = 0

    def post(self, path: str, body: dict) -> dict:
        data = json.dumps(body, separators=(",", ":")).encode()
        ts = max(int(time.time() * 1000), self._last_ts + 1)   # strictly increasing
        self._last_ts = ts
        msg = b"POST\n" + path.encode() + b"\n" + str(ts).encode() + b"\n" + data
        sig = hmac.new(self.key, msg, hashlib.sha256).hexdigest()
        conn = http.client.HTTPSConnection(self.host, self.port, context=self._ctx,
                                           timeout=self.cfg.timeout_s)
        try:
            conn.connect()
            pin = spki_pin(conn.sock.getpeercert(binary_form=True))
            if pin not in self.cfg.pins:
                raise GatewayError(0, f"the gateway's key is not pinned (got {pin})")
            conn.request("POST", path, body=data, headers={
                "Content-Type": "application/json",
                "Authorization": f"LoftAgent {ts}.{sig}",
                "User-Agent": f"loft-nas-agent/{VERSION}"})
            r = conn.getresponse()
            raw = r.read()
        except (OSError, http.client.HTTPException) as e:
            raise GatewayDown(f"{self.host}:{self.port}: {e}") from e
        finally:
            conn.close()
        try:
            out = json.loads(raw) if raw else {}
        except ValueError:
            out = raw[:200].decode("utf-8", "replace")
        if r.status != 200:
            raise GatewayError(r.status, out)
        return out


# ── the agent ─────────────────────────────────────────────────────────────────


class Watched:
    """One share: its root folder's fd, its place in the subvolume, what was reported."""

    def __init__(self, share: Share):
        self.share = share
        self.fd = os.open(share.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.info = subvol_info(self.fd)
            top = ino_path(self.fd, os.fstat(self.fd).st_ino)
            if top is None:
                raise OSError(f"{share.path}: can't find it in its subvolume")
            self.prefix = top                       # e.g. b".data", b"" at the subvolume top
        except BaseException:
            os.close(self.fd)
            raise
        st = os.fstat(self.fd)
        self.ident = (st.st_dev, st.st_ino)         # to notice a share recreated at its path
        # Reads rewrite inodes (atime) only without noatime; only then is the ctime
        # filter worth its one risk (a change committed > CTIME_SLACK_S late is skipped).
        self.atime_writes = not os.fstatvfs(self.fd).f_flag & os.ST_NOATIME
        self.cursor: int | None = None              # from the gateway
        self.cursor_wall = 0.0                      # when that generation was read (0: unknown)
        self.seen: dict[int, tuple[int, bytes]] = {}
        self.error = ""                             # why its last round failed ("" = it didn't)

    def close(self):
        os.close(self.fd)

    def rel(self, p: bytes) -> str | None:
        """Share-relative path of a subvolume path, None when outside the share or
        not valid UTF-8 (such a name can't be matched by the gateway: the nearest
        valid parent is reported instead)."""
        if self.prefix:
            if p == self.prefix:
                p = b""
            elif p.startswith(self.prefix + b"/"):
                p = p[len(self.prefix) + 1:]
            else:
                return None
        parts = []
        for x in p.split(b"/") if p else []:
            try:
                parts.append(x.decode("utf-8"))
            except UnicodeDecodeError:
                break
        return "/".join(parts)

    def changes(self, since: int, cfg: Config, since_wall: float = 0
                ) -> tuple[set[str] | None, int, dict, float]:
        """(changed folders since `since`, or None: too many; the generation they
        reach up to; what `seen` becomes once the gateway has them; when that
        generation was read). The generation is read first: whatever commits later
        has a higher transid and is found by the next round."""
        wall = time.time()
        upto = subvol_info(self.fd).generation
        seen = dict(self.seen)
        min_ctime = since_wall - CTIME_SLACK_S if since_wall and self.atime_writes else 0
        dirs, parents, lone = changed_inodes(self.fd, since, seen, min_ctime)
        # forget what the next round (from `upto`) can't return again
        seen = {k: v for k, v in seen.items() if v[0] >= upto}
        if len(dirs) + len(parents) + len(lone) > cfg.max_dirs:
            log.warning("%s: more than %d changed folders; the gateway rescans it",
                        self.share.name, cfg.max_dirs)
            return None, upto, seen, wall
        out: set[str] = set()
        try:
            for ino in dirs | parents:
                p = ino_path(self.fd, ino)
                if p is not None:
                    r = self.rel(p)
                    if r is not None:
                        out.add(r)
            for ino in lone:
                p = ino_path(self.fd, ino)
                if p is not None:
                    r = self.rel(p.rpartition(b"/")[0] if b"/" in p else b"")
                    if r is not None:
                        out.add(r)
        except OSError as e:
            if e.errno in GONE:
                raise
            # e.g. ENAMETOOLONG: a path longer than the lookup can return (Samba allows
            # it). Which folder it was can't be said: the gateway rescans the share.
            log.warning("%s: can't look up a changed folder (%s); the gateway rescans it",
                        self.share.name, e)
            return None, upto, seen, wall
        return {d for d in out if not excluded(cfg, self.share.name, d)}, upto, seen, wall


def excluded(cfg: Config, share: str, rel: str) -> bool:
    p = (share + "/" + rel).rstrip("/").casefold()
    for x in cfg.exclude:
        x = x.strip("/").casefold()
        if fnmatch.fnmatchcase(p, x) or fnmatch.fnmatchcase(p, x + "/*"):
            return True
    return False


class Agent:
    def __init__(self, cfg: Config, gw: Gateway | None = None):
        self.cfg = cfg
        self.gw = gw or Gateway(cfg)
        self.watched: dict[str, Watched] = {}
        self.stop = False
        self._next_discover = 0.0
        self._next_sync = 0.0
        self.samba = SambaConfig(cfg.smb_conf) if cfg.smb_conf else None
        self._want: dict[str, Share] | None = None
        self._unwatchable: dict[str, str] = {}      # share id -> why (logged once)

    def _wanted(self, shares: list[Share]) -> dict[str, Share]:
        want = {s.id: s for s in shares}
        for s in self.cfg.shares:
            want[s.id] = s
        return {k: s for k, s in want.items() if not excluded(self.cfg, s.name, "")
                and not (s.user and any(fnmatch.fnmatchcase(s.user.casefold(), u.casefold())
                                        for u in self.cfg.exclude_users))}

    def discover(self):
        """Watch what Samba shares (re-read only when its files change). Every call
        also retries shares that couldn't be opened (the volume not mounted yet) and
        reopens shares recreated at their path."""
        if self.samba is not None:
            shares, changed = self.samba.read()
            if changed or self._want is None:
                self._want = self._wanted(shares)
        elif self._want is None:
            self._want = self._wanted([])
        want = self._want
        for k, w in list(self.watched.items()):
            why = ""
            if k not in want or w.share.path != want[k].path:
                why = "no longer shared"
            else:
                try:
                    st = os.stat(w.share.path)
                    if (st.st_dev, st.st_ino) != w.ident:
                        why = "replaced"
                except OSError as e:
                    why = str(e)
            if why:
                self._drop(k, why)
        for k, s in want.items():
            if k in self.watched:
                continue
            try:
                self.watched[k] = Watched(s)
            except OSError as e:
                if self._unwatchable.get(k) != str(e):
                    log.warning("can't watch %s (%s): %s; trying again every %d s",
                                s.name, s.path, e, self.cfg.discover_s)
                self._unwatchable[k] = str(e)
                continue
            self._unwatchable.pop(k, None)
            self._next_sync = 0
            log.info("watching %s%s (%s)", s.name, f" for {s.user}" if s.user else "", s.path)

    def _drop(self, k: str, why: str):
        """Stop watching a share; the next discover opens it again if it is there.
        The gateway counts it as not reported until then."""
        w = self.watched.pop(k, None)
        if w is not None:
            log.warning("%s%s: %s; reopening", w.share.name,
                        f" for {w.share.user}" if w.share.user else "", why)
            w.close()
        self._next_sync = 0
        self._next_discover = 0

    def sync(self):
        """Tell the gateway what is watched; take its cursors."""
        shares = []
        for k, w in list(self.watched.items()):
            try:
                info = subvol_info(w.fd)
            except OSError as e:
                if e.errno not in GONE:
                    raise
                self._drop(k, "its subvolume is gone")
                continue
            w.info = info
            # ok: its last round went through (the gateway being unreachable doesn't
            # count). A share whose rounds keep failing is left uncovered by the
            # gateway, so the periodic full pass looks after it.
            shares.append({"id": k, "share": w.share.name, "user": w.share.user,
                           "uuid": info.uuid, "tree_id": info.tree_id,
                           "generation": info.generation, "ok": not w.error})
        r = self.gw.post("/v1/nas-agent/sync", {"agent": VERSION, "host": socket.gethostname(),
                                                 "shares": shares})
        cursors = r.get("cursors", {})
        for k, w in self.watched.items():
            c = cursors.get(k)
            if c is None or c != w.cursor:
                w.seen.clear()
                w.cursor_wall = 0.0                 # unknown: no ctime filter this round
            w.cursor = c

    def round(self):
        """One look at every share. A failure on one share doesn't hold up the others:
        it is logged, and the share stops counting as reported (see sync) until its
        rounds go through again. Only an unreachable gateway ends the round."""
        for k, w in list(self.watched.items()):
            if w.cursor is None:
                continue
            try:
                self._round_one(k, w)
            except GatewayDown:
                raise
            except GatewayError as e:
                if e.status == 409:                 # its cursor moved: sync again
                    log.info("%s: %s; syncing again", w.share.name, e.body)
                    self._next_sync = 0
                    continue
                if e.status in (0, 401, 403) or e.status >= 500:
                    raise                           # not this share's doing
                self._failed(w, e)
            except OSError as e:
                if e.errno in GONE:                 # deleted (and maybe recreated) under us
                    self._drop(k, "its subvolume is gone")
                    continue
                self._failed(w, e)
            except Exception as e:  # noqa: BLE001
                self._failed(w, e)
            else:
                if w.error:
                    log.info("%s: reporting again", w.share.name)
                    w.error = ""

    @staticmethod
    def _failed(w: Watched, e: Exception):
        if w.error != repr(e):
            log.warning("%s: %r; the gateway falls back to full passes for it until this clears",
                        w.share.name, e)
        w.error = repr(e)

    def _round_one(self, k: str, w: Watched):
        if subvol_info(w.fd).uuid != w.info.uuid:
            log.warning("%s: its subvolume changed; telling the gateway", w.share.name)
            self._next_sync = 0
            return
        dirs, upto, seen, wall = w.changes(w.cursor, self.cfg, w.cursor_wall)
        if dirs is not None and not dirs and upto == w.cursor:
            w.seen = seen
            if not w.cursor_wall:
                w.cursor_wall = wall
            return
        base = {"id": k, "uuid": w.info.uuid, "since": w.cursor, "until": upto}
        overflow = {**base, "overflow": True, "final": True, "dirs": []}
        if dirs is None:                        # changes() said why
            self.gw.post("/v1/nas-agent/changes", overflow)
        else:
            ds = sorted(dirs)
            chunks = [ds[i:i + self.cfg.chunk] for i in range(0, len(ds), self.cfg.chunk)] or [[]]
            try:
                for n, ch in enumerate(chunks):
                    self.gw.post("/v1/nas-agent/changes", {**base, "dirs": ch,
                                                            "final": n == len(chunks) - 1})
            except GatewayError as e:
                if e.status not in (400, 413):
                    raise
                # the gateway won't take this report (too big, a path it refuses):
                # it rescans the share instead; the cursor hasn't moved, so `since` holds
                log.warning("%s: the gateway refused the report (%s); it rescans the share",
                            w.share.name, e)
                self.gw.post("/v1/nas-agent/changes", overflow)
            if ds:
                log.debug("%s: %d folder(s) since %d", w.share.name, len(ds), w.cursor)
        if upto != w.cursor or not w.cursor_wall:
            w.cursor_wall = wall
        w.cursor, w.seen = upto, seen           # only now: a failed post reports them again

    def run(self):
        backoff = 1.0
        while not self.stop:
            now = time.monotonic()
            try:
                if now >= self._next_discover:
                    self.discover()
                    self._next_discover = now + self.cfg.discover_s
                if now >= self._next_sync:
                    self.sync()
                    self._next_sync = now + self.cfg.heartbeat_s
                self.round()
                backoff = 1.0
                time.sleep(self.cfg.poll_s)
            except GatewayError as e:
                if e.status == 409:                     # cursor moved: start over from its
                    log.info("gateway: %s; syncing again", e.body)
                    self._next_sync = 0
                    continue
                log.warning("gateway: %s", e)
                self._next_sync = 0
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except (GatewayDown, OSError) as e:
                log.warning("%s", e)
                self._next_sync = 0
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception:  # noqa: BLE001  (stay up; systemd would only restart us)
                log.exception("round failed")
                self._next_sync = 0
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        for w in self.watched.values():
            w.close()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=os.environ.get("LOFT_AGENT_CONFIG",
                                                       "/data/loft-agent/agent.toml"))
    ap.add_argument("--print-changes", metavar="SINCE", type=int,
                    help="print the folders changed since this transaction on every share, "
                         "then exit (nothing is sent)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    cfg = load_config(a.config)
    if a.print_changes is not None:
        ag = Agent(cfg, gw=object())                    # type: ignore[arg-type]
        ag.discover()
        for w in ag.watched.values():
            dirs, upto, _, _ = w.changes(a.print_changes, cfg)
            print(f"{w.share.id!r} uuid={w.info.uuid} generation={upto}"
                  f" {'TOO MANY' if dirs is None else len(dirs)} folder(s)")
            for d in sorted(dirs or ()):
                print("   ", d or "(share root)")
        return 0
    ag = Agent(cfg)

    def _stop(*_):
        ag.stop = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("loft-nas-agent %s: reporting to %s", VERSION, cfg.gateway)
    ag.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
