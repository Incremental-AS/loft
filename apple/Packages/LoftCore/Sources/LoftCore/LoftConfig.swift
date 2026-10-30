// Fixed identifiers, and the state the host app and the File Provider
// extension share through the app group. Foundation-only.
import Foundation

public enum LoftConfig {
    /// App group shared by the app and its extensions. macOS: the team-prefixed form, which
    /// is also the keychain access group. iOS only accepts group.* ids (iPhone app, 2026-10-30).
    /// Must match the .entitlements files and NSExtensionFileProviderDocumentGroup in project.yml.
    #if os(iOS)
    public static let appGroup = "group.no.incremental.loft"
    #else
    public static let appGroup = "5A3JX4D37B.no.incremental.loft"
    #endif
    public static let keychainGroup = "5A3JX4D37B.no.incremental.loft"  // gitleaks:allow (team id, public by design)
    public static let keychainService = "no.incremental.loft"

    // No gateway address or pin lives in the code (docs/pairing.md): the device is paired with
    // one (Pairing, stored in SharedState). 0.1.x installs: LegacyPairing.

    public static let rotateTokenAfter: TimeInterval = 7 * 86400   // gateway TTL is 90 days, sliding
    public static let longPollWait = 25                             // gateway caps at 25 s (Cloudflare: 100)
    public static let defaultEvictAfterMinutes = 10                 // docs/design.md §E.1
}

/// Small key/value state in the app group's UserDefaults. The host app writes the
/// account/root/settings; the extension writes the upload journal and transfer count.
public final class SharedState: @unchecked Sendable {
    public let defaults: UserDefaults
    private let lock = NSLock()

    public init(suiteName: String = LoftConfig.appGroup) {
        defaults = UserDefaults(suiteName: suiteName) ?? .standard
    }

    private enum K {
        static let nasUser = "nasUser", deviceId = "deviceId", deviceName = "deviceName"
        static let root = "root", domainId = "domainIdentifier", paused = "paused"
        static let evictAfter = "evictAfterMinutes", pins = "directPins"
        static let lastSynced = "lastSynced", transfers = "activeTransfers"
        static let journal = "uploadedJournal", resume = "uploadResume"
        static let route = "route", unreachableReported = "unreachableReported"
        static let pairing = "pairing"
    }

    // MARK: pairing (host app writes; the extensions read)

    /// The gateway this device is paired with (one per device in 0.2), kept as its
    /// payload and re-validated on every read. Nothing secret: address + public-key pins.
    public var pairing: Pairing? {
        get { defaults.string(forKey: K.pairing).flatMap { try? Pairing.decode(payload: $0) } }
    }

    /// Pair with `p`, replacing any earlier gateway: its refreshed pins and last route go
    /// too. The caller signs out first (the token belongs to the old gateway).
    public func setPairing(_ p: Pairing?) {
        lock.lock(); defer { lock.unlock() }
        defaults.removeObject(forKey: K.pins)
        defaults.removeObject(forKey: K.route)
        if let p { defaults.set(p.payload, forKey: K.pairing) } else { defaults.removeObject(forKey: K.pairing) }
    }

    /// 0.1.x -> 0.2: a device that was signed in (it has a token) but has no pairing yet
    /// gets the build's legacy pairing, keeping the pins it already refreshed from
    /// /v1/identity. Returns true if it migrated. Idempotent; safe from any process.
    @discardableResult
    public func migrateLegacyPairing(signedIn: Bool, legacy: Pairing? = LegacyPairing.pairing) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard signedIn, defaults.string(forKey: K.pairing) == nil, let legacy else { return false }
        defaults.set(legacy.payload, forKey: K.pairing)
        return true
    }

    // MARK: account + root (host app writes)

    public var nasUser: String? {
        get { defaults.string(forKey: K.nasUser) }
        set { defaults.set(newValue, forKey: K.nasUser) }
    }
    public var deviceId: String? {
        get { defaults.string(forKey: K.deviceId) }
        set { defaults.set(newValue, forKey: K.deviceId) }
    }
    public var deviceName: String? {
        get { defaults.string(forKey: K.deviceName) }
        set { defaults.set(newValue, forKey: K.deviceName) }
    }
    public var root: RootInfo? {
        get { defaults.data(forKey: K.root).flatMap { try? JSONDecoder().decode(RootInfo.self, from: $0) } }
        set { defaults.set(newValue.flatMap { try? JSONEncoder().encode($0) }, forKey: K.root) }
    }
    public var domainIdentifier: String? {
        get { defaults.string(forKey: K.domainId) }
        set { defaults.set(newValue, forKey: K.domainId) }
    }
    public var paused: Bool {
        get { defaults.bool(forKey: K.paused) }
        set { defaults.set(newValue, forKey: K.paused) }
    }
    public var evictAfterMinutes: Int {
        get { (defaults.object(forKey: K.evictAfter) as? Int) ?? LoftConfig.defaultEvictAfterMinutes }
        set { defaults.set(newValue, forKey: K.evictAfter) }
    }
    /// Pins last fetched from /v1/identity; empty = use the pairing's.
    public var directPins: Set<String> {
        get { Set(defaults.stringArray(forKey: K.pins) ?? []) }
        set { defaults.set(Array(newValue).sorted(), forKey: K.pins) }
    }
    public var effectivePins: Set<String> {
        let p = directPins
        return p.isEmpty ? Set(pairing?.pins ?? []) : p
    }
    public var lastSynced: Date? {
        get { defaults.object(forKey: K.lastSynced) as? Date }
        set { defaults.set(newValue, forKey: K.lastSynced) }
    }
    public var route: Route {
        get { Route(rawValue: defaults.string(forKey: K.route) ?? "") ?? .publicInternet }
        set { defaults.set(newValue.rawValue, forKey: K.route) }
    }

    /// Everything account-bound goes on sign-out; settings (evict-after, pairing, pins) stay.
    public func clearAccount() {
        for k in [K.nasUser, K.deviceId, K.deviceName, K.root, K.domainId, K.paused,
                  K.journal, K.resume, K.transfers, K.lastSynced] {
            defaults.removeObject(forKey: k)
        }
    }

    // MARK: extension -> app

    /// Uploads + downloads in flight in the extension ("Syncing N files").
    /// The extension told the system "server unreachable" (Finder then shows "connection
    /// error" and backs off until signalErrorResolved). Only the app learns when home is
    /// back, and its own long-poll often never failed, so the extension leaves this note.
    public func noteUnreachableReported(at t: Date = Date()) {
        defaults.set(t.timeIntervalSince1970, forKey: K.unreachableReported)
    }
    /// When the extension last reported it (0 = never). A timestamp, not a flag the app
    /// clears: the two are separate processes, and a clear could swallow a newer note.
    public var unreachableReportedAt: Double { defaults.double(forKey: K.unreachableReported) }

    public var activeTransfers: Int { defaults.integer(forKey: K.transfers) }

    public func transferStarted() { bumpTransfers(+1) }
    public func transferEnded() { bumpTransfers(-1) }
    private func bumpTransfers(_ d: Int) {
        lock.lock(); defer { lock.unlock() }
        defaults.set(max(0, defaults.integer(forKey: K.transfers) + d), forKey: K.transfers)
    }
    /// The extension resets this when it starts: a killed extension must not leave
    /// the menu spinning forever.
    public func resetTransfers() { defaults.set(0, forKey: K.transfers) }

    /// Items whose upload the gateway acknowledged, with the time it did. The host app
    /// evicts them after evict_after (docs/design.md §E.1). Nothing else is ever evicted by us.
    public func journalUploaded(_ itemId: String, at: Date = Date()) {
        lock.lock(); defer { lock.unlock() }
        var j = defaults.dictionary(forKey: K.journal) as? [String: Double] ?? [:]
        j[itemId] = at.timeIntervalSince1970
        defaults.set(j, forKey: K.journal)
    }
    public func uploadedJournal() -> [String: Date] {
        let j = defaults.dictionary(forKey: K.journal) as? [String: Double] ?? [:]
        return j.mapValues { Date(timeIntervalSince1970: $0) }
    }
    public func removeFromJournal(_ itemId: String) {
        lock.lock(); defer { lock.unlock() }
        var j = defaults.dictionary(forKey: K.journal) as? [String: Double] ?? [:]
        j.removeValue(forKey: itemId)
        defaults.set(j, forKey: K.journal)
    }

    /// Unfinished upload sessions by content ("<sha256>:<size>"), so a retried save after
    /// the extension was killed continues from the gateway's `received` instead of zero.
    /// Content-keyed is safe: the gateway checks the whole-file hash at commit.
    public func resumableUpload(sha256: String, size: Int64) -> String? {
        (defaults.dictionary(forKey: K.resume) as? [String: String])?["\(sha256):\(size)"]
    }
    public func setResumableUpload(_ id: String?, sha256: String, size: Int64) {
        lock.lock(); defer { lock.unlock() }
        var r = defaults.dictionary(forKey: K.resume) as? [String: String] ?? [:]
        r["\(sha256):\(size)"] = id
        defaults.set(r, forKey: K.resume)
    }
}
