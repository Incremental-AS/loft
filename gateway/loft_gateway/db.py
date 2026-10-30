"""SQLite index + device/audit store. One gateway process, one DB file, WAL.

Concurrency model: a single connection guarded by an RLock. Request handlers
run in a threadpool; every write goes through `tx()`, which is one
transaction. The data volume (one row per file the users can see) is far below
what needs anything heavier.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  nas_user TEXT NOT NULL,
  device_name TEXT NOT NULL,
  token_hash BLOB NOT NULL,
  wrapped_cred BLOB NOT NULL,
  prev_token_hash BLOB,
  prev_wrapped_cred BLOB,
  prev_valid_until REAL,
  root_id TEXT,
  created_at REAL NOT NULL,
  last_used_at REAL,
  last_ip TEXT,
  expires_at REAL NOT NULL,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS devices_user ON devices(nas_user);

-- Sign-in codes: the NAS password wrapped like a device's, under the code's
-- secret instead of a token secret (crypto.py). Revoking wipes the blob.
CREATE TABLE IF NOT EXISTS signin_codes (
  id TEXT PRIMARY KEY,
  nas_user TEXT NOT NULL,
  code_hash BLOB NOT NULL,
  wrapped_cred BLOB NOT NULL,
  created_by TEXT NOT NULL,      -- device id that minted it (direct route)
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  max_uses INTEGER NOT NULL,
  uses INTEGER NOT NULL DEFAULT 0,
  revoked_at REAL
);

CREATE TABLE IF NOT EXISTS roots (
  id TEXT PRIMARY KEY,
  nas_user TEXT NOT NULL,
  share TEXT NOT NULL,
  path TEXT NOT NULL,            -- share-relative, '' = share root
  epoch TEXT NOT NULL,           -- changes when the index is rebuilt -> anchors expire
  min_seq INTEGER NOT NULL DEFAULT 0,
  last_scan_at REAL,
  created_at REAL NOT NULL,
  UNIQUE (nas_user, share, path)
);

CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  root_id TEXT NOT NULL,
  parent_id TEXT,                -- NULL only for the root container
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,        -- NFC + casefold: SMB and APFS are case-insensitive
  is_dir INTEGER NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  file_id INTEGER,               -- server inode (FileInternalInformation), a move hint only
  content_version INTEGER NOT NULL DEFAULT 1,
  meta_version INTEGER NOT NULL DEFAULT 1,
  sha256 TEXT,
  deleted INTEGER NOT NULL DEFAULT 0,
  superseded_by TEXT,
  updated_seq INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS items_live_name
  ON items(root_id, parent_id, name_key) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS items_parent ON items(parent_id) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS items_fileid ON items(root_id, file_id);

CREATE TABLE IF NOT EXISTS changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  root_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('upsert', 'delete')),
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS changes_root ON changes(root_id, seq);

CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  root_id TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  received INTEGER NOT NULL DEFAULT 0,
  temp_path TEXT NOT NULL,       -- share-relative, under .loft/uploads/
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  root_id TEXT NOT NULL,
  content_version INTEGER NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  path TEXT NOT NULL,            -- share-relative, under .loft/versions/
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS versions_item ON versions(item_id, created_at);

CREATE TABLE IF NOT EXISTS trash (
  id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  root_id TEXT NOT NULL,
  parent_id TEXT,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,
  is_dir INTEGER NOT NULL,
  content_version INTEGER NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  path TEXT NOT NULL,            -- share-relative, under .loft/trash/
  deleted_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS trash_name ON trash(root_id, parent_id, name_key, deleted_at);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  nas_user TEXT,
  device_id TEXT,
  ip TEXT,
  route TEXT,
  event TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS login_failures (
  k TEXT NOT NULL,               -- 'u:<user>' or 'ip:<addr>'
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_k ON login_failures(k, at);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._depth = 0
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                              (str(SCHEMA_VERSION),))

    @contextmanager
    def tx(self):
        """One transaction. Nested calls join the outer one."""
        with self.lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self.conn
                finally:
                    self._depth -= 1
                return
            self._depth = 1
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    def one(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def audit(self, event: str, *, nas_user=None, device_id=None, ip=None, route=None,
              **detail):
        with self.tx() as c:
            c.execute("INSERT INTO audit (at, nas_user, device_id, ip, route, event, detail)"
                      " VALUES (?,?,?,?,?,?,?)",
                      (time.time(), nas_user, device_id, ip, route, event,
                       json.dumps(detail, ensure_ascii=False) if detail else None))
