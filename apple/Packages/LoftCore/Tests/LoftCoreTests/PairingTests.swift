import XCTest
@testable import LoftCore
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// docs/pairing.md, as the gateway's `loft-gateway pair` writes it (admin.encode_payload:
/// compact JSON, base64url without padding). The pins are throwaway test values.
final class PairingTests: XCTestCase {
    static let pinA = "IKvfsXGEqE/chNtQCiZAAd+gCKwQLWQN2XrXp9GWCMQ="
    static let pinB = "2cgYA3fUhyDBJkOgvM77KnM3sbij6S6NEZglL+RUX40="
    /// {"v":1,"name":"Hjemme på loftet","lan":"https://10.0.0.20:8443","pins":[pinA,pinB]}
    static let lanOnly = "eyJ2IjoxLCJuYW1lIjoiSGplbW1lIHDDpSBsb2Z0ZXQiLCJsYW4iOiJodHRwczovLzEwLjAuMC4yMDo4NDQzIiwicGlucyI6WyJJS3Zmc1hHRXFFL2NoTnRRQ2laQUFkK2dDS3dRTFdRTjJYclhwOUdXQ01RPSIsIjJjZ1lBM2ZVaHlEQkprT2d2TTc3S25NM3NiaWo2UzZORVpnbEwrUlVYNDA9Il19"
    /// {"v":1,"name":"Home","lan":"https://10.0.0.5:8443","pins":[pinA],
    ///  "pub":"https://files.example.org","mode":"direct"}
    static let withDirectPub = "eyJ2IjoxLCJuYW1lIjoiSG9tZSIsImxhbiI6Imh0dHBzOi8vMTAuMC4wLjU6ODQ0MyIsInBpbnMiOlsiSUt2ZnNYR0VxRS9jaE50UUNpWkFBZCtnQ0t3UUxXUU4yWHJYcDlHV0NNUT0iXSwicHViIjoiaHR0cHM6Ly9maWxlcy5leGFtcGxlLm9yZyIsIm1vZGUiOiJkaXJlY3QifQ"  // gitleaks:allow (test key)

    /// base64url of arbitrary JSON, for the refusal cases.
    private func code(_ json: String) -> String {
        Data(json.utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private func refuses(_ text: String, _ want: PairingError, line: UInt = #line) {
        XCTAssertThrowsError(try Pairing.parse(text), line: line) { e in
            XCTAssertEqual(e as? PairingError, want, line: line)
        }
    }

    // MARK: accepted forms

    func testGatewayLinkLANOnly() throws {
        let p = try Pairing.parse("https://loft.incremental.no/pair#" + Self.lanOnly)
        XCTAssertEqual(p.name, "Hjemme på loftet")
        XCTAssertEqual(p.lan.absoluteString, "https://10.0.0.20:8443")
        XCTAssertEqual(p.pins, [Self.pinA, Self.pinB])
        XCTAssertNil(p.publicURL)
        XCTAssertNil(p.publicMode)
        XCTAssertFalse(p.publicAllowsSignIn)
        XCTAssertEqual(p.lanHost, "10.0.0.20")
    }

    func testEveryLinkFormGivesTheSamePairing() throws {
        let want = try Pairing.decode(payload: Self.withDirectPub)
        for s in ["https://loft.incremental.no/pair#\(Self.withDirectPub)",
                  "loft://pair#\(Self.withDirectPub)",
                  "loft://pair?\(Self.withDirectPub)",
                  "loft://pair?p=\(Self.withDirectPub)",
                  "  \(Self.withDirectPub)\n",
                  Self.withDirectPub + "=="] {           // padded is fine too
            XCTAssertEqual(try Pairing.parse(s), want, s)
        }
        XCTAssertEqual(try Pairing.parse(url: URL(string: "loft://pair#\(Self.withDirectPub)")!), want)
        XCTAssertEqual(want.publicURL?.absoluteString, "https://files.example.org")
        XCTAssertEqual(want.publicMode, .direct)
        XCTAssertTrue(want.publicAllowsSignIn)
    }

    func testRoundTripAndUnknownKeysIgnored() throws {
        let p = try Pairing.decode(payload: Self.withDirectPub)
        XCTAssertEqual(try Pairing.decode(payload: p.payload), p)
        XCTAssertTrue(p.link.hasPrefix("https://loft.incremental.no/pair#"))
        XCTAssertFalse(p.payload.contains("="))
        let extra = code(#"{"v":1,"name":"N","lan":"https://h:1","pins":["\#(Self.pinA)"],"later":true}"#)
        XCTAssertEqual(try Pairing.parse(extra).name, "N")
    }

    func testTrailingSlashAndDuplicatePins() throws {
        let p = try Pairing.parse(code(#"{"v":1,"name":"N","lan":"https://h:8443/","pins":["\#(Self.pinA)","\#(Self.pinA)"]}"#))
        XCTAssertEqual(p.lan.absoluteString, "https://h:8443")
        XCTAssertEqual(p.pins, [Self.pinA])
    }

    func testIsPairingURL() {
        XCTAssertTrue(Pairing.isPairingURL(URL(string: "https://loft.incremental.no/pair#x")!))
        XCTAssertTrue(Pairing.isPairingURL(URL(string: "loft://pair#x")!))
        XCTAssertFalse(Pairing.isPairingURL(URL(string: "loft://stream?items=a")!))
        XCTAssertFalse(Pairing.isPairingURL(URL(string: "https://loft.incremental.no/download")!))
        XCTAssertFalse(Pairing.isPairingURL(URL(string: "https://example.org/pair#x")!))
    }

    // MARK: refused (docs/pairing.md: unknown v, non-https, no pins)

    func testRefusesUnknownVersion() {
        refuses(code(#"{"v":2,"name":"N","lan":"https://h:1","pins":["\#(Self.pinA)"]}"#), .unsupportedVersion(2))
        refuses(code(#"{"v":0}"#), .unsupportedVersion(0))
        refuses(code(#"{"name":"N","lan":"https://h:1","pins":["\#(Self.pinA)"]}"#), .missingField("v"))
        refuses(code(#"{"v":"1","name":"N","lan":"https://h:1","pins":["\#(Self.pinA)"]}"#), .missingField("v"))
    }

    func testRefusesNonHTTPS() {
        refuses(code(#"{"v":1,"name":"N","lan":"http://h:8443","pins":["\#(Self.pinA)"]}"#), .notHTTPS("lan"))
        refuses(code(#"{"v":1,"name":"N","lan":"h:8443","pins":["\#(Self.pinA)"]}"#), .notHTTPS("lan"))
        refuses(code(#"{"v":1,"name":"N","lan":"https://h:1","pins":["\#(Self.pinA)"],"pub":"http://x.org","mode":"tunnel"}"#),
                .notHTTPS("pub"))
        refuses(code(#"{"v":1,"name":"N","lan":"https://u:pw@h:1","pins":["\#(Self.pinA)"]}"#), .badURL("lan"))
    }

    func testRefusesMissingOrBadPins() {
        refuses(code(#"{"v":1,"name":"N","lan":"https://h:1"}"#), .noPins)
        refuses(code(#"{"v":1,"name":"N","lan":"https://h:1","pins":[]}"#), .noPins)
        refuses(code(#"{"v":1,"name":"N","lan":"https://h:1","pins":["standby="]}"#), .badPin("standby="))
    }

    func testRefusesPubWithoutAKnownMode() {
        refuses(code(#"{"v":1,"name":"N","lan":"https://h:1","pins":["\#(Self.pinA)"],"pub":"https://x.org"}"#), .badMode(""))
        refuses(code(#"{"v":1,"name":"N","lan":"https://h:1","pins":["\#(Self.pinA)"],"pub":"https://x.org","mode":"vpn"}"#),
                .badMode("vpn"))
    }

    func testRefusesJunk() {
        refuses("", .empty)
        refuses("https://loft.incremental.no/pair", .notAPairingLink)
        refuses("loft://stream?items=a", .notAPairingLink)
        refuses("not a code!", .badEncoding)
        refuses(code("[1,2]"), .badEncoding)
        refuses(code(#"{"v":1,"lan":"https://h:1","pins":["\#(Self.pinA)"]}"#), .missingField("name"))
        refuses(code(#"{"v":1,"name":"  ","lan":"https://h:1","pins":["\#(Self.pinA)"]}"#), .missingField("name"))
    }

    // MARK: Set up manually

    func testManual() throws {
        let p = try Pairing.manual(address: " 10.0.0.5 ", pins: "sha256//\(Self.pinA)\n\(Self.pinB)")
        XCTAssertEqual(p.lan.absoluteString, "https://10.0.0.5:8443")
        XCTAssertEqual(p.pins, [Self.pinA, Self.pinB])
        XCTAssertEqual(p.name, "10.0.0.5")
        XCTAssertNil(p.publicURL)
        XCTAssertEqual(try Pairing.manual(address: "https://nas.example:9443", pins: Self.pinA, name: "Loft").lan.absoluteString,
                       "https://nas.example:9443")
        XCTAssertThrowsError(try Pairing.manual(address: "http://10.0.0.5:8443", pins: Self.pinA)) {
            XCTAssertEqual($0 as? PairingError, .notHTTPS("address"))
        }
        XCTAssertThrowsError(try Pairing.manual(address: "10.0.0.5", pins: " ")) {
            XCTAssertEqual($0 as? PairingError, .noPins)
        }
        XCTAssertThrowsError(try Pairing.manual(address: "10.0.0.5", pins: "abc")) {
            XCTAssertEqual($0 as? PairingError, .badPin("abc"))
        }
        XCTAssertThrowsError(try Pairing.manual(address: "", pins: Self.pinA)) {
            XCTAssertEqual($0 as? PairingError, .missingField("address"))
        }
    }

    // MARK: routes from a pairing

    func testTunnelRoutes() throws {
        let p = try Pairing(name: "N", lan: URL(string: "https://10.0.0.5:8443")!, pins: [Self.pinA],
                            publicURL: URL(string: "https://files.example.org")!, publicMode: .tunnel)
        let c = p.routeConfig
        XCTAssertFalse(c.publicIsPinned)
        XCTAssertEqual(c.base(for: .publicInternet).absoluteString, "https://files.example.org")
        XCTAssertEqual(c.signInURL(for: .direct), p.lan)
        XCTAssertEqual(c.signInURL(for: .publicInternet), p.lan, "a tunnel never carries the password")
        XCTAssertEqual(c.identityURL(for: .direct).absoluteString, "https://files.example.org")
    }

    func testDirectPublicRoutes() throws {
        let c = try Pairing.decode(payload: Self.withDirectPub).routeConfig
        XCTAssertTrue(c.publicIsPinned)
        XCTAssertEqual(c.signInURL(for: .publicInternet).absoluteString, "https://files.example.org")
        XCTAssertEqual(c.signInURL(for: .direct).absoluteString, "https://10.0.0.5:8443")
        XCTAssertEqual(c.identityURL(for: .publicInternet).absoluteString, "https://files.example.org")
        XCTAssertEqual(c.identityURL(for: .direct).absoluteString, "https://10.0.0.5:8443")
    }

    func testLANOnlyRoutes() throws {
        let c = try Pairing.decode(payload: Self.lanOnly).routeConfig
        XCTAssertEqual(c.base(for: .publicInternet), c.direct)
        XCTAssertEqual(c.signInURL(for: .publicInternet), c.direct)
        XCTAssertEqual(c.identityURL(for: .publicInternet), c.direct)
        XCTAssertEqual(c.pins, [Self.pinA, Self.pinB])
    }

    // MARK: client behaviour

    private func client(_ p: Pairing?, _ t: MockTransport, token: Bool = false) -> GatewayClient {
        let store = MemoryTokenStore(token ? StoredToken(token: "tok", deviceId: "d", expiresAt: .distantFuture) : nil)
        let c = GatewayClient(transport: t, tokens: store, selector: RouteSelector(config: p?.routeConfig))
        c.retryDelays = [0]
        return c
    }

    func testPasswordNeverGoesThroughATunnel() async throws {
        let p = try Pairing(name: "N", lan: URL(string: "https://10.0.0.5:8443")!, pins: [Self.pinA],
                            publicURL: URL(string: "https://files.example.org")!, publicMode: .tunnel)
        // Away from home (unprobed = publicInternet): the password still only goes to the
        // pinned home address, which doesn't answer; it is not retried through the tunnel.
        let t = MockTransport([{ r in
            XCTAssertEqual(r.url?.host, "10.0.0.5")
            throw URLError(.timedOut)
        }])
        let c = client(p, t)
        XCTAssertFalse(c.selector.canPasswordSignIn)
        do {
            _ = try await c.login(user: "u", password: "pw", deviceName: "d")
            XCTFail("signed in")
        } catch GatewayError.unreachable {
        } catch { XCTFail("\(error)") }
        XCTAssertEqual(t.seen.count, 1)
        XCTAssertFalse(t.seen.contains { $0.url?.host == "files.example.org" })
    }

    func testPasswordGoesToADirectModePublicAddress() async throws {
        let t = MockTransport([{ r in
            XCTAssertEqual(r.url?.absoluteString, "https://files.example.org/v1/auth/login")
            return (200, [:], Data(#"{"token":"t","device_id":"d","expires_at":1,"nas_user":"u"}"#.utf8))
        }])
        let c = client(try Pairing.decode(payload: Self.withDirectPub), t)
        XCTAssertTrue(c.selector.canPasswordSignIn)
        _ = try await c.login(user: "u", password: "pw", deviceName: "d")
        XCTAssertEqual(t.seen.count, 1)
    }

    func testPasswordGoesToLANWhenHome() async throws {
        let t = MockTransport([{ r in
            XCTAssertEqual(r.url?.absoluteString, "https://10.0.0.20:8443/v1/auth/login")
            return (200, [:], Data(#"{"token":"t","device_id":"d","expires_at":1,"nas_user":"u"}"#.utf8))
        }])
        let c = client(try Pairing.decode(payload: Self.lanOnly), t)
        await c.selector.reprobe { true }
        _ = try await c.login(user: "u", password: "pw", deviceName: "d")
    }

    func testIdentityOverTheTunnel() async throws {
        let p = try Pairing(name: "N", lan: URL(string: "https://10.0.0.5:8443")!, pins: [Self.pinA],
                            publicURL: URL(string: "https://files.example.org")!, publicMode: .tunnel)
        let t = MockTransport([{ r in
            XCTAssertEqual(r.url?.absoluteString, "https://files.example.org/v1/identity")
            return (200, [:], Data(#"{"direct_spki_sha256":["\#(Self.pinB)"]}"#.utf8))
        }])
        let c = client(p, t)
        await c.selector.reprobe { true }
        let id = try await c.identity()
        XCTAssertEqual(id.directSpkiSha256, [Self.pinB])
    }

    func testUnpairedClientSendsNothing() async {
        let t = MockTransport([])
        let c = client(nil, t, token: true)
        do { _ = try await c.me(); XCTFail() } catch GatewayError.notPaired {} catch { XCTFail("\(error)") }
        do { _ = try await c.identity(); XCTFail() } catch GatewayError.notPaired {} catch { XCTFail("\(error)") }
        do { _ = try await c.login(user: "u", password: "p", deviceName: "d"); XCTFail() }
        catch GatewayError.notPaired {} catch { XCTFail("\(error)") }
        XCTAssertTrue(t.seen.isEmpty)
        XCTAssertEqual(LoopState(GatewayError.notPaired), .failed(.signInExpired))
    }

    func testLANOnlyDoesNotRetryElsewhere() async throws {
        let t = MockTransport([{ _ in throw URLError(.timedOut) }])
        let c = client(try Pairing.decode(payload: Self.lanOnly), t, token: true)
        await c.selector.reprobe { true }
        do { _ = try await c.me(); XCTFail() } catch GatewayError.unreachable {} catch { XCTFail("\(error)") }
        XCTAssertEqual(t.seen.count, 1)
    }

    func testReconfigureForgetsTheRoute() async throws {
        let s = RouteSelector(config: try Pairing.decode(payload: Self.lanOnly).routeConfig)
        await s.reprobe { true }
        XCTAssertEqual(s.current, .direct)
        s.configure(try Pairing.decode(payload: Self.withDirectPub).routeConfig)
        XCTAssertEqual(s.current, .publicInternet)
        XCTAssertEqual(s.baseURL?.absoluteString, "https://files.example.org")
        s.configure(nil)
        XCTAssertNil(s.baseURL)
        XCTAssertFalse(s.canPasswordSignIn)
    }

    // MARK: storage + 0.1.x migration

    private var suite: String!
    private var shared: SharedState!

    override func setUp() {
        suite = "loft-pairing-test-\(UUID().uuidString)"
        shared = SharedState(suiteName: suite)
    }

    override func tearDown() {
        shared.defaults.removePersistentDomain(forName: suite)
    }

    func testStoredPairingAndPins() throws {
        XCTAssertNil(shared.pairing)
        XCTAssertTrue(shared.effectivePins.isEmpty)
        let p = try Pairing.decode(payload: Self.lanOnly)
        shared.setPairing(p)
        XCTAssertEqual(shared.pairing, p)
        XCTAssertEqual(shared.effectivePins, [Self.pinA, Self.pinB])
        shared.directPins = [Self.pinB]                  // refreshed from /v1/identity
        XCTAssertEqual(shared.effectivePins, [Self.pinB])
        shared.clearAccount()                            // sign-out keeps the pairing and pins
        XCTAssertEqual(shared.pairing, p)
        XCTAssertEqual(shared.effectivePins, [Self.pinB])
        shared.setPairing(try Pairing.decode(payload: Self.withDirectPub))   // another gateway
        XCTAssertEqual(shared.effectivePins, [Self.pinA], "old gateway's refreshed pins are gone")
        shared.setPairing(nil)
        XCTAssertNil(shared.pairing)
    }

    func testMigrationOnlyForSignedInDevicesWithoutAPairing() throws {
        let legacy = try Pairing.decode(payload: Self.withDirectPub)
        XCTAssertFalse(shared.migrateLegacyPairing(signedIn: false, legacy: legacy), "new install")
        XCTAssertNil(shared.pairing)
        XCTAssertFalse(shared.migrateLegacyPairing(signedIn: true, legacy: nil), "public build")
        XCTAssertNil(shared.pairing)
        shared.directPins = [Self.pinB]                  // 0.1.x had refreshed its pins
        XCTAssertTrue(shared.migrateLegacyPairing(signedIn: true, legacy: legacy))
        XCTAssertEqual(shared.pairing, legacy)
        XCTAssertEqual(shared.effectivePins, [Self.pinB], "the refreshed pins survive migration")
        let other = try Pairing.decode(payload: Self.lanOnly)
        shared.setPairing(other)
        XCTAssertFalse(shared.migrateLegacyPairing(signedIn: true, legacy: legacy), "never overwrites")
        XCTAssertEqual(shared.pairing, other)
    }

    func testLegacyPairingIsValidIfThisBuildHasOne() {
        // Public tree: no LegacyPairing+Ops.swift, nothing to migrate. A private build's
        // generated payload must parse, or 0.1.x devices would silently stay unpaired.
        // LOFT_EXPECT_LEGACY=1: this run must carry it (checks the generator + the cast).
        if ProcessInfo.processInfo.environment["LOFT_EXPECT_LEGACY"] == "1" {
            XCTAssertNotNil(LegacyPairing.pairing, "no legacy pairing compiled in")
        }
        if (LegacyPairing.self as Any.Type) is LegacyPairingSource.Type {
            XCTAssertNotNil(LegacyPairing.pairing, "LegacyPairing+Ops.swift holds an invalid payload")
        } else {
            XCTAssertNil(LegacyPairing.pairing)
        }
    }
}
