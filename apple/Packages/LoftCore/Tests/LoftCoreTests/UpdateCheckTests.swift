import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import LoftCore

/// The Mac's releases.json check. No test reaches the network: the transport, URL, clock and
/// defaults are all injected.
final class UpdateCheckTests: XCTestCase {
    // MARK: parsing (docs/releases-json.md)

    func testParsesTheFlatForm() throws {
        let json = """
        {"android": {"version": "0.3.0", "version_code": 3, "url": "https://example.org/a"},
         "macos": {"version": "0.2.1", "url": "https://loft.incremental.no/download/#mac",
                   "sha256": "ab12", "notes": "Fixes", "future_field": 1},
         "something_new": true}
        """
        let r = try XCTUnwrap(UpdateCheck.parse(Data(json.utf8)))
        XCTAssertEqual(r, AvailableRelease(version: "0.2.1", url: URL(string: "https://loft.incremental.no/download/#mac")!,
                                           sha256: "ab12", notes: "Fixes"))
    }

    func testParsesThePlatformsForm() throws {
        let json = #"{"platforms": {"macos": {"version": "0.4.0", "url": "https://example.org/m"}}}"#
        XCTAssertEqual(try UpdateCheck.parse(Data(json.utf8))?.version, "0.4.0")
    }

    func testNoMacEntryIsNil() throws {
        for json in ["{}", #"{"macos": null}"#, #"{"linux": {"version": "9.9.9", "url": "https://example.org/l"}}"#,
                     #"{"platforms": {}}"#, #"{"macos": "0.2.1"}"#] {
            XCTAssertNil(try UpdateCheck.parse(Data(json.utf8)), json)
        }
    }

    func testRefusesAnEntryWithoutHTTPSOrVersion() throws {
        for json in [#"{"macos": {"version": "0.9.0", "url": "http://example.org/m"}}"#,
                     #"{"macos": {"version": "0.9.0", "url": "ftp://example.org/m"}}"#,
                     #"{"macos": {"version": "0.9.0", "url": "https://"}}"#,
                     #"{"macos": {"version": "0.9.0"}}"#,
                     #"{"macos": {"url": "https://example.org/m"}}"#,
                     #"{"macos": {"version": "soon", "url": "https://example.org/m"}}"#,
                     #"{"macos": {"version": "", "url": "https://example.org/m"}}"#] {
            XCTAssertNil(try UpdateCheck.parse(Data(json.utf8)), json)
        }
    }

    func testNotJSONThrows() {
        XCTAssertThrowsError(try UpdateCheck.parse(Data("<html>".utf8)))
        XCTAssertThrowsError(try UpdateCheck.parse(Data("[1, 2]".utf8)))
    }

    // MARK: version compare

    func testVersionCompare() {
        let newer: [(String, String)] = [("0.2.1", "0.2.0"), ("0.10.0", "0.9.3"), ("1.0.0", "0.99.99"),
                                         ("0.2.0", "0.2.0-rc1"), ("v0.3.0", "0.2.9"), ("0.2.0.1", "0.2")]
        for (a, b) in newer {
            XCTAssertTrue(UpdateCheck.isNewer(a, than: b), "\(a) > \(b)")
            XCTAssertFalse(UpdateCheck.isNewer(b, than: a), "\(b) !> \(a)")
        }
        for (a, b) in [("0.2.0", "0.2.0"), ("0.2", "0.2.0"), ("v0.2.0", "0.2.0"), ("0.2.0-rc2", "0.2.0-rc1")] {
            XCTAssertFalse(UpdateCheck.isNewer(a, than: b), "\(a) !> \(b)")
        }
    }

    // MARK: daily limit

    func testDue() {
        let t = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertTrue(UpdateCheck.isDue(lastCheck: nil, now: t))
        XCTAssertFalse(UpdateCheck.isDue(lastCheck: t, now: t))
        XCTAssertFalse(UpdateCheck.isDue(lastCheck: t, now: t + 86399))
        XCTAssertTrue(UpdateCheck.isDue(lastCheck: t, now: t + 86400))
        XCTAssertTrue(UpdateCheck.isDue(lastCheck: t + 3600, now: t), "clock went back")
    }

    // MARK: checker

    final class FakeTransport: HTTPTransport, @unchecked Sendable {
        var status = 200
        var body = Data()
        var fail = false
        private(set) var requests: [URLRequest] = []
        func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
            requests.append(request)
            if fail { throw GatewayError.unreachable("offline") }
            return (body, HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        }
    }

    final class Clock: @unchecked Sendable { var now = Date(timeIntervalSince1970: 1_800_000_000) }

    let testURL = URL(string: "https://releases.test.invalid/releases.json")!
    var defaults: UserDefaults!
    var suite: String!

    override func setUp() {
        suite = "no.incremental.loft.update-tests"      // one file, emptied around every test
        UserDefaults.standard.removePersistentDomain(forName: suite)
        defaults = UserDefaults(suiteName: suite)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
    }

    func checker(_ t: FakeTransport, _ clock: Clock, version: String = "0.2.0") -> UpdateChecker {
        UpdateChecker(currentVersion: version, url: testURL, transport: t, defaults: defaults, now: { clock.now })
    }

    func mac(_ v: String) -> Data { Data(#"{"macos": {"version": "\#(v)", "url": "https://example.org/Loft.dmg"}}"#.utf8) }

    func testChecksAtMostDaily() async {
        let t = FakeTransport(), clock = Clock()
        t.body = mac("0.2.1")
        let c = checker(t, clock)
        let first = await c.checkIfDue()
        XCTAssertEqual(first?.version, "0.2.1")
        XCTAssertEqual(first?.url.absoluteString, "https://example.org/Loft.dmg")
        clock.now += 3600
        let again = await c.checkIfDue()
        XCTAssertEqual(again?.version, "0.2.1", "remembered, not fetched")
        XCTAssertEqual(t.requests.count, 1)
        clock.now += 86400
        t.body = mac("0.2.2")
        let next = await c.checkIfDue()
        XCTAssertEqual(next?.version, "0.2.2")
        XCTAssertEqual(t.requests.count, 2)
    }

    func testTheRequestIsAPlainGETWithAUserAgent() async throws {
        let t = FakeTransport(), clock = Clock()
        t.body = mac("0.2.0")
        await checker(t, clock).checkIfDue()
        let r = try XCTUnwrap(t.requests.first)
        XCTAssertEqual(r.url, testURL)
        XCTAssertEqual(r.httpMethod, "GET")
        XCTAssertNil(r.httpBody)
        XCTAssertEqual(r.allHTTPHeaderFields ?? [:], ["User-Agent": "Loft (macOS)", "Accept-Language": "*"])
        XCTAssertFalse(r.httpShouldHandleCookies)
        XCTAssertNil(URL(string: r.url!.absoluteString)?.query, "no query string")
        XCTAssertEqual(UpdateCheck.releasesURL.absoluteString, "https://loft.incremental.no/releases.json")
    }

    func testSameOrOlderIsNotOffered() async {
        let t = FakeTransport(), clock = Clock()
        t.body = mac("0.2.0")
        let same = await checker(t, clock).checkIfDue()
        XCTAssertNil(same)
        let c = checker(t, clock, version: "0.3.0")
        XCTAssertNil(c.available)
        XCTAssertEqual(c.latest?.version, "0.2.0")
    }

    func testAFailedAttemptStillCountsForTheDay() async {
        let t = FakeTransport(), clock = Clock()
        t.fail = true
        let c = checker(t, clock)
        let r = await c.checkIfDue()
        XCTAssertNil(r)
        XCTAssertNotNil(c.lastError)
        _ = await c.checkIfDue()
        XCTAssertEqual(t.requests.count, 1, "no retry within the day")
        t.fail = false; t.status = 404
        clock.now += 86400
        _ = await c.checkIfDue()
        XCTAssertEqual(t.requests.count, 2)
        XCTAssertNil(c.available)
        t.status = 200; t.body = mac("0.2.5")
        clock.now += 86400
        let ok = await c.checkIfDue()
        XCTAssertEqual(ok?.version, "0.2.5")
        XCTAssertNil(c.lastError)
    }

    func testAPulledReleaseIsForgotten() async {
        let t = FakeTransport(), clock = Clock()
        t.body = mac("0.2.1")
        let c = checker(t, clock)
        _ = await c.checkIfDue()
        XCTAssertNotNil(c.available)
        t.body = Data("{}".utf8)
        clock.now += 86400
        let r = await c.checkIfDue()
        XCTAssertNil(r)
        XCTAssertNil(c.latest)
    }

    func testSwitchedOffNeverFetches() async {
        let t = FakeTransport(), clock = Clock()
        t.body = mac("0.2.1")
        let c = checker(t, clock)
        _ = await c.checkIfDue()
        XCTAssertNotNil(c.available)
        c.isEnabled = false
        XCTAssertNil(c.available, "switching off hides the notice")
        clock.now += 7 * 86400
        let r = await c.checkIfDue()
        XCTAssertNil(r)
        XCTAssertFalse(c.isDue)
        XCTAssertEqual(t.requests.count, 1)
        c.isEnabled = true
        XCTAssertTrue(c.isDue)
        XCTAssertEqual(UpdateChecker(currentVersion: "0.2.0", url: testURL, transport: t, defaults: defaults).isEnabled, true,
                       "the setting is stored")
    }

    func testOnByDefault() {
        XCTAssertTrue(checker(FakeTransport(), Clock()).isEnabled)
    }

    /// What really goes over the wire with the real session, against a local server that
    /// answers with the request headers it got as JSON. Skipped unless
    /// LOFT_UPDATE_ECHO_URL=http://127.0.0.1:<port>/ is set; never the real site.
    func testRealSessionSendsNothingButAUserAgent() async throws {
        guard let s = ProcessInfo.processInfo.environment["LOFT_UPDATE_ECHO_URL"], let u = URL(string: s),
              u.host == "127.0.0.1" else { throw XCTSkip("LOFT_UPDATE_ECHO_URL not set") }
        let (data, resp) = try await URLSessionTransport(session: UpdateCheck.makeSession()).send(UpdateCheck.request(u))
        XCTAssertEqual(resp.statusCode, 200)
        let got = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: String])
        print("update check request headers: \(got.keys.sorted())")
        XCTAssertEqual(got["User-Agent"], "Loft (macOS)")
        // The HTTP stack's own transport headers are fine; nothing that names the user or device.
        let allowed: Set<String> = ["host", "user-agent", "accept", "accept-encoding", "accept-language",
                                    "connection", "priority"]
        XCTAssertEqual(Set(got.keys.map { $0.lowercased() }).subtracting(allowed), [])
        XCTAssertNil(got["Cookie"])
        XCTAssertEqual(got["Accept-Language"], "*", "not the user's languages")
    }
}
