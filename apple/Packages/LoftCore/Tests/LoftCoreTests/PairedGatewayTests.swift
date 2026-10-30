// Pairing end to end against the real gateway code (scripts/dev_server.py): the pairing
// code feeds the route selector and the pinned session, the password goes only over TLS
// whose key matches a pin, and pins refresh from /v1/identity. Needs Security (SecTrust),
// so macOS/iOS only; skipped unless apple/scripts/core-it.sh set it up:
//   LOFT_IT_PAIR      pairing link for the dev server's direct listener (its dev pin)
//   LOFT_IT_USER, LOFT_IT_PASSWORD   the dev server's test account
#if canImport(Security)
import XCTest
@testable import LoftCore

final class PairedGatewayTests: XCTestCase {
    var pairing: Pairing!
    var user = "", password = ""
    var suites: [String] = []

    override func setUp() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let link = env["LOFT_IT_PAIR"], let u = env["LOFT_IT_USER"], let pw = env["LOFT_IT_PASSWORD"] else {
            throw XCTSkip("LOFT_IT_PAIR / LOFT_IT_USER / LOFT_IT_PASSWORD not set (apple/scripts/core-it.sh)")
        }
        pairing = try Pairing.parse(link)
        user = u; password = pw
    }

    override func tearDown() {
        for s in suites { UserDefaults(suiteName: s)?.removePersistentDomain(forName: s) }
    }

    private func environment() -> LoftEnvironment {
        let s = "loft-it-\(UUID().uuidString)"
        suites.append(s)
        return LoftEnvironment(shared: SharedState(suiteName: s), tokens: MemoryTokenStore(), legacy: nil)
    }

    func testUnpairedDoesNothing() async {
        let e = environment()
        XCTAssertFalse(e.isPaired)
        do { let r = await e.reprobe(); XCTAssertEqual(r, .publicInternet) }
        do { _ = try await e.client.login(user: user, password: password, deviceName: "it"); XCTFail() }
        catch GatewayError.notPaired {} catch { XCTFail("\(error)") }
    }

    func testPairProbeRefreshSignIn() async throws {
        let e = environment()
        await e.pair(pairing)
        XCTAssertEqual(e.pairing, pairing)
        do { let r = await e.reprobe(); XCTAssertEqual(r, .direct, "pinned probe of the paired address") }
        XCTAssertTrue(e.selector.canPasswordSignIn)

        await e.refreshPins()                     // LAN-only pairing: over the pinned route
        XCTAssertEqual(e.shared.directPins, Set(pairing.pins), "identity answered over the pinned route")

        let r = try await e.client.login(user: user, password: password, deviceName: "paired-it")
        try e.tokens.save(StoredToken(r))
        let me = try await e.client.me()
        XCTAssertEqual(me.nasUser, r.nasUser ?? user)
        XCTAssertEqual(me.route, "direct")
        try await e.client.revoke(deviceId: r.deviceId)
    }

    func testWrongPinNeverGetsThePassword() async throws {
        let wrong = try Pairing(name: "evil twin", lan: pairing.lan,
                                pins: ["2cgYA3fUhyDBJkOgvM77KnM3sbij6S6NEZglL+RUX40="])  // gitleaks:allow (test key)
        let e = environment()
        await e.pair(wrong)
        do { let r = await e.reprobe(); XCTAssertEqual(r, .publicInternet, "the handshake fails on the pin") }
        XCTAssertFalse(e.selector.canPasswordSignIn, "LAN-only pairing away from home: no sign-in")
        // Force the direct route anyway: the TLS handshake is cancelled before any byte of
        // the request (and so the password) is sent.
        await e.selector.reprobe { true }
        do {
            _ = try await e.client.login(user: user, password: password, deviceName: "evil-it")
            XCTFail("signed in through a key that matches no pin")
        } catch GatewayError.unreachable {
        } catch { XCTFail("\(error)") }
    }

    func testRepairSwitchesInPlace() async throws {
        let e = environment()
        let wrong = try Pairing(name: "other", lan: pairing.lan,
                                pins: ["2cgYA3fUhyDBJkOgvM77KnM3sbij6S6NEZglL+RUX40="])  // gitleaks:allow (test key)
        await e.pair(wrong)
        do { let r = await e.reprobe(); XCTAssertEqual(r, .publicInternet) }
        await e.pair(pairing)                     // "Pair with another gateway"
        do { let r = await e.reprobe(); XCTAssertEqual(r, .direct) }
        await e.pair(nil)
        XCTAssertFalse(e.isPaired)
        XCTAssertNil(e.shared.pairing)
    }
}
#endif
