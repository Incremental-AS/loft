"""Content search: find files by the words inside them, over what the caller may read.

Two parts:

* `ContentIndex`: a full-text index (SQLite FTS5, in the gateway's own database) of the
  text of documents, keyed by the gateway's item ids and scoped by index root. It knows
  nothing about NAS shares or rights; a hosted store can feed the same index with
  its own scopes.
* `Indexer`: keeps the index in step with the files. A background thread reads the text
  of every new or changed document in each root, as that root's own NAS account (a live
  session of one of its signed-in devices: the gateway never keeps a NAS password, so a
  root is indexed while that account has a device connected), through the sandboxed
  worker that also makes thumbnails (thumbworker.py). A file that goes to the trash, is
  deleted or replaced leaves the index in the same transaction that marks it deleted
  (Gateway._mark_deleted); the thread's sweep drops anything else that is gone.

The index is only a list of candidates. Gateway.search and the MCP tool check every hit
again when it is asked for: the item must still be live in the caller's own root (or a
folder the assistant may read now), and the caller's NAS session must still be able to
open it. A hit that fails either never leaves the gateway.

Words: NFKC, case-folded, split on anything that isn't a letter, digit or combining mark;
Chinese and Japanese runs become overlapping character pairs. Each word is indexed as
itself and as its Snowball stem in each of `search_languages`, so "budgets" finds
"budget" and "regnskapet" finds "regnskap" (docs/search.md lists the languages). A query
matches a file that has every word of the query, in any of its forms.

The extracted text is kept zlib-compressed beside the index (capped at
search_text_max_chars) so a hit can show a short passage; it goes with the index entry.
"""
from __future__ import annotations

import base64
import functools
import logging
import re
import sqlite3
import threading
import time
import unicodedata
import zlib
from typing import Iterable, Iterator

from .thumbs import TEXT_KINDS

log = logging.getLogger("loft.search")

try:  # Snowball stemmers (BSD-3-Clause, pure Python). Without them: whole words only.
    import snowballstemmer
except ImportError:  # pragma: no cover - the lock installs it
    snowballstemmer = None

SCHEMA_VERSION = "1"

# Language tags (the suite's launch locales, BCP 47 primary subtag) -> Snowball algorithm.
# Launch languages not listed here are indexed and searched as whole words, no stemming.
STEMMERS = {
    "ar": "arabic", "cs": "czech", "da": "danish", "de": "german", "el": "greek",
    "en": "english", "es": "spanish", "et": "estonian", "fi": "finnish", "fr": "french",
    "hi": "hindi", "hu": "hungarian", "id": "indonesian", "it": "italian",
    "lt": "lithuanian", "nb": "norwegian", "nl": "dutch", "nn": "norwegian",
    "pl": "polish", "pt": "portuguese", "ro": "romanian", "sv": "swedish", "tr": "turkish",
}
WORDS_ONLY = ("bg", "hr", "ja", "ko", "lv", "sk", "sl", "uk", "zh")
LANGUAGES = tuple(sorted(set(STEMMERS) | set(WORDS_ONLY)))

# Plain text files indexed as they are (UTF-8, UTF-16 with a BOM, else Windows-1252).
PLAIN_KINDS = frozenset(("txt", "text", "md", "markdown", "mdown", "csv", "tsv", "log",
                         "json", "yaml", "yml"))
PLAIN_READ_MAX = 8 << 20          # bytes of a plain text file that are read
MAX_TOKEN = 48                    # longer "words" are hashes, base64 or binary noise
MAX_QUERY_WORDS = 16
SNIPPET_MAX = 400

# Combining marks belong to the word (Devanagari vowel signs, Arabic harakat, ...):
# Python's \w alone splits "हिन्दी" into pieces.
_MARKS = "".join(
    re.escape(chr(c)) for c in range(0x300, 0x20000)
    if unicodedata.category(chr(c)) in ("Mn", "Mc", "Me"))
_WORD = re.compile(rf"(?:[^\W_]|[{_MARKS}])+")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿"
                  r"\U00020000-\U0003134f]+")


def language_algorithm(tag: str) -> str | None:
    return STEMMERS.get(tag.split("-")[0].lower())


def check_languages(tags: list[str]) -> None:
    for t in tags:
        if not isinstance(t, str) or t.split("-")[0].lower() not in LANGUAGES:
            raise ValueError(f"search_languages: {t!r} is not a supported language tag "
                             f"(one of {', '.join(LANGUAGES)})")


def indexable(name: str) -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in PLAIN_KINDS or ext in TEXT_KINDS


# ── words and their forms ─────────────────────────────────────────────────

def words(text: str) -> Iterator[str]:
    """The words of a text, normalised (NFKC, case-folded). CJK runs as character pairs."""
    text = unicodedata.normalize("NFKC", text).casefold()
    for m in _WORD.finditer(text):
        w = m.group()
        if not _CJK.search(w):
            if len(w) <= MAX_TOKEN:
                yield w
            continue
        pos = 0
        for c in _CJK.finditer(w):
            if c.start() > pos and c.start() - pos <= MAX_TOKEN:
                yield w[pos:c.start()]
            run = c.group()
            if len(run) == 1:
                yield run
            for i in range(len(run) - 1):
                yield run[i:i + 2]
            pos = c.end()
        if pos < len(w) and len(w) - pos <= MAX_TOKEN:
            yield w[pos:]


def words_in(query: str) -> bool:
    return next(words(query or ""), None) is not None


class Forms:
    """A word and its stems in the configured languages (cached; thread-safe)."""

    def __init__(self, languages: Iterable[str]):
        algos = []
        for t in languages:
            a = language_algorithm(t)
            if a and a not in algos:
                algos.append(a)
        self.algorithms = tuple(algos) if snowballstemmer else ()
        if algos and not snowballstemmer:
            log.warning("snowballstemmer is not installed: content search matches whole words only")
        self._stemmers = [snowballstemmer.stemmer(a) for a in self.algorithms]
        self._lock = threading.Lock()
        self.of = functools.lru_cache(maxsize=200_000)(self._of)

    @property
    def key(self) -> str:
        """What the index was built with: a different set re-indexes every document."""
        return ",".join(self.algorithms) or "-"

    def _of(self, word: str) -> tuple[str, ...]:
        out = [word]
        if self._stemmers and not _CJK.match(word):
            with self._lock:
                for s in self._stemmers:
                    st = s.stemWord(word)
                    if st and st not in out:
                        out.append(st)
        return tuple(out)


# ── the index ─────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS search_docs (
  id INTEGER PRIMARY KEY,            -- = search_fts rowid
  item_id TEXT NOT NULL UNIQUE,
  scope TEXT NOT NULL,               -- the index root (NAS: roots.id)
  content_version INTEGER NOT NULL,  -- of the item when read, -1 = waiting for a retry
  forms TEXT NOT NULL,               -- Forms.key it was indexed with
  state TEXT NOT NULL,               -- indexed | empty | skipped | failed | retry
  size INTEGER NOT NULL DEFAULT 0,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  retry_at REAL,
  text BLOB,                         -- zlib of the indexed text, for passages
  indexed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS search_docs_scope ON search_docs(scope);
"""


def _scope_token(scope: str) -> str:
    # Index roots are hex ids; anything else is hashed into one word.
    if re.fullmatch(r"[0-9a-f]{1,64}", scope):
        return "r" + scope
    return "r" + format(zlib.crc32(scope.encode()), "08x") + str(len(scope))


class ContentIndex:
    """The full-text index. All writes go through the gateway DB's transactions."""

    def __init__(self, db, forms: Forms, text_max_chars: int):
        self.db, self.forms, self.text_max = db, forms, text_max_chars
        self.available = True
        with db.tx() as c:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            have = c.execute("SELECT name FROM sqlite_master WHERE name='search_fts'").fetchone()
            if not have:
                try:
                    # contentless: the index keeps no copy of the words (SQLite >= 3.43)
                    c.execute("CREATE VIRTUAL TABLE search_fts USING fts5(scope, body, content='',"
                              " contentless_delete=1, tokenize='unicode61 remove_diacritics 0')")
                except sqlite3.OperationalError as e:
                    if "contentless_delete" not in str(e):
                        raise
                    c.execute("CREATE VIRTUAL TABLE search_fts USING fts5(scope, body,"
                              " tokenize='unicode61 remove_diacritics 0')")
            c.execute("INSERT OR REPLACE INTO meta VALUES ('search_schema', ?)", (SCHEMA_VERSION,))

    # writes (inside the caller's transaction `c`)

    def remove(self, c, item_id: str) -> None:
        r = c.execute("SELECT id, state FROM search_docs WHERE item_id=?", (item_id,)).fetchone()
        if r is None:
            return
        if r["state"] == "indexed":
            c.execute("DELETE FROM search_fts WHERE rowid=?", (r["id"],))
        c.execute("DELETE FROM search_docs WHERE id=?", (r["id"],))

    def put(self, c, item_id: str, scope: str, content_version: int, size: int, mtime_ns: int,
            text: str | None, state: str = "indexed", attempts: int = 0,
            retry_at: float | None = None) -> None:
        """Index (or re-index) one document. text None: a row that records why it has none."""
        self.remove(c, item_id)
        blob = None
        if text is not None:
            text = text[:self.text_max]
            body = " ".join(f for w in words(text) for f in self.forms.of(w))
            if not body:
                text, state = None, "empty"
        if text is None and state == "indexed":
            state = "empty"
        if text is not None:
            blob = zlib.compress(text.encode("utf-8"), 6)
        rid = c.execute(
            "INSERT INTO search_docs (item_id, scope, content_version, forms, state, size,"
            " mtime_ns, attempts, retry_at, text, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, scope, content_version, self.forms.key, state, size, mtime_ns, attempts,
             retry_at, blob, time.time())).lastrowid
        if text is not None:
            c.execute("INSERT INTO search_fts (rowid, scope, body) VALUES (?,?,?)",
                      (rid, _scope_token(scope), body))

    # reads

    def expression(self, query: str) -> str | None:
        """FTS5 query for every word of `query` in any of its forms, or None (no words)."""
        terms = []
        for w in dict.fromkeys(words(query)):
            terms.append("(" + " OR ".join(f'body:"{f}"' for f in self.forms.of(w)) + ")")
            if len(terms) >= MAX_QUERY_WORDS:
                break
        return " AND ".join(terms) or None

    def candidates(self, scopes: list[str], query: str, offset: int, limit: int) -> list:
        """Rows (id, item_id, scope, content_version, size, mtime_ns, rank), best first."""
        expr = self.expression(query)
        if not expr or not scopes:
            return []
        where = "(" + " OR ".join(f'scope:"{_scope_token(s)}"' for s in scopes) + ") AND " + expr
        try:
            return self.db.all(
                "SELECT d.id, d.item_id, d.scope, d.content_version, d.size, d.mtime_ns, f.rank"
                " FROM search_fts f"
                " JOIN search_docs d ON d.id = f.rowid WHERE search_fts MATCH ?"
                " AND d.state='indexed' ORDER BY f.rank LIMIT ? OFFSET ?",
                (where, limit, offset))
        except sqlite3.OperationalError as e:  # a query FTS5 can't parse: no hits, logged
            log.warning("content query refused by FTS5: %s", e)
            return []

    def passage(self, doc_id: int, query: str) -> str | None:
        """A passage of at most SNIPPET_MAX characters around the first match."""
        r = self.db.one("SELECT text FROM search_docs WHERE id=?", (doc_id,))
        if not r or r["text"] is None:
            return None
        text = zlib.decompress(r["text"]).decode("utf-8", "replace")
        return passage(text, query, self.forms)


def passage(text: str, query: str, forms: Forms) -> str:
    low = unicodedata.normalize("NFKC", text).casefold()
    if len(low) != len(text):          # rare: positions in `low` don't map back
        text = low
    want = sorted({f for w in words(query) for f in forms.of(w)}, key=len, reverse=True)
    at = None
    for f in want:
        m = re.search(r"(?<![^\W_])" + re.escape(f) if not _CJK.match(f) else re.escape(f), low)
        if m and (at is None or m.start() < at):
            at = m.start()
    at = at or 0
    start = max(0, at - SNIPPET_MAX // 3)
    if start:
        sp = text.find(" ", start, at)
        start = sp + 1 if sp != -1 else start
    out = " ".join(text[start:start + SNIPPET_MAX * 2].split())
    lead = "…" if start else ""
    if len(lead) + len(out) > SNIPPET_MAX:
        out = out[:SNIPPET_MAX - len(lead) - 1]
        cut = out.rfind(" ")
        out = (out[:cut] if cut > SNIPPET_MAX // 2 else out) + "…"
    return lead + out


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"c{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        if raw.startswith("c") and raw[1:].isdigit() and int(raw[1:]) < 1_000_000:
            return int(raw[1:])
    except (ValueError, UnicodeDecodeError):
        pass
    raise ValueError("bad cursor")


# ── keeping it in step (own NAS) ──────────────────────────────────────────

def decode_plain(data: bytes) -> str | None:
    """Text of a plain text file, or None when the bytes aren't text."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data[:8192]:
        return None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


RETRY_BASE_S = 60
MAX_ATTEMPTS = 3
BATCH = 50


class Indexer:
    """Background indexing of the NAS roots (own-NAS mode)."""

    def __init__(self, gw):
        self.gw = gw
        s = gw.s
        self.enabled = s.search_index
        self.forms = Forms(s.search_languages)
        self.index = ContentIndex(gw.db, self.forms, s.search_text_max_chars)
        self.max_bytes = s.search_max_file_mb << 20
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.db = gw.db
        self.db.conn.create_function("loft_indexable", 1, lambda n: int(indexable(n or "")),
                                     deterministic=True)

    # thread

    def start(self):
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="search-index")
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                busy = self.run_pending(budget_s=30)
            except Exception:  # noqa: BLE001 - logged; the next round retries
                log.exception("content indexing failed; retrying in a minute")
                busy = False
            if not busy:
                self._wake.wait(60)

    # work

    def forget(self, c, item_id: str):
        """In the transaction that marks the item deleted (trash, delete, replaced)."""
        if self.enabled:
            self.index.remove(c, item_id)

    def sweep(self) -> int:
        """Drop index entries whose item is gone or deleted. Returns how many."""
        rows = self.db.all("SELECT d.item_id FROM search_docs d LEFT JOIN items i"
                           " ON i.id = d.item_id WHERE i.id IS NULL OR i.deleted = 1"
                           " OR i.is_dir = 1 OR i.root_id != d.scope LIMIT 1000")
        with self.db.tx() as c:
            for r in rows:
                self.index.remove(c, r["item_id"])
        return len(rows)

    def pending(self, root_id: str, limit: int = BATCH) -> list:
        """Live documents of a root whose text isn't in the index as they are now."""
        return self.db.all(
            "SELECT i.* FROM items i LEFT JOIN search_docs d ON d.item_id = i.id"
            " WHERE i.root_id=? AND i.deleted=0 AND i.is_dir=0 AND loft_indexable(i.name)"
            " AND (d.id IS NULL OR d.content_version != i.content_version OR d.forms != ?)"
            " AND (d.retry_at IS NULL OR d.retry_at <= ?)"
            " ORDER BY i.mtime_ns DESC LIMIT ?",
            (root_id, self.forms.key, time.time(), limit))

    def caught_up(self, root_id: str) -> bool:
        """Every document of the root is in the index (a waiting retry counts as not)."""
        return not self.db.one(
            "SELECT 1 FROM items i LEFT JOIN search_docs d ON d.item_id = i.id"
            " WHERE i.root_id=? AND i.deleted=0 AND i.is_dir=0 AND loft_indexable(i.name)"
            " AND (d.id IS NULL OR d.content_version != i.content_version OR d.forms != ?)"
            " LIMIT 1", (root_id, self.forms.key))

    def run_pending(self, budget_s: float = 30, roots: list[str] | None = None) -> bool:
        """Index what is waiting, for up to budget_s. True when it stopped with work left."""
        if not self.enabled:
            return False
        self.sweep()
        deadline = time.monotonic() + budget_s
        q = "SELECT * FROM roots" + (" WHERE id IN (%s)" % ",".join("?" * len(roots))
                                     if roots else "")
        left = False
        for root in self.db.all(q, tuple(roots or ())):
            sess = self.gw.session_for_user(root["nas_user"])
            if sess is None:
                continue            # nobody of that account is connected: no way to read
            while True:
                rows = self.pending(root["id"])
                if not rows:
                    break
                for it in rows:
                    if time.monotonic() > deadline or self._stop.is_set():
                        return True
                    if not self.gw.session_live(sess):
                        sess = self.gw.session_for_user(root["nas_user"])
                        if sess is None:
                            break
                    self.index_item(root, it, sess)
                if sess is None:
                    left = True
                    break
        return left

    def index_item(self, root, it, sess) -> None:
        """Read one document as the root's account and put its text in the index."""
        from .rootfs import Blocked
        from .storage.base import AccessDenied, NotFound
        state, text = "indexed", None
        name, size = it["name"], it["size"]
        ext = name.rsplit(".", 1)[-1].lower()
        try:
            if size > self.max_bytes:
                state = "skipped"
            elif size == 0:
                state = "empty"
            else:
                fs = self.gw._fs(root, sess)
                share, path = fs.nas(self.gw._relpath(root, it))
                if ext in PLAIN_KINDS:
                    data = b"".join(sess.read(share, path, 0, min(size, PLAIN_READ_MAX)))
                    text = decode_plain(data)
                else:
                    text = self.gw.thumbs.text(
                        (it["id"], it["content_version"]), name, size,
                        lambda off, n: sess.read(share, path, off, n), cache=False)
                if text is None:
                    state = "empty"
        except (Blocked, AccessDenied, NotFound):
            state = "skipped"       # the next rescan sorts out what happened to it
        except Exception as e:  # noqa: BLE001 - NAS hiccup, worker busy, bad file: retried
            prev = self.db.one("SELECT attempts FROM search_docs WHERE item_id=?", (it["id"],))
            attempts = (prev["attempts"] if prev else 0) + 1
            log.info("indexing %s (attempt %d) failed: %s", it["id"][:8], attempts,
                     type(e).__name__)
            with self.db.tx() as c:
                if not self._still(c, it):
                    return
                if attempts >= MAX_ATTEMPTS:
                    self.index.put(c, it["id"], root["id"], it["content_version"], size,
                                   it["mtime_ns"], None, state="failed", attempts=attempts)
                else:
                    self.index.put(c, it["id"], root["id"], -1, size, it["mtime_ns"], None,
                                   state="retry", attempts=attempts,
                                   retry_at=time.time() + RETRY_BASE_S * 4 ** (attempts - 1))
            return
        with self.db.tx() as c:
            if self._still(c, it):
                self.index.put(c, it["id"], root["id"], it["content_version"], size,
                               it["mtime_ns"], text, state=state)

    @staticmethod
    def _still(c, it) -> bool:
        """The item is live and unchanged since it was read (else the next pass has it)."""
        r = c.execute("SELECT deleted, content_version FROM items WHERE id=?",
                      (it["id"],)).fetchone()
        return bool(r) and not r["deleted"] and r["content_version"] == it["content_version"]
