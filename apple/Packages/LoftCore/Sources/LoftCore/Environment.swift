// One wired-up client per process (host app and extension build the same thing).
// The pinned URLSession needs Security, so on Linux tests build GatewayClient by hand.
import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
#if canImport(Security)
import Security

/// Pins the gateway's key (docs/design.md §C) on every pinned listener of the pairing: the
/// home address, plus a public address in `direct` mode. Every other host gets the
/// system's normal WebPKI evaluation (a tunnel). A pin mismatch cancels the handshake,
/// which the client reads as "no answer" and moves to the internet route. Never "accept
/// any cert".
public final class PinnedSessionDelegate: NSObject, URLSessionDelegate, @unchecked Sendable {
    public struct Endpoint: Hashable, Sendable {
        public let host: String
        public let port: Int
        public init(_ u: URL) { host = (u.host ?? "").lowercased(); port = u.port ?? 443 }
        public init(host: String, port: Int) { self.host = host.lowercased(); self.port = port }
    }

    private var endpoints: Set<Endpoint>
    private var pins: Set<String>
    private let lock = NSLock()

    public init(endpoints: Set<Endpoint>, pins: Set<String>) {
        self.endpoints = endpoints
        self.pins = pins
    }

    /// The listeners whose key must match the pins.
    public static func endpoints(_ c: RouteConfig?) -> Set<Endpoint> {
        guard let c else { return [] }
        var e: Set<Endpoint> = [Endpoint(c.direct)]
        if c.publicIsPinned, let p = c.publicURL { e.insert(Endpoint(p)) }
        return e
    }

    public func setPins(_ p: Set<String>) { lock.withLock { pins = p } }

    public func configure(endpoints e: Set<Endpoint>, pins p: Set<String>) {
        lock.withLock { endpoints = e; pins = p }
    }

    public func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                           completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let space = challenge.protectionSpace
        let pinned = lock.withLock { endpoints.contains(Endpoint(host: space.host, port: space.port)) }
        guard space.authenticationMethod == NSURLAuthenticationMethodServerTrust, pinned else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        guard let trust = space.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first, let key = SecCertificateCopyKey(leaf),
              let raw = SecKeyCopyExternalRepresentation(key, nil) as Data?,
              let pin = SPKIPin.pin(ecPoint: raw),
              lock.withLock({ pins.contains(pin) }) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

/// One process's wiring: shared state, token store, route selector, pinned sessions and
/// the client. Built whether or not the device is paired; until it is, requests throw
/// GatewayError.notPaired. `pair(_:)` switches it to another gateway in place.
public final class LoftEnvironment: @unchecked Sendable {
    public let shared: SharedState
    public let tokens: TokenStore
    public let selector: RouteSelector
    public let pinning: PinnedSessionDelegate
    public let session: URLSession
    public let client: GatewayClient
    public let probeTransport: URLSessionTransport
    private let probeSession: URLSession

    /// `legacy`: what a device signed in with 0.1.x is migrated to (LegacyPairing).
    public init(shared: SharedState = SharedState(), tokens: TokenStore = KeychainTokenStore(),
                legacy: Pairing? = LegacyPairing.pairing) {
        self.shared = shared
        self.tokens = tokens
        shared.migrateLegacyPairing(signedIn: tokens.load() != nil, legacy: legacy)
        let cfg = Self.routeConfig(shared)
        selector = RouteSelector(config: cfg)
        pinning = PinnedSessionDelegate(endpoints: PinnedSessionDelegate.endpoints(cfg),
                                        pins: shared.effectivePins)

        let c = URLSessionConfiguration.ephemeral          // no cookie/cache on disk
        c.timeoutIntervalForRequest = 60
        c.timeoutIntervalForResource = 24 * 3600            // 5 GB over a slow uplink
        c.waitsForConnectivity = false
        c.httpMaximumConnectionsPerHost = 4
        c.requestCachePolicy = .reloadIgnoringLocalCacheData
        session = URLSession(configuration: c, delegate: pinning, delegateQueue: nil)

        let p = URLSessionConfiguration.ephemeral
        p.timeoutIntervalForRequest = RouteConfig.probeTimeout
        p.timeoutIntervalForResource = RouteConfig.probeTimeout * 2
        p.waitsForConnectivity = false
        probeSession = URLSession(configuration: p, delegate: pinning, delegateQueue: nil)
        probeTransport = URLSessionTransport(session: probeSession)

        client = GatewayClient(transport: URLSessionTransport(session: session), tokens: tokens,
                               selector: selector)
        let probeT = probeTransport, sel = selector
        client.probe = { await RouteSelector.probeDirect(probeT, sel.config) }
    }

    private static func routeConfig(_ shared: SharedState) -> RouteConfig? {
        guard var c = shared.pairing?.routeConfig else { return nil }
        c.pins = shared.effectivePins
        return c
    }

    /// The gateway this device is paired with.
    public var pairing: Pairing? { shared.pairing }
    public var isPaired: Bool { selector.config != nil }

    /// Pair with `p` (nil = unpair), replacing the gateway this process talks to. The
    /// caller signs out of the old gateway first. Open connections are dropped, so no
    /// connection checked against the old pins carries a request under the new pairing.
    public func pair(_ p: Pairing?) async {
        shared.setPairing(p)
        apply(Self.routeConfig(shared))
        await session.reset()
        await probeSession.reset()
    }

    // The extensions need no reload: a new pairing comes with a sign-out, which removes the
    // File Provider domain (and its extension process); the share extension starts fresh.
    private func apply(_ cfg: RouteConfig?) {
        pinning.configure(endpoints: PinnedSessionDelegate.endpoints(cfg), pins: shared.effectivePins)
        selector.configure(cfg)
    }

    /// Probe the direct route now (start, network change, every 5 min, sign-in sheet).
    @discardableResult
    public func reprobe() async -> Route {
        let r = await selector.reprobe { await RouteSelector.probeDirect(probeTransport, selector.config) }
        shared.route = r
        return r
    }

    /// Fetch the current pins (/v1/identity) and use them from now on (pin rotation).
    public func refreshPins() async {
        guard selector.config != nil, let id = try? await client.identity() else { return }
        let p = Set(id.directSpkiSha256.filter(Pairing.isPin))
        guard !p.isEmpty else { return }
        shared.directPins = p
        pinning.setPins(p)
    }
}
#endif
