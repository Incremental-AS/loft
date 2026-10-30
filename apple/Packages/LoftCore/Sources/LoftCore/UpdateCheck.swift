// The Mac app's update check (docs/releases-json.md). The Mac DMG is not in the App Store, so
// like the Android, Windows and Linux apps it asks loft.incremental.no/releases.json for a
// newer version, at most once a day. It is the only request Loft makes to Incremental: a
// plain GET with a User-Agent and nothing else (no ID, no account, no gateway, no cookies).
// The iPhone app never checks: the App Store updates it.
// Foundation-only: parsing, version compare and the daily limit are tested on Linux.
import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// One platform's entry in releases.json.
public struct AvailableRelease: Codable, Equatable, Sendable {
    public var version: String
    public var url: URL
    public var sha256: String?
    public var notes: String?

    public init(version: String, url: URL, sha256: String? = nil, notes: String? = nil) {
        self.version = version; self.url = url; self.sha256 = sha256; self.notes = notes
    }
}

public enum UpdateCheck {
    public static let releasesURL = URL(string: "https://loft.incremental.no/releases.json")!
    public static let interval: TimeInterval = 24 * 3600
    public static let platform = "macos"
    public static let userAgent = "Loft (macOS)"
    static let maxBody = 1 << 20

    public enum ParseError: Error, Equatable { case notJSON, notAnObject }

    /// The `platform` entry: the flat form `{"macos": {…}}` (what the site publishes) or
    /// `{"platforms": {"macos": {…}}}`. nil when there is none (left out, `null`), or when it
    /// has no version or a url that isn't https (docs/releases-json.md: refuse it). Unknown
    /// keys are ignored. Throws only if the document isn't a JSON object.
    public static func parse(_ data: Data, platform: String = platform) throws -> AvailableRelease? {
        let any: Any
        do { any = try JSONSerialization.jsonObject(with: data) } catch { throw ParseError.notJSON }
        guard let doc = any as? [String: Any] else { throw ParseError.notAnObject }
        let entry = doc[platform] as? [String: Any]
            ?? (doc["platforms"] as? [String: Any])?[platform] as? [String: Any]
        guard let e = entry,
              let v = (e["version"] as? String)?.trimmingCharacters(in: .whitespaces), isVersion(v),
              let s = e["url"] as? String, let u = URL(string: s),
              u.scheme?.lowercased() == "https", u.host?.isEmpty == false else { return nil }
        return AvailableRelease(version: v, url: u, sha256: e["sha256"] as? String,
                                notes: e["notes"] as? String)
    }

    /// "0.2.1", "v0.3", "1.0.0-rc1": dotted numbers, an optional v, an optional suffix.
    static func isVersion(_ v: String) -> Bool {
        let (nums, _) = split(v)
        return !nums.isEmpty && nums.allSatisfy { $0 != nil }
    }

    /// `a` is newer than `b`: numeric per part ("0.10.0" > "0.9.3", "0.2" == "0.2.0"); a
    /// pre-release suffix (-rc1) is older than the same plain version. Same rule as the
    /// Linux app (linux/internal/update).
    public static func isNewer(_ a: String, than b: String) -> Bool {
        let (pa, sa) = split(a), (pb, sb) = split(b)
        for i in 0..<max(pa.count, pb.count) {
            let x = i < pa.count ? (pa[i] ?? 0) : 0
            let y = i < pb.count ? (pb[i] ?? 0) : 0
            if x != y { return x > y }
        }
        return sa.isEmpty && !sb.isEmpty
    }

    private static func split(_ v: String) -> ([Int?], String) {
        var s = Substring(v.trimmingCharacters(in: .whitespaces))
        if s.first == "v" || s.first == "V" { s = s.dropFirst() }
        var suffix = ""
        if let i = s.firstIndex(where: { $0 == "-" || $0 == "+" }) {
            suffix = String(s[i...]); s = s[..<i]
        }
        guard !s.isEmpty else { return ([], suffix) }
        return (s.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }, suffix)
    }

    /// At most once a day. A clock that went back (last check in the future) counts as due,
    /// or a wrong date could stop the check for good.
    public static func isDue(lastCheck: Date?, now: Date) -> Bool {
        guard let last = lastCheck else { return true }
        return now.timeIntervalSince(last) >= interval || last > now
    }

    /// The request, and all of it: GET, a User-Agent, no cookies, no cache. The HTTP stack adds
    /// only its transport headers (Host, Accept, Accept-Encoding, Connection).
    public static func request(_ url: URL = releasesURL) -> URLRequest {
        var r = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        r.httpMethod = "GET"
        r.httpShouldHandleCookies = false
        r.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        // The system would add the user's own languages (nb-NO,nb;q=0.9). A fixed value says nothing.
        r.setValue("*", forHTTPHeaderField: "Accept-Language")
        return r
    }

    /// A session for the check alone: nothing stored, no cookies, no extra headers.
    public static func makeSession() -> URLSession {
        let c = URLSessionConfiguration.ephemeral
        c.httpCookieAcceptPolicy = .never
        c.httpShouldSetCookies = false
        c.urlCache = nil
        c.httpAdditionalHeaders = [:]
        c.timeoutIntervalForRequest = 15
        c.timeoutIntervalForResource = 30
        #if canImport(Darwin)
        c.waitsForConnectivity = false
        #endif
        return URLSession(configuration: c)
    }
}

/// Runs the check against stored state. Everything from outside is injected (URL, transport,
/// clock, defaults), so tests never reach the network or wait a day.
public final class UpdateChecker: @unchecked Sendable {
    public let url: URL
    public let currentVersion: String
    private let transport: HTTPTransport
    private let defaults: UserDefaults
    private let now: @Sendable () -> Date
    private let lock = NSLock()
    private var running = false

    enum K {
        static let enabled = "updateCheckEnabled", checkedAt = "updateCheckedAt"
        static let latest = "updateLatest", error = "updateLastError"
    }

    public init(currentVersion: String, url: URL = UpdateCheck.releasesURL,
                transport: HTTPTransport = URLSessionTransport(session: UpdateCheck.makeSession()),
                defaults: UserDefaults = .standard, now: @escaping @Sendable () -> Date = { Date() }) {
        self.currentVersion = currentVersion; self.url = url; self.transport = transport
        self.defaults = defaults; self.now = now
    }

    /// Settings › General. On unless switched off.
    public var isEnabled: Bool {
        get { defaults.object(forKey: K.enabled) as? Bool ?? true }
        set {
            defaults.set(newValue, forKey: K.enabled)
            if !newValue { defaults.removeObject(forKey: K.latest) }
        }
    }

    public var lastCheck: Date? { defaults.object(forKey: K.checkedAt) as? Date }
    /// Why the last attempt failed (logs only).
    public var lastError: String? { defaults.string(forKey: K.error) }

    /// What the last successful check found for this platform (any version).
    public var latest: AvailableRelease? {
        defaults.data(forKey: K.latest).flatMap { try? JSONDecoder().decode(AvailableRelease.self, from: $0) }
    }

    /// The release to offer: newer than this app, and the check is on.
    public var available: AvailableRelease? {
        guard isEnabled, let l = latest, UpdateCheck.isNewer(l.version, than: currentVersion) else { return nil }
        return l
    }

    public var isDue: Bool { isEnabled && UpdateCheck.isDue(lastCheck: lastCheck, now: now()) }

    /// Fetches releases.json if the check is on and due, then returns what to offer. The
    /// attempt's time is saved even when it fails, so a bad network or a broken file never
    /// turns into more than one request a day.
    @discardableResult
    public func checkIfDue() async -> AvailableRelease? {
        guard isDue else { return available }
        lock.lock()
        let start = !running
        running = true
        lock.unlock()
        guard start else { return available }
        defer { lock.lock(); running = false; lock.unlock() }
        defaults.set(now(), forKey: K.checkedAt)
        do {
            let (data, resp) = try await transport.send(UpdateCheck.request(url))
            guard resp.statusCode == 200 else { throw GatewayError.badResponse(resp.statusCode, "releases.json") }
            guard data.count <= UpdateCheck.maxBody else { throw GatewayError.badResponse(200, "releases.json too big") }
            if let r = try UpdateCheck.parse(data) {
                defaults.set(try JSONEncoder().encode(r), forKey: K.latest)
            } else {
                defaults.removeObject(forKey: K.latest)        // no macos release (any more)
            }
            defaults.removeObject(forKey: K.error)
        } catch {
            defaults.set(String(describing: error), forKey: K.error)
        }
        return available
    }
}
