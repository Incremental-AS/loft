// The device token: the only secret on the Mac (docs/design.md §E.3: never the password).
// Keychain item in the shared access group so the sandboxed extension reads what the
// host app wrote. Only the host app writes (sign-in, rotation, sign-out); the extension
// re-reads on a 401 in case the app rotated underneath it.
import Foundation
#if canImport(Security)
import Security
#endif

public struct StoredToken: Codable, Equatable, Sendable {
    public var token: String
    public var deviceId: String
    public var issuedAt: Date
    public var expiresAt: Date

    public init(token: String, deviceId: String, issuedAt: Date = Date(), expiresAt: Date) {
        self.token = token; self.deviceId = deviceId; self.issuedAt = issuedAt; self.expiresAt = expiresAt
    }

    public init(_ r: LoginResult, issuedAt: Date = Date()) {
        self.init(token: r.token, deviceId: r.deviceId, issuedAt: issuedAt,
                  expiresAt: Date(timeIntervalSince1970: r.expiresAt))
    }

    public var dueForRotation: Bool { Date().timeIntervalSince(issuedAt) > LoftConfig.rotateTokenAfter }
}

public protocol TokenStore: AnyObject, Sendable {
    func load() -> StoredToken?
    func save(_ t: StoredToken) throws
    func clear()
}

public final class MemoryTokenStore: TokenStore, @unchecked Sendable {
    private var t: StoredToken?
    private let lock = NSLock()
    public init(_ t: StoredToken? = nil) { self.t = t }
    public func load() -> StoredToken? { lock.lock(); defer { lock.unlock() }; return t }
    public func save(_ t: StoredToken) throws { lock.lock(); self.t = t; lock.unlock() }
    public func clear() { lock.lock(); t = nil; lock.unlock() }
}

#if canImport(Security)
public struct KeychainError: Error, CustomStringConvertible {
    public let status: OSStatus
    public var description: String {
        // -34018 errSecMissingEntitlement: keychain-access-groups not authorised by the
        // embedded provisioning profile (sign-notarize.sh embeds them).
        "keychain error \(status)" + (status == -34018 ? " (missing entitlement)" : "")
    }
}

public final class KeychainTokenStore: TokenStore, @unchecked Sendable {
    private let account = "device-token"

    public init() {}

    private var base: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: LoftConfig.keychainService,
         kSecAttrAccount as String: account,
         kSecAttrAccessGroup as String: LoftConfig.keychainGroup,
         kSecUseDataProtectionKeychain as String: true]
    }

    public func load() -> StoredToken? {
        var q = base
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return try? JSONDecoder().decode(StoredToken.self, from: data)
    }

    public func save(_ t: StoredToken) throws {
        let data = try JSONEncoder().encode(t)
        let upd = SecItemUpdate(base as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if upd == errSecSuccess { return }
        guard upd == errSecItemNotFound else { throw KeychainError(status: upd) }
        var add = base
        add[kSecValueData as String] = data
        // The extension runs when Finder asks, including behind a locked screen
        // after the first unlock; never synced to iCloud Keychain.
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let st = SecItemAdd(add as CFDictionary, nil)
        guard st == errSecSuccess else { throw KeychainError(status: st) }
    }

    public func clear() { SecItemDelete(base as CFDictionary) }
}
#endif
