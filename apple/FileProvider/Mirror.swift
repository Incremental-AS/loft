// What this extension has told the system exists: id, parent, case-folded name,
// content etag. Only used for the Office safe-save shape "createItem(N) before
// deleteItem(X)" (docs/design.md §D.3): the system cannot hold two items with one name
// in a folder, so a create whose (parent, name) matches a live item WE reported means
// "this replaces X" -> commit with replaces=X, if_match=X's etag. A same-name item the
// system never saw is a real collision and must become a conflict copy instead, which
// is why this can't be answered by asking the gateway. SQLite in the app group
// container, so it survives the extension being stopped between Office's steps.
import Foundation
import SQLite3
import LoftCore

final class Mirror: @unchecked Sendable {
    private var db: OpaquePointer?
    private let lock = NSLock()
    private static let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    init?(domainId: String) {
        guard let dir = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier: LoftConfig.appGroup) else { return nil }
        let safe = domainId.filter { $0.isLetter || $0.isNumber || $0 == "-" }
        let url = dir.appendingPathComponent("mirror-\(safe).sqlite")
        guard sqlite3_open_v2(url.path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
                              nil) == SQLITE_OK else { return nil }
        exec("PRAGMA journal_mode=WAL")
        exec("""
            CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, parent TEXT, name_key TEXT,
              is_dir INTEGER, content_etag TEXT, deleted INTEGER NOT NULL DEFAULT 0)
            """)
        exec("CREATE INDEX IF NOT EXISTS by_name ON items (parent, name_key)")
    }

    deinit { sqlite3_close(db) }

    static func nameKey(_ name: String) -> String {
        name.precomposedStringWithCanonicalMapping.folding(options: [.caseInsensitive], locale: nil)
    }

    private static func text(_ st: OpaquePointer?, _ i: Int32) -> String {
        sqlite3_column_text(st, i).map { String(cString: $0) } ?? ""
    }

    private func exec(_ sql: String) { sqlite3_exec(db, sql, nil, nil, nil) }

    func upsert(_ items: [Item]) {
        guard !items.isEmpty else { return }
        lock.lock(); defer { lock.unlock() }
        exec("BEGIN")
        var st: OpaquePointer?
        sqlite3_prepare_v2(db, """
            INSERT INTO items (id, parent, name_key, is_dir, content_etag, deleted) VALUES (?,?,?,?,?,0)
            ON CONFLICT(id) DO UPDATE SET parent=excluded.parent, name_key=excluded.name_key,
              is_dir=excluded.is_dir, content_etag=excluded.content_etag, deleted=0
            """, -1, &st, nil)
        for it in items {
            sqlite3_reset(st)
            sqlite3_bind_text(st, 1, it.id, -1, Mirror.transient)
            sqlite3_bind_text(st, 2, it.parentId ?? "", -1, Mirror.transient)
            sqlite3_bind_text(st, 3, Mirror.nameKey(it.name), -1, Mirror.transient)
            sqlite3_bind_int(st, 4, it.isDirectory ? 1 : 0)
            sqlite3_bind_text(st, 5, it.contentEtag, -1, Mirror.transient)
            sqlite3_step(st)
        }
        sqlite3_finalize(st)
        exec("COMMIT")
    }

    func markDeleted(_ ids: [String]) {
        guard !ids.isEmpty else { return }
        lock.lock(); defer { lock.unlock() }
        var st: OpaquePointer?
        sqlite3_prepare_v2(db, "UPDATE items SET deleted=1 WHERE id=?", -1, &st, nil)
        for id in ids {
            sqlite3_reset(st)
            sqlite3_bind_text(st, 1, id, -1, Mirror.transient)
            sqlite3_step(st)
        }
        sqlite3_finalize(st)
    }

    struct Entry { let id: String; let isDir: Bool; let contentEtag: String }

    func live(parent: String, name: String) -> Entry? {
        lock.lock(); defer { lock.unlock() }
        var st: OpaquePointer?
        sqlite3_prepare_v2(db, "SELECT id, is_dir, content_etag FROM items WHERE parent=? AND name_key=?"
                               + " AND deleted=0 LIMIT 1", -1, &st, nil)
        defer { sqlite3_finalize(st) }
        sqlite3_bind_text(st, 1, parent, -1, Mirror.transient)
        sqlite3_bind_text(st, 2, Mirror.nameKey(name), -1, Mirror.transient)
        guard sqlite3_step(st) == SQLITE_ROW else { return nil }
        return Entry(id: Mirror.text(st, 0), isDir: sqlite3_column_int(st, 1) != 0,
                     contentEtag: Mirror.text(st, 2))
    }

    func isDirectory(_ id: String) -> Bool? {
        lock.lock(); defer { lock.unlock() }
        var st: OpaquePointer?
        sqlite3_prepare_v2(db, "SELECT is_dir FROM items WHERE id=?", -1, &st, nil)
        defer { sqlite3_finalize(st) }
        sqlite3_bind_text(st, 1, id, -1, Mirror.transient)
        guard sqlite3_step(st) == SQLITE_ROW else { return nil }
        return sqlite3_column_int(st, 0) != 0
    }
}
