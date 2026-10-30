"""Storage backend interface. Paths are share-relative POSIX strings ('' = share root).

A `Session` is one authenticated NAS user. Every file operation the gateway
does on behalf of a user goes through that user's session, so NAS permissions
apply natively and the gateway never needs its own ACL logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol


class StorageError(Exception):
    pass


class AuthFailed(StorageError):
    pass


class NotFound(StorageError):
    pass


class AccessDenied(StorageError):
    pass


class Exists(StorageError):
    pass


def refuse_short_names(path: str, lookup) -> None:
    """Samba's `mangled names` (on by default) give every long name
    a case-insensitive 8.3 alias such as FAMILY~1, which opens the real entry. A path
    through an alias dodges every name-based check above the session (blocked paths
    above all), so a part containing '~' must be some entry's own name. `lookup(parent
    parts, part)` returns the names the server lists for `part` as a search pattern,
    which matches short names too. Some listed but none of them is `part`: an alias,
    refused as NotFound. None listed: a new name, left to the server. Lookup failing:
    NotFound."""
    parts = [x for x in path.strip("/").split("/") if x]
    for i, part in enumerate(parts):
        if "~" not in part:
            continue
        try:
            names = lookup(parts[:i], part)
        except Exception as e:  # noqa: BLE001
            raise NotFound(path) from e
        if names and part.casefold() not in {n.casefold() for n in names}:
            raise NotFound(path)


# Characters SMB can't carry in a name go over the wire as Private Use code points
# (the "SFM" mapping macOS, Windows and Samba's catia/fruit share): a Mac saving
# "a | b: c" leaves "a \uf027 b\uf022 c" on the NAS, and the Mac's own SMB client
# shows it as "a | b: c" again (':' reads as '/' in Finder). The gateway does the
# same at the backend edge, so the index, the API and the Mac only see the real
# characters. Not mapped: \uf026 ('\\' would split the path) and \uf001-\uf01f,
# \uf028/\uf029 (control characters, trailing space/period), left as they are.
_SFM = {0xF020: '"', 0xF021: "*", 0xF022: ":", 0xF023: "<", 0xF024: ">", 0xF025: "?",
        0xF027: "|"}
_SFM_BACK = {ord(v): k for k, v in _SFM.items()}
SFM_PRIVATE = frozenset(chr(k) for k in _SFM)


def from_nas(name: str) -> str:
    return name.translate(_SFM)


def to_nas(path: str) -> str:
    return path.translate(_SFM_BACK)


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int
    mtime_ns: int
    file_id: int | None


def join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def split(path: str) -> tuple[str, str]:
    path = path.strip("/")
    if "/" not in path:
        return "", path
    head, tail = path.rsplit("/", 1)
    return head, tail


class Session(Protocol):
    user: str

    def can_access(self, share: str) -> bool: ...
    def list_shares(self) -> list[str]: ...
    def stat(self, share: str, path: str) -> Entry: ...
    def scandir(self, share: str, path: str) -> list[Entry]: ...
    def read(self, share: str, path: str, offset: int = 0,
             length: int | None = None, block: int = 4 << 20) -> Iterator[bytes]: ...
    def write_at(self, share: str, path: str, offset: int, data: bytes) -> None: ...
    def create_empty(self, share: str, path: str) -> None: ...
    def makedirs(self, share: str, path: str) -> None: ...
    def rename(self, share: str, src: str, dst: str, replace: bool = False) -> None: ...
    def remove(self, share: str, path: str) -> None: ...
    def rmtree(self, share: str, path: str) -> None: ...
    def set_mtime(self, share: str, path: str, mtime_ns: int) -> None: ...
    def close(self) -> None: ...


class Backend(Protocol):
    def authenticate(self, user: str, password: str) -> Session: ...
