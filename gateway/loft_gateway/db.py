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

SCHEMA_VERSION = 3

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
  revoked_at REAL,
  kind TEXT NOT NULL DEFAULT 'app',   -- 'app' (Loft apps, REST API) or 'mcp' (an assistant, /mcp only)
  read_only INTEGER NOT NULL DEFAULT 0,
  -- An assistant signed in with OAuth (oauth.py): the newest access token (the columns
  -- above), earlier ones still within their 2 hours (oauth_access), and one refresh
  -- token that doesn't rotate, each with its own wrapped credential.
  client_id TEXT,
  refresh_hash BLOB,
  refresh_wrapped BLOB,
  refresh_expires_at REAL
);
CREATE INDEX IF NOT EXISTS devices_user ON devices(nas_user);

-- Access tokens of an OAuth connection that a later refresh replaced on the device row
-- but that are still within their own 2 hours: an assistant on two devices shares one
-- connection, and each device refreshes by itself (docs/mcp.md).
CREATE TABLE IF NOT EXISTS oauth_access (
  device_id TEXT NOT NULL REFERENCES devices(id),
  token_hash BLOB NOT NULL,
  wrapped_cred BLOB NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_access_device ON oauth_access(device_id);

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
  revoked_at REAL,
  purpose TEXT NOT NULL DEFAULT 'device'   -- 'device' (signs in an app) or 'assistant' (approves an OAuth sign-in)
);

-- OAuth for assistants (oauth.py). Clients register themselves (RFC 7591).
CREATE TABLE IF NOT EXISTS oauth_clients (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  redirect_uris TEXT NOT NULL,   -- JSON list, matched exactly
  created_at REAL NOT NULL,
  created_ip TEXT,
  used_at REAL                   -- last sign-in; never-used clients are pruned
);
-- A sign-in page that is open in a browser: binds the form to what the client asked for.
CREATE TABLE IF NOT EXISTS oauth_requests (
  id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  state TEXT,
  code_challenge TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  created_ip TEXT                -- pages open per IP are limited
);
-- An authorization code: the NAS password wrapped under the code's secret until the
-- client trades it (with its PKCE verifier) for tokens, once, within minutes.
CREATE TABLE IF NOT EXISTS oauth_codes (
  id TEXT PRIMARY KEY,
  code_hash BLOB NOT NULL,
  client_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  nas_user TEXT NOT NULL,
  wrapped_cred BLOB NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  used_at REAL
);

-- What an assistant connection may open (mcp.py). path is "share/folder" from the
-- top of the user's drive ('' = all of it). access 'write' = the working folder
-- (read and write; one per context), 'read' = readable. context '' applies to every
-- call; another value only to calls that name it (an assistant's project or thread).
-- Folders the user has approved for an assistant connection (schema 3), 'read' or
-- 'write' (read and save; covers read). A grant (mcp_grants) that none covers needs the
-- user's approval first: the setup right after sign-in, or a fresh approval code (mcp.py,
-- m_access_set). Narrowing a grant doesn't take an approval back; signing the
-- connection out does.
CREATE TABLE IF NOT EXISTS mcp_approved (
  device_id TEXT NOT NULL,
  path TEXT NOT NULL,
  access TEXT NOT NULL DEFAULT 'read',
  created_at REAL NOT NULL,
  PRIMARY KEY (device_id, path)
);

CREATE TABLE IF NOT EXISTS mcp_grants (
  device_id TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '',
  access TEXT NOT NULL,
  path TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (device_id, context, access, path)
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

-- Owner SID of each share's root folder as each account first saw it (SMB only).
-- A different owner later = the NAS is serving another folder under that name.
CREATE TABLE IF NOT EXISTS share_owners (
  nas_user TEXT NOT NULL,        -- casefolded
  share TEXT NOT NULL,           -- casefolded
  owner_sid TEXT NOT NULL,
  first_seen REAL NOT NULL,
  PRIMARY KEY (nas_user, share)
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
            self._migrate()
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                              (str(SCHEMA_VERSION),))

    def _migrate(self):
        """Columns added after a table first shipped (CREATE TABLE IF NOT EXISTS
        leaves an existing table as it was)."""
        added = {                                                  # schema 2: assistants (MCP)
            "devices": [("kind", "TEXT NOT NULL DEFAULT 'app'"),
                        ("read_only", "INTEGER NOT NULL DEFAULT 0"),
                        ("client_id", "TEXT"), ("refresh_hash", "BLOB"),
                        ("refresh_wrapped", "BLOB"), ("refresh_expires_at", "REAL")],
            "signin_codes": [("purpose", "TEXT NOT NULL DEFAULT 'device'")],
            "oauth_requests": [("created_ip", "TEXT")],
            # a schema 3 table made by 0bca895, before approvals kept read/write apart
            "mcp_approved": [("access", "TEXT NOT NULL DEFAULT 'read'")],
        }
        v = self.conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        if v and int(v["v"]) < 3:
            # schema 3: approvals. What a connection had before counts as approved.
            # A working folder's grant is a write approval ('write' > 'read').
            self.conn.execute("INSERT OR IGNORE INTO mcp_approved (device_id, path, access,"
                              " created_at) SELECT device_id, path, MAX(access),"
                              " MIN(created_at) FROM mcp_grants GROUP BY device_id, path")
        for table, cols in added.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.execute("UPDATE meta SET v=? WHERE k='schema_version' AND CAST(v AS INTEGER)<?",
                          (str(SCHEMA_VERSION), SCHEMA_VERSION))

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
