"""A root's view of the NAS: root-relative "virtual paths" in, (share, path) out.

A root is either one folder in one share (`share`, `path`), or ALL the shares
the signed-in account can open (`share == ALL_SHARES`). In the second case the
first component of every path is the share, and the root's own listing is the
share list. Stored paths (uploads, versions, trash) are virtual paths too, so
one translation point covers every NAS call.

Blocked shares never appear and every call into one is refused here, whatever
the account can open: this is the wall that keeps e.g. a backup share off
laptops when an admin account that can open everything signs in. It holds for
single-share roots too, so a root saved before its share was blocked stops working.

Blocked paths are folders inside an allowed share that must stay off Macs
(e.g. `Family/Paperwork/Passwords`). They are
left out of every listing and every call into them is NotFound (404, not 403,
so the name isn't confirmed). Matching is on a normalised key: NFC,
case-folded, `.`/`..`/empty parts resolved, since the NAS is case-insensitive
and macOS sends decomposed (NFD) names.
"""
from __future__ import annotations

import time
import unicodedata
from typing import Iterator

from .storage.base import Entry, Exists, NotFound, Session, from_nas, join

ALL_SHARES = "*"
SHARES_TTL_S = 60


class FixedItem(Exception):
    """The operation would create, rename, move or delete a share itself."""


class CrossShare(Exception):
    """A rename whose source and destination are in different shares."""


class Blocked(Exception):
    pass


def is_blocked(share: str, blocked: list[str]) -> bool:
    return share.casefold() in {b.casefold() for b in blocked}


def path_key(path: str) -> str:
    # from_nas: a client path may spell ':' as the NAS does (\uf022); both open the
    # same entry, so both must match the same blocked path.
    parts: list[str] = []
    for part in unicodedata.normalize("NFC", from_nas(path)).replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(unicodedata.normalize("NFC", part.casefold()))
    return "/".join(parts)


def has_dot_parts(path: str) -> bool:
    """A '.' or '..' part, with '/' or '\\' as separator. Never a real name; refused
    everywhere so a path can't climb."""
    return any(p in (".", "..") for p in path.replace("\\", "/").split("/"))


def is_path_blocked(share: str, path: str, blocked_paths: list[str]) -> bool:
    """True if `share/path` is a blocked path or anything under one. The share is
    fixed and '..' in `path` stops at the share root, as on the NAS: smbclient
    normalises the UNC path, so '<share>/a/../../x' opens '<share>/x'."""
    key = "/".join(k for k in (path_key(share), path_key(path)) if k)
    for b in blocked_paths:
        bk = path_key(b)
        if key == bk or key.startswith(bk + "/"):
            return True
    return False


def holds_blocked_path(share: str, path: str, blocked_paths: list[str]) -> bool:
    """True if a blocked path lies strictly inside `share/path`. Renaming, moving or
    deleting such a folder would carry the hidden folder with it, and a rename would
    also take it out from under its block."""
    key = "/".join(k for k in (path_key(share), path_key(path)) if k)
    return any(path_key(b).startswith(key + "/") for b in blocked_paths)


class RootFS:
    def __init__(self, root, session: Session, blocked: list[str], offered: list[str],
                 share_cache: dict | None = None, blocked_paths: list[str] = ()):
        self.root, self.sess, self.blocked, self.offered = root, session, blocked, offered
        self.blocked_paths = list(blocked_paths)
        self.multi = root["share"] == ALL_SHARES
        self._cache = share_cache if share_cache is not None else {}

    # ── shares ────────────────────────────────────────────────────────────

    def shares(self) -> list[str]:
        """Shares this account can open, minus blocked ones. With an explicit
        `shares` list in the config, only those are offered (and checked one by
        one); otherwise the NAS's own per-user share list is used."""
        key = self.sess.user.casefold()          # per account, never per object id
        hit = self._cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        if self.offered:
            names = [s for s in self.offered if self.sess.can_access(s)]
        else:
            names = self.sess.list_shares()
        names = sorted(s for s in names if not is_blocked(s, self.blocked))
        self._cache[key] = (time.time() + SHARES_TTL_S, names)
        return names

    def nas(self, vpath: str) -> tuple[str, str]:
        if self.multi:
            share, _, rest = vpath.strip("/").partition("/")
            if not share:
                raise FixedItem(vpath)
        else:
            share, rest = self.root["share"], vpath   # already share-relative
        if has_dot_parts(vpath):
            raise NotFound(vpath)
        if is_blocked(share, self.blocked):
            raise Blocked(share)
        if is_path_blocked(share, rest, self.blocked_paths):
            raise NotFound(vpath)
        return share, rest

    def _inside(self, vpath: str) -> tuple[str, str]:
        """(share, path) for a path strictly inside a share: never the share itself."""
        share, rest = self.nas(vpath)
        if self.multi and not rest:
            raise FixedItem(vpath)
        return share, rest

    def share_of(self, vpath: str) -> str:
        return self.nas(vpath)[0]

    def meta(self, kind: str, name: str, near: str | None) -> str:
        """Virtual path in the hidden area: `<root>/.loft/<kind>/<name>`, or in
        a multi-share root `<share of near>/.loft/<kind>/<name>`, so renames
        between the area and the file never cross a share."""
        if not self.multi:
            return join(self.root["path"], ".loft", kind, name)
        if not near:
            raise FixedItem("")
        return join(self.share_of(near), ".loft", kind, name)

    # ── file operations on virtual paths ──────────────────────────────────

    def stat(self, vpath: str) -> Entry:
        if self.multi and not vpath.strip("/"):
            return Entry("", True, 0, 0, None)
        share, rest = self.nas(vpath)
        if self.multi and not rest and share not in self.shares():
            raise NotFound(share)
        e = self.sess.stat(share, rest)
        return e if rest or not self.multi else Entry(share, True, 0, e.mtime_ns, None)

    def scandir(self, vpath: str) -> list[Entry]:
        if self.multi and not vpath.strip("/"):
            # file_id None: shares never take part in move detection
            return [Entry(s, True, 0, 0, None) for s in self.shares()]
        share, rest = self.nas(vpath)
        return [e for e in self.sess.scandir(share, rest)
                if not is_path_blocked(share, join(rest, e.name), self.blocked_paths)]

    def read(self, vpath: str, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        return self.sess.read(*self._inside(vpath), offset, length)

    def write_at(self, vpath: str, offset: int, data: bytes):
        self.sess.write_at(*self._inside(vpath), offset, data)

    def create_empty(self, vpath: str):
        self.sess.create_empty(*self._inside(vpath))

    def makedirs(self, vpath: str):
        self.sess.makedirs(*self._inside(vpath))

    def _movable(self, vpath: str) -> tuple[str, str]:
        """(share, path) for something that may be renamed, moved or deleted:
        never a share, never a folder that holds a blocked path."""
        share, rest = self._inside(vpath)
        if holds_blocked_path(share, rest, self.blocked_paths):
            raise FixedItem(vpath)
        return share, rest

    def remove(self, vpath: str):
        self.sess.remove(*self._movable(vpath))

    def rmtree(self, vpath: str):
        self.sess.rmtree(*self._movable(vpath))

    def rename(self, src: str, dst: str, replace: bool = False):
        s_share, s_rest = self._movable(src)
        d_share, d_rest = self._movable(dst)
        if s_share != d_share:
            raise CrossShare(f"{src} -> {dst}")
        self.sess.rename(s_share, s_rest, d_rest, replace=replace)

    def copy(self, src: str, dst: str):
        """Byte copy, streamed through the gateway. Only for crossing shares: a
        resumed upload whose temp file is in another share, and move_across."""
        off = 0
        self.create_empty(dst)
        for chunk in self.read(src):
            self.write_at(dst, off, chunk)
            off += len(chunk)

    def move_across(self, src: str, dst: str):
        """Move between shares, which SMB rename can't: copy (keeping mtimes, so
        the index sees no content change and nothing re-downloads), then delete
        the source. Not atomic: if it stops halfway, the next rescan indexes
        what is where, and nothing is lost (the source goes only after its copy)."""
        self._movable(src)                    # before copying anything, not at rmtree
        st = self.stat(src)
        try:
            self.stat(dst)
            raise Exists(dst)
        except NotFound:
            pass
        if st.is_dir:
            self.makedirs(dst)
            for e in self.scandir(src):
                self.move_across(join(src, e.name), join(dst, e.name))
            self.rmtree(src)
        else:
            self.copy(src, dst)
            self.sess.set_mtime(*self._inside(dst), st.mtime_ns)
            self.remove(src)
