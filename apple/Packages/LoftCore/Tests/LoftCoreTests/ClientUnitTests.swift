import XCTest
@testable import LoftCore
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Scripted transport: each request is answered by the next handler in line.
final class MockTransport: HTTPTransport, @unchecked Sendable {
    typealias Handler = (URLRequest) throws -> (Int, [String: String], Data)
    var handlers: [Handler]
    var seen: [URLRequest] = []
    init(_ h: [Handler]) { handlers = h }

    func send(_ r: URLRequest) async throws -> (Data, HTTPURLResponse) {
        seen.append(r)
        let h = handlers.removeFirst()
        let (code, headers, body) = try h(r)
        return (body, HTTPURLResponse(url: r.url!, statusCode: code, httpVersion: "HTTP/1.1",
                                      headerFields: headers)!)
    }
}

private func json(_ s: String) -> Data { Data(s.utf8) }

final class ClientUnitTests: XCTestCase {
    let cfg = RouteConfig(direct: URL(string: "https://10.0.0.1:8443")!,
                          publicURL: URL(string: "https://loft.example")!, pins: [])

    private func client(_ t: MockTransport, token: String = "tok-A") -> (GatewayClient, MemoryTokenStore) {
        let store = MemoryTokenStore(StoredToken(token: token, deviceId: "d1",
                                                 expiresAt: Date().addingTimeInterval(86400)))
        let c = GatewayClient(transport: t, tokens: store, selector: RouteSelector(config: cfg))
        c.retryDelays = [0, 0]
        return (c, store)
    }

    func testSHA256Vectors() {
        XCTAssertEqual(Hasher256.hex(of: Data()),
                       "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        XCTAssertEqual(Hasher256.hex(of: Data("abc".utf8)),
                       "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        // two-block message, and a long one fed in odd pieces
        XCTAssertEqual(Hasher256.hex(of: Data("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq".utf8)),
                       "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1")
        var h = Hasher256()
        let a = Data(repeating: 0x61, count: 1_000_000)
        var i = 0
        while i < a.count { let n = min(777, a.count - i); h.update(a.subdata(in: i..<i + n)); i += n }
        XCTAssertEqual(h.finalizeHex(), "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0")
    }

    func testSPKIPinMatchesOpenSSL() {
        // A throwaway P-256 key (used nowhere). Expected pin made the way openssl
        // and the gateway make it:
        //   openssl ec -in k.pem -pubout -outform der | openssl dgst -sha256 -binary | base64
        let hex = "0413837656c1ef575d2aa9e4f9479cd78e3881cac93f2335b8295b6990dd10bd45d739dd14ae3dfdd1673bb3efe07f06ea8bf37ad100a665d85bc6baf5dd656d09"
        var d = Data()
        var idx = hex.startIndex
        while idx < hex.endIndex {
            let n = hex.index(idx, offsetBy: 2)
            d.append(UInt8(hex[idx..<n], radix: 16)!)
            idx = n
        }
        XCTAssertEqual(SPKIPin.pin(ecPoint: d), "IKvfsXGEqE/chNtQCiZAAd+gCKwQLWQN2XrXp9GWCMQ=")  // gitleaks:allow (test key)
        XCTAssertTrue(Pairing.isPin(SPKIPin.pin(ecPoint: d)!))
        XCTAssertNil(SPKIPin.pin(ecPoint: d.prefix(33)))   // compressed / unknown: fail closed
    }

    func testRetriesOnceWithATokenTheAppRotatedMeanwhile() async throws {
        var store: MemoryTokenStore!
        let t = MockTransport([
            { r in
                XCTAssertEqual(r.value(forHTTPHeaderField: "Authorization"), "Bearer tok-A")
                try store.save(StoredToken(token: "tok-B", deviceId: "d1", expiresAt: Date()))
                return (401, [:], json(#"{"error":"invalid_token"}"#))
            },
            { r in
                XCTAssertEqual(r.value(forHTTPHeaderField: "Authorization"), "Bearer tok-B")
                return (200, [:], json(#"{"anchor":"e.5"}"#))
            },
        ])
        let (c, s) = client(t)
        store = s
        let a = try await c.latestAnchor()
        XCTAssertEqual(a, "e.5")
    }

    func testThumbnail() async throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47])
        let t = MockTransport([
            { r in
                XCTAssertEqual(r.url?.path, "/v1/items/it1/thumbnail")
                XCTAssertEqual(r.url?.query, "px=256")
                return (200, ["Content-Type": "image/png"], png)
            },
            { _ in (404, [:], json(#"{"error":"no_thumbnail"}"#)) },
            { _ in (404, [:], json(#"{"error":"not_found"}"#)) },
        ])
        let (c, _) = client(t)
        let got = try await c.thumbnail("it1", px: 256)
        XCTAssertEqual(got, png)
        let none = try await c.thumbnail("it2", px: 256)
        XCTAssertNil(none)                                   // a kind with no thumbnail: the icon
        do { _ = try await c.thumbnail("gone", px: 256); XCTFail() } catch {
            XCTAssertEqual((error as? GatewayError)?.apiError?.error, "not_found")
        }
    }

    func testDownloadRangeWritesAtItsOffset() async throws {
        let t = MockTransport([
            { r in
                XCTAssertEqual(r.value(forHTTPHeaderField: "Range"), "bytes=8-11")
                return (206, ["ETag": "\"c7\"", "Content-Range": "bytes 8-11/100"], Data("WXYZ".utf8))
            },
            { _ in throw URLError(.networkConnectionLost) },          // retried
            { r in
                XCTAssertEqual(r.value(forHTTPHeaderField: "Range"), "bytes=96-103")
                return (206, ["ETag": "\"c7\"", "Content-Range": "bytes 96-99/100"], Data("tail".utf8))
            },
            { _ in (416, [:], json(#"{"error":"range_not_satisfiable"}"#)) },   // empty file
            { r in
                XCTAssertNil(r.value(forHTTPHeaderField: "Range"))
                return (200, ["ETag": "\"c1\""], Data())
            },
        ])
        let (c, _) = client(t)
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let f = dir.appendingPathComponent("v")
        var r = try await c.downloadRange("v1", offset: 8, length: 4, into: f)
        XCTAssertEqual(r.etag, "c7"); XCTAssertEqual(r.total, 100)
        XCTAssertEqual(r.start, 8); XCTAssertEqual(r.count, 4)
        r = try await c.downloadRange("v1", offset: 96, length: 8, into: f)   // same file, later bytes
        XCTAssertEqual(r.start, 96); XCTAssertEqual(r.count, 4)                // cut at EOF
        let d = try Data(contentsOf: f)
        XCTAssertEqual(d.count, 100)
        XCTAssertEqual(d.subdata(in: 8..<12), Data("WXYZ".utf8))
        XCTAssertEqual(d.subdata(in: 96..<100), Data("tail".utf8))
        let e = dir.appendingPathComponent("empty")
        r = try await c.downloadRange("e1", offset: 0, length: 4096, into: e)
        XCTAssertEqual(r.total, 0); XCTAssertEqual(r.count, 0); XCTAssertEqual(r.etag, "c1")
    }

    func testDeadTokenIsNotRetried() async {
        let t = MockTransport([{ _ in (401, [:], json(#"{"error":"device_revoked"}"#)) }])
        let (c, _) = client(t)
        do { _ = try await c.me(); XCTFail() } catch {
            XCTAssertEqual(LoopState(error), .failed(.deviceRemoved))
        }
    }

    func testCloudflareHTMLIs_CantReachHome_GatewayJSONIsNot() async {
        let t = MockTransport([
            { _ in (530, ["Content-Type": "text/html"], json("<html>origin unreachable</html>")) },
            { _ in (503, [:], json(#"{"error":"nas_unavailable","detail":"x"}"#)) },
            { _ in (403, [:], json(#"{"error":"access_denied"}"#)) },
        ])
        let (c, _) = client(t)
        do { _ = try await c.me() } catch { XCTAssertEqual(LoopState(error), .cantReachHome) }
        do { _ = try await c.me() } catch { XCTAssertEqual(LoopState(error), .cantReachNAS) }
        do { _ = try await c.me() } catch { XCTAssertEqual(LoopState(error), .failed(.noAccess)) }
    }

    func testDirectRouteDropsToInternetMidSession() async throws {
        let t = MockTransport([
            { r in XCTAssertEqual(r.url?.host, "10.0.0.1"); throw URLError(.timedOut) },
            { r in XCTAssertEqual(r.url?.host, "loft.example"); return (200, [:], json(#"{"anchor":"e.1"}"#)) },
        ])
        let (c, _) = client(t)
        await c.selector.reprobe { true }
        XCTAssertEqual(c.selector.current, .direct)
        _ = try await c.latestAnchor()
        XCTAssertEqual(c.selector.current, .publicInternet)
    }

    func testLoginAlwaysGoesDirectAndIsNeverRetriedElsewhere() async {
        let t = MockTransport([{ r in XCTAssertEqual(r.url?.host, "10.0.0.1"); throw URLError(.timedOut) }])
        let (c, _) = client(t)
        do { _ = try await c.login(user: "loft-x", password: "p", deviceName: "Mac"); XCTFail() } catch {
            XCTAssertTrue((error as? GatewayError)?.isUnreachable ?? false)
        }
        XCTAssertEqual(t.seen.count, 1)
    }

    func testWrongOffsetResumesFromTheGatewaysCount() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let f = dir.appendingPathComponent("f")
        try Data(repeating: 7, count: 25).write(to: f)
        let t = MockTransport([
            { _ in (200, [:], json(#"{"upload_id":"u","received":0,"size":25,"chunk_max":10}"#)) },
            { r in  // the gateway already had 10 bytes (a previous attempt's chunk landed)
                XCTAssertTrue(r.url!.query!.contains("offset=0"))
                return (409, [:], json(#"{"error":"wrong_offset","received":10}"#)) },
            { r in XCTAssertTrue(r.url!.query!.contains("offset=10")); XCTAssertEqual(r.httpBody?.count, 10)
                return (200, [:], json(#"{"upload_id":"u","received":20,"size":25}"#)) },
            { _ in throw URLError(.networkConnectionLost) },          // transport blip ...
            { _ in (200, [:], json(#"{"upload_id":"u","received":20,"size":25}"#)) },   // ... status
            { r in XCTAssertTrue(r.url!.query!.contains("offset=20")); XCTAssertEqual(r.httpBody?.count, 5)
                XCTAssertEqual(r.value(forHTTPHeaderField: "X-Chunk-Sha256"),
                               Hasher256.hex(of: Data(repeating: 7, count: 5)))
                return (200, [:], json(#"{"upload_id":"u","received":25,"size":25}"#)) },
        ])
        let (c, _) = client(t)
        let id = try await c.upload(file: f)
        XCTAssertEqual(id, "u")
        XCTAssertTrue(t.handlers.isEmpty)
    }

    func testLoopStateMapping() {
        XCTAssertEqual(LoopState(GatewayError.unreachable("x")), .cantReachHome)
        XCTAssertEqual(LoopState(URLError(.notConnectedToInternet)), .cantReachHome)
        XCTAssertEqual(LoopState(GatewayError.notSignedIn), .failed(.signInExpired))
        XCTAssertEqual(LoopState(GatewayError.api(APIError(error: "nas_credentials_invalid", status: 401))),
                       .failed(.nasPasswordChanged))
        XCTAssertEqual(LoopState(GatewayError.api(APIError(error: "token_superseded", status: 401))),
                       .failed(.signInExpired))
        XCTAssertEqual(LoopState(GatewayError.api(APIError(error: "no_root_selected", status: 409))),
                       .failed(.stopped(code: "no_root_selected")))
    }

    func testRealGatewayBodiesDecode() throws {
        let me = try JSONDecoder().decode(Me.self, from: json(#"""
        {"nas_user":"loft-x","device_id":"d","device_name":"Mac","route":"direct",
         "root":{"id":"r","share":"files","path":"","root_item_id":"r","anchor":"ab12.0"}}
        """#))
        XCTAssertEqual(me.root?.displayPath, "files")
        let e = try JSONDecoder().decode(APIError.self, from: json(#"""
        {"error":"content_etag_mismatch","current":{"id":"i","parent_id":"r","name":"a","type":"file",
         "size":1,"mtime_ns":5,"content_etag":"c2","meta_etag":"m1","sha256":null}}
        """#))
        XCTAssertEqual(e.current?.contentEtag, "c2")
        let d = try JSONDecoder().decode(DeleteResponse.self, from: json(#"{"result":"superseded","superseded_by":"y"}"#))
        XCTAssertEqual(d.supersededBy, "y")
    }
}
