// Route selection (docs/design.md §C, §E.2). Pure logic; the host app feeds it
// NWPathMonitor events, and both processes call ensureFresh() before requests.
// The addresses come from the device's pairing (docs/pairing.md), never from the code.
import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum Route: String, Sendable { case direct, publicInternet }

public struct RouteConfig: Sendable {
    public let direct: URL          // https://<lan-address>:<port>, pinned key
    public let publicURL: URL?      // outside address; nil = home network only
    public let publicMode: PublicMode   // tunnel: WebPKI, tokens only; direct: pinned, sign-in ok
    public var pins: Set<String>    // base64 SPKI sha256: the pairing's, refreshed from /v1/identity
    public static let probeTimeout: TimeInterval = 1.5
    public var probeTimeout: TimeInterval { Self.probeTimeout }
    public let reprobeEvery: TimeInterval = 300

    public init(direct: URL, publicURL: URL?, publicMode: PublicMode = .tunnel, pins: Set<String>) {
        self.direct = direct; self.publicURL = publicURL; self.publicMode = publicMode; self.pins = pins
    }

    /// The public address reaches the pinned key (a port forward), so it is checked
    /// against the pins and may carry the password.
    public var publicIsPinned: Bool { publicURL != nil && publicMode == .direct }

    /// Where a request goes on `route`. With no public address there is only home.
    public func base(for route: Route) -> URL {
        route == .direct ? direct : (publicURL ?? direct)
    }

    /// Where the password goes on `route`: always a pinned listener. Away from home that is
    /// a `direct`-mode public address; otherwise the home address (pinned, so a stale
    /// "away" verdict costs a timeout, never the password). Never a tunnel.
    public func signInURL(for route: Route) -> URL {
        if route == .publicInternet, publicIsPinned, let p = publicURL { return p }
        return direct
    }

    /// Where pins are refreshed from: a tunnel's WebPKI route (works even if the gateway
    /// rotated to a key this device doesn't know yet), else the current pinned route.
    public func identityURL(for route: Route) -> URL {
        if let p = publicURL, publicMode == .tunnel { return p }
        return base(for: route)
    }
}

public final class RouteSelector: @unchecked Sendable {
    private var _current: Route = .publicInternet
    private var _config: RouteConfig?
    private var lastProbe: Date?
    private let lock = NSLock()

    public init(config: RouteConfig?) { _config = config }

    /// nil = the device isn't paired with a gateway.
    public var config: RouteConfig? { lock.withLock { _config } }

    /// A new pairing (or none): forget the route, probe again before the next request.
    public func configure(_ c: RouteConfig?) {
        lock.withLock { _config = c; _current = .publicInternet; lastProbe = nil }
    }

    public var current: Route { lock.withLock { _current } }

    /// nil when not paired.
    public var baseURL: URL? {
        lock.withLock { _config?.base(for: _current) }
    }

    /// `probe` = GET <direct>/healthz over a URLSession whose delegate enforces
    /// the SPKI pin (PinnedSessionDelegate) with probeTimeout. It must return
    /// true only for HTTP 200 with {"service":"loft","route":"direct"}.
    /// A pin mismatch is a failed handshake -> false -> internet route. Never
    /// "accept any cert".
    @discardableResult
    public func reprobe(_ probe: () async -> Bool) async -> Route {
        let ok = await probe()
        set(ok ? .direct : .publicInternet, probed: true)
        return current
    }

    /// Probe if the last one is older than reprobeEvery (or there was none).
    @discardableResult
    public func ensureFresh(_ probe: () async -> Bool) async -> Route {
        return isStale ? await reprobe(probe) : current
    }

    /// A request on the direct route got no answer: use the internet until the next probe.
    public func directFailed() { set(.publicInternet, probed: false) }

    /// There is somewhere else to go when the direct route stops answering.
    public var hasPublicRoute: Bool { config?.publicURL != nil }

    private var isStale: Bool {
        lock.withLock {
            guard let c = _config else { return false }
            return lastProbe.map { Date().timeIntervalSince($0) > c.reprobeEvery } ?? true
        }
    }

    private func set(_ r: Route, probed: Bool) {
        lock.withLock {
            _current = r
            if probed { lastProbe = Date() }
        }
    }

    /// Password sign-in is offered on the direct route, or anywhere when the public address
    /// is in `direct` mode (pinned, docs/pairing.md). Never through a tunnel.
    public var canPasswordSignIn: Bool {
        lock.withLock { _config.map { _current == .direct || $0.publicIsPinned } ?? false }
    }

    /// nil when not paired.
    public var signInURL: URL? { lock.withLock { _config?.signInURL(for: _current) } }

    public var identityURL: URL? { lock.withLock { _config?.identityURL(for: _current) } }

    /// The probe itself, over whatever transport the caller built (pinned on macOS).
    public static func probeDirect(_ transport: HTTPTransport, _ config: RouteConfig?) async -> Bool {
        guard let config else { return false }
        var r = URLRequest(url: config.direct.appendingPathComponent("healthz"))
        r.timeoutInterval = config.probeTimeout
        guard let (data, resp) = try? await transport.send(r), resp.statusCode == 200,
              let h = try? JSONDecoder().decode(Health.self, from: data) else { return false }
        return h.service == "loft" && h.route == "direct"
    }
}
