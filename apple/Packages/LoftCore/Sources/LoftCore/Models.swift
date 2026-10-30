// Wire models for the gateway API (gateway/loft_gateway/api.py + service.py are the
// source of truth). Foundation-only: compiled and tested on Linux too.
import Foundation

public struct Item: Codable, Equatable, Sendable {
    public let id: String
    public let parentId: String?
    public let name: String
    public let type: String          // "file" | "dir"
    public let size: Int64
    public let mtimeNs: Int64
    public let contentEtag: String   // -> NSFileProviderItemVersion.contentVersion
    public let metaEtag: String      // -> NSFileProviderItemVersion.metadataVersion
    public let sha256: String?
    /// All-shares root: true on the root and on each share. Shares can't be renamed,
    /// moved or deleted, and the root holds nothing but shares.
    public let fixed: Bool?

    public var isDirectory: Bool { type == "dir" }
    public var isFixed: Bool { fixed ?? false }
    public var modified: Date { Date(timeIntervalSince1970: Double(mtimeNs) / 1e9) }

    public init(id: String, parentId: String?, name: String, type: String, size: Int64,
                mtimeNs: Int64, contentEtag: String, metaEtag: String, sha256: String?,
                fixed: Bool? = nil) {
        self.id = id; self.parentId = parentId; self.name = name; self.type = type
        self.size = size; self.mtimeNs = mtimeNs; self.contentEtag = contentEtag
        self.metaEtag = metaEtag; self.sha256 = sha256; self.fixed = fixed
    }

    enum CodingKeys: String, CodingKey {
        case id, name, type, size, sha256, fixed
        case parentId = "parent_id", mtimeNs = "mtime_ns"
        case contentEtag = "content_etag", metaEtag = "meta_etag"
    }
}

public struct ChildrenPage: Codable, Sendable {
    public let items: [Item]
    public let nextCursor: String?
    enum CodingKeys: String, CodingKey { case items; case nextCursor = "next_cursor" }
}

public struct ChangesPage: Codable, Sendable {
    public let updated: [Item]
    public let deleted: [String]
    public let anchor: String        // opaque "<epoch>.<seq>" -> NSFileProviderSyncAnchor
    public let more: Bool

    public var isEmpty: Bool { updated.isEmpty && deleted.isEmpty }
}

public struct AnchorResponse: Codable, Sendable { public let anchor: String }

public struct LoginResult: Codable, Sendable {
    public let deviceId: String
    public let token: String
    public let nasUser: String?
    public let expiresAt: Double
    enum CodingKeys: String, CodingKey {
        case token; case deviceId = "device_id"; case nasUser = "nas_user"
        case expiresAt = "expires_at"
    }
}

/// POST /v1/auth/refresh. Same shape as login minus nas_user.
public typealias RefreshResult = LoginResult

public struct RootInfo: Codable, Equatable, Sendable {
    public let id: String
    public let share: String
    public let path: String
    public let rootItemId: String
    public let anchor: String
    enum CodingKeys: String, CodingKey {
        case id, share, path, anchor; case rootItemId = "root_item_id"
    }

    /// "share/path" as the Settings › Folder line shows it.
    public var displayPath: String { path.isEmpty ? share : "\(share)/\(path)" }
    /// Every share the account can open, each a top-level folder (`PUT /v1/me/root {"share":"*"}`).
    public var isAllShares: Bool { share == RootInfo.allShares }
    public static let allShares = "*"
}

/// GET /v1/me (and the answer to PUT /v1/me/root).
public struct Me: Codable, Sendable {
    public let nasUser: String
    public let deviceId: String
    public let deviceName: String
    public let route: String
    public let root: RootInfo?
    enum CodingKeys: String, CodingKey {
        case route, root
        case nasUser = "nas_user", deviceId = "device_id", deviceName = "device_name"
    }
}

public struct Device: Codable, Sendable, Identifiable {
    public let id: String
    public let deviceName: String
    public let createdAt: Double
    public let lastUsedAt: Double?
    public let revokedAt: Double?
    enum CodingKeys: String, CodingKey {
        case id
        case deviceName = "device_name", createdAt = "created_at"
        case lastUsedAt = "last_used_at", revokedAt = "revoked_at"
    }
}

public struct DevicesResponse: Codable, Sendable {
    public let devices: [Device]
    public let current: String
}

/// GET /v1/roots without a share.
public struct SharesResponse: Codable, Sendable { public let shares: [String] }

/// GET /v1/roots?share=&path=
public struct FoldersResponse: Codable, Sendable {
    public let share: String
    public let path: String
    public let folders: [String]
}

public struct UploadState: Codable, Sendable {
    public let uploadId: String
    public let received: Int64
    public let size: Int64
    public let chunkMax: Int64?      // only on create
    enum CodingKeys: String, CodingKey {
        case received, size; case uploadId = "upload_id"; case chunkMax = "chunk_max"
    }
}

public enum CommitResult: String, Codable, Sendable {
    case created, modified, replaced, conflict
}

public struct CommitRequest: Codable, Sendable {
    public var parentId: String?
    public var name: String?
    public var itemId: String?       // modify existing content (modifyItem)
    public var ifMatch: String?      // content etag the edit is based on
    public var replaces: String?     // safe-save: the item this create supersedes

    public init(parentId: String? = nil, name: String? = nil, itemId: String? = nil,
                ifMatch: String? = nil, replaces: String? = nil) {
        self.parentId = parentId; self.name = name; self.itemId = itemId
        self.ifMatch = ifMatch; self.replaces = replaces
    }
    enum CodingKeys: String, CodingKey {
        case name, replaces
        case parentId = "parent_id", itemId = "item_id", ifMatch = "if_match"
    }
}

public struct CommitResponse: Codable, Sendable {
    public let result: CommitResult
    public let item: Item
    public let replacedId: String?
    enum CodingKeys: String, CodingKey { case result, item; case replacedId = "replaced_id" }
}

/// DELETE /v1/items/{id}: "trashed" | "superseded" | "already_deleted". All three are success.
public struct DeleteResponse: Codable, Sendable {
    public let result: String
    public let supersededBy: String?
    enum CodingKeys: String, CodingKey { case result; case supersededBy = "superseded_by" }
}

public struct Health: Codable, Sendable {
    public let service: String
    public let route: String
}

public struct Identity: Codable, Sendable {
    public let directSpkiSha256: [String]
    enum CodingKeys: String, CodingKey { case directSpkiSha256 = "direct_spki_sha256" }
}

/// Gateway error body: {"error": "<code>", ...extra}. `status` is the HTTP status.
public struct APIError: Error, Codable, Sendable, Equatable {
    public let error: String
    public var status: Int = 0
    public var received: Int64? = nil       // 409 wrong_offset / upload_incomplete
    public var existingId: String? = nil    // 409 name_exists
    public var current: Item? = nil         // 412 *_etag_mismatch
    enum CodingKeys: String, CodingKey {
        case error, received, current; case existingId = "existing_id"
    }

    /// Codes the File Provider layer maps to NSFileProviderError.
    public var isAnchorExpired: Bool { error == "anchor_expired" }          // .syncAnchorExpired
    public var needsSignIn: Bool { SyncError(syncLoopFailure: self)?.needsSignIn ?? false }  // .notAuthenticated
    /// The gateway answered but the NAS behind it did not. Not an error state: offline
    /// glyph and rules, status "Can't reach the NAS. Retrying…" (SyncStatus.cantReachNAS).
    public var isNASUnreachable: Bool { error == "nas_unavailable" || error == "nas_error" }
}

/// Why sync has stopped, as the menu shows it (words in Copy.swift).
/// Feed it failures of the sync loop only (changes long-poll, /v1/me, token refresh),
/// not per-item errors: a conflict or a missing file never stops sync.
public enum SyncError: Equatable, Sendable {
    case nasPasswordChanged        // 401 nas_credentials_invalid
    case signInExpired             // 401 token_expired / invalid_token
    case deviceRemoved             // 401 device_revoked (Settings on another Mac, or the admin CLI)
    case noAccess                  // 403 access_denied / 404 root_missing: the Loft folder is gone
                                   // or this account lost it. Sign-in won't fix it; another folder will.
    case stopped(code: String)     // anything else; the code is for the log, never the menu

    public var needsSignIn: Bool {
        switch self {
        case .nasPasswordChanged, .signInExpired, .deviceRemoved: true
        case .noAccess, .stopped: false
        }
    }

    /// nil = not an error state: the NAS behind the gateway is down (isNASUnreachable,
    /// "Can't reach the NAS") or the client recovers by itself (anchor_expired), so the
    /// loop keeps retrying. "Can't reach home" is for no answer from the gateway at all.
    public init?(syncLoopFailure e: APIError) {
        switch e.error {
        case "nas_credentials_invalid": self = .nasPasswordChanged
        // invalid_token: the gateway no longer knows this token at all (device row gone,
        // gateway rebuilt). Only a fresh sign-in fixes it, so it reads as an expiry.
        // token_superseded: refresh with a token that is no longer current; same fix.
        case "token_expired", "invalid_token", "token_superseded", "missing_token":
            self = .signInExpired
        case "device_revoked": self = .deviceRemoved
        case "access_denied", "root_missing": self = .noAccess
        case "nas_unavailable", "nas_error", "anchor_expired": return nil
        default: self = .stopped(code: e.error)
        }
    }
}
