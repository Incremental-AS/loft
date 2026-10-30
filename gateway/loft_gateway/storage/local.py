"""Local-directory backend for tests and dev. Shares are directories; users are
a dict of password + the shares they may open. Not for production."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterator

from .base import AccessDenied, AuthFailed, Entry, Exists, NotFound, from_nas, refuse_short_names, to_nas


class LocalSession:
    def __init__(self, backend: "LocalBackend", user: str):
        self.b, self.user = backend, user

    def _p(self, share: str, path: str) -> Path:
        if share not in self.b.users[self.user]["shares"]:
            raise AccessDenied(share)
        base = (self.b.root / share).resolve()
        path = to_nas(path)      # names on disk as Samba stores them
        refuse_short_names(path, lambda parent, part: self._listed(base.joinpath(*parent), part))
        p = (base / self._long(base, path)).resolve() if path else base
        if p != base and base not in p.parents:
            raise AccessDenied(path)
        return p

    # Samba emulation for tests: backend.short_names = {"FAMILY~1": "family_photos"}
    # opens the long name through its alias, case-insensitively, as Samba does.
    def _alias(self, d: Path, part: str) -> str | None:
        long = self.b.short_names.get(part.upper())
        return long if long and (d / long).exists() and not (d / part).exists() else None

    def _long(self, base: Path, path: str) -> str:
        d, out = base, []
        for part in path.strip("/").split("/"):
            part = self._alias(d, part) or part
            out.append(part)
            d = d / part
        return "/".join(out)

    def _listed(self, d: Path, part: str) -> list[str]:
        if (d / part).exists():
            return [part]
        return [a] if (a := self._alias(d, part)) else []

    def can_access(self, share: str) -> bool:
        return share in self.b.users[self.user]["shares"] and (self.b.root / share).is_dir()

    def list_shares(self) -> list[str]:
        return sorted(s for s in self.b.users[self.user]["shares"] if (self.b.root / s).is_dir())

    @staticmethod
    def _entry(p: Path) -> Entry:
        st = p.stat()
        return Entry(from_nas(p.name), p.is_dir(), 0 if p.is_dir() else st.st_size,
                     st.st_mtime_ns, st.st_ino)

    def stat(self, share, path):
        p = self._p(share, path)
        if not p.exists():
            raise NotFound(path)
        return self._entry(p)

    def scandir(self, share, path):
        p = self._p(share, path)
        if not p.is_dir():
            raise NotFound(path)
        return [self._entry(c) for c in p.iterdir()]

    def read(self, share, path, offset=0, length=None, block=4 << 20) -> Iterator[bytes]:
        p = self._p(share, path)
        if not p.is_file():
            raise NotFound(path)
        with open(p, "rb") as f:
            f.seek(offset)
            left = length
            while left is None or left > 0:
                chunk = f.read(block if left is None else min(block, left))
                if not chunk:
                    return
                if left is not None:
                    left -= len(chunk)
                yield chunk

    def write_at(self, share, path, offset, data):
        p = self._p(share, path)
        with open(p, "r+b" if p.exists() else "wb") as f:
            f.seek(offset)
            f.write(data)

    def create_empty(self, share, path):
        self._p(share, path).write_bytes(b"")

    def makedirs(self, share, path):
        self._p(share, path).mkdir(parents=True, exist_ok=True)

    def rename(self, share, src, dst, replace=False):
        s, d = self._p(share, src), self._p(share, dst)
        if not s.exists():
            raise NotFound(src)
        if d.exists() and not replace:
            raise Exists(dst)
        os.replace(s, d)

    def remove(self, share, path):
        p = self._p(share, path)
        if not p.exists():
            raise NotFound(path)
        p.unlink()

    def rmtree(self, share, path):
        p = self._p(share, path)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    def set_mtime(self, share, path, mtime_ns):
        os.utime(self._p(share, path), ns=(mtime_ns, mtime_ns))

    def close(self):
        pass


class LocalBackend:
    def __init__(self, root: str | Path, users: dict[str, dict],
                 short_names: dict[str, str] | None = None):
        """users = {"alice": {"password": "...", "shares": ["home", ...]}}"""
        self.root = Path(root)
        self.users = users
        self.short_names = {k.upper(): v for k, v in (short_names or {}).items()}

    def authenticate(self, user: str, password: str) -> LocalSession:
        u = self.users.get(user)
        if not u or u["password"] != password:
            raise AuthFailed(user)
        return LocalSession(self, user)
