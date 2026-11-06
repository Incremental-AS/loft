import XCTest
@testable import LoftCore
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// A gateway that answers Range requests for one file, from any number of lanes at once.
final class RangeServer: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var content: Data
    private(set) var etag: String
    private(set) var requested: [Int64] = []          // start offsets, in arrival order
    private(set) var lanesUsed = Set<Int>()
    private(set) var maxInFlight = 0
    private var inFlight = 0
    var failOnce = Set<Int64>()                        // these offsets drop once
    var failFrom: Int64?                               // offsets >= this always drop
    var delay: UInt64 = 3_000_000

    init(_ content: Data, etag: String = "c1") { self.content = content; self.etag = etag }

    func replace(_ data: Data, etag: String) { lock.withLock { content = data; self.etag = etag } }

    func answer(_ r: URLRequest, lane: Int) async throws -> (Data, HTTPURLResponse) {
        let range = r.value(forHTTPHeaderField: "Range")
        var start: Int64 = 0, end: Int64 = 0
        if let range, let eq = range.firstIndex(of: "=") {
            let parts = range[range.index(after: eq)...].split(separator: "-", omittingEmptySubsequences: false)
            start = Int64(parts[0])!; end = Int64(parts[1])!
        }
        let (body, tag, fail): (Data, String, Bool) = lock.withLock {
            inFlight += 1; maxInFlight = max(maxInFlight, inFlight)
            requested.append(start); lanesUsed.insert(lane)
            var f = failOnce.remove(start) != nil
            if let failFrom, start >= failFrom { f = true }
            return (content, etag, f)
        }
        defer { lock.withLock { inFlight -= 1 } }
        try await Task.sleep(nanoseconds: delay)
        if fail { throw URLError(.networkConnectionLost) }
        let n = Int64(body.count)
        func resp(_ code: Int, _ h: [String: String]) -> HTTPURLResponse {
            HTTPURLResponse(url: r.url!, statusCode: code, httpVersion: "HTTP/1.1",
                            headerFields: h.merging(["ETag": "\"\(tag)\""]) { a, _ in a })!
        }
        guard range != nil else { return (body, resp(200, [:])) }
        if start >= n {
            return (Data(#"{"error":"range_not_satisfiable"}"#.utf8), resp(416, ["Content-Range": "bytes */\(n)"]))
        }
        let last = min(end, n - 1)
        return (body.subdata(in: Int(start)..<Int(last + 1)),
                resp(206, ["Content-Range": "bytes \(start)-\(last)/\(n)"]))
    }
}

struct Lane: HTTPTransport {
    let server: RangeServer
    let id: Int
    func send(_ r: URLRequest) async throws -> (Data, HTTPURLResponse) { try await server.answer(r, lane: id) }
}

final class Progresses: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var seen: [(Int64, Int64)] = []
    func add(_ d: Int64, _ t: Int64) { lock.withLock { seen.append((d, t)) } }
}

final class ParallelDownloadTests: XCTestCase {
    let cfg = RouteConfig(direct: URL(string: "https://10.0.0.1:8443")!,
                          publicURL: URL(string: "https://loft.example")!, pins: [])
    var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("loft-pd-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDown() { try? FileManager.default.removeItem(at: dir) }

    private func bytes(_ n: Int, seed: UInt8 = 1) -> Data { Data((0..<n).map { UInt8(truncatingIfNeeded: $0 &* 7) &+ seed }) }

    private func client(_ s: RangeServer, lanes: Int = 4) -> GatewayClient {
        let store = MemoryTokenStore(StoredToken(token: "tok", deviceId: "d1", expiresAt: Date().addingTimeInterval(86400)))
        let c = GatewayClient(transport: Lane(server: s, id: -1), tokens: store, selector: RouteSelector(config: cfg))
        c.retryDelays = [0, 0]
        c.downloadSlice = 64
        c.downloadParallel = 4
        c.downloadLanes = (0..<lanes).map { Lane(server: s, id: $0) }
        return c
    }

    func testSlicesRunInParallelOnSeparateLanes() async throws {
        let data = bytes(1000)
        let s = RangeServer(data)
        let p = Progresses()
        let f = dir.appendingPathComponent("a")
        let tag = try await client(s).download("x", to: f, progress: { p.add($0, $1) })
        XCTAssertEqual(tag, "c1")
        XCTAssertEqual(try Data(contentsOf: f), data)
        XCTAssertEqual(s.requested.count, 16)              // 1000 / 64, each once
        XCTAssertGreaterThan(s.maxInFlight, 1)
        XCTAssertLessThanOrEqual(s.maxInFlight, 4)
        XCTAssertEqual(s.lanesUsed, [0, 1, 2, 3])
        XCTAssertEqual(p.seen.last?.0, 1000); XCTAssertEqual(p.seen.last?.1, 1000)
        XCTAssertEqual(p.seen.map(\.0), p.seen.map(\.0).sorted())   // never goes backwards
        XCTAssertFalse(FileManager.default.fileExists(atPath: GatewayClient.partsNote(for: f).path))
    }

    func testSizeHintStartsAllSlicesAtOnce() async throws {
        let s = RangeServer(bytes(640))
        let f = dir.appendingPathComponent("a")
        try await client(s).download("x", to: f, expectedSize: 640)
        XCTAssertEqual(try Data(contentsOf: f), s.content)
        XCTAssertEqual(s.requested.count, 10)
        XCTAssertEqual(s.maxInFlight, 4)
    }

    func testStaleSizeHintStillGivesTheRightFile() async throws {
        let s = RangeServer(bytes(700))
        let f = dir.appendingPathComponent("a")
        try await client(s).download("x", to: f, expectedSize: 300)
        XCTAssertEqual(try Data(contentsOf: f), s.content)
    }

    func testDroppedSliceIsRetried() async throws {
        let s = RangeServer(bytes(500))
        s.failOnce = [128, 384]
        let f = dir.appendingPathComponent("a")
        try await client(s).download("x", to: f, expectedSize: 500)
        XCTAssertEqual(try Data(contentsOf: f), s.content)
        XCTAssertEqual(s.requested.filter { $0 == 128 }.count, 2)
    }

    func testResumeKeepsFinishedSlices() async throws {
        let data = bytes(1000)
        let s = RangeServer(data)
        s.failFrom = 512                                    // the connection is gone half-way
        let c = client(s)
        let f = dir.appendingPathComponent("a.part")
        do {
            try await c.download("x", to: f, expectedSize: 1000, resume: true)
            XCTFail("should have failed")
        } catch GatewayError.unreachable {}
        XCTAssertTrue(FileManager.default.fileExists(atPath: GatewayClient.partsNote(for: f).path))
        let before = s.requested.count
        s.failFrom = nil
        let p = Progresses()
        try await c.download("x", to: f, expectedSize: 1000, resume: true, progress: { p.add($0, $1) })
        XCTAssertEqual(try Data(contentsOf: f), data)
        let again = s.requested[before...]
        XCTAssertTrue(again.allSatisfy { $0 >= 512 }, "re-fetched a finished slice: \(Array(again))")
        XCTAssertEqual(again.count, 8)
        XCTAssertGreaterThanOrEqual(p.seen.first?.0 ?? 0, 512)   // starts from what it had
        XCTAssertFalse(FileManager.default.fileExists(atPath: GatewayClient.partsNote(for: f).path))
    }

    func testResumeOfAnotherVersionStartsOver() async throws {
        let s = RangeServer(bytes(1000))
        s.failFrom = 512
        let c = client(s)
        let f = dir.appendingPathComponent("a.part")
        _ = try? await c.download("x", to: f, expectedSize: 1000, resume: true)
        let v2 = bytes(900, seed: 9)
        s.replace(v2, etag: "c2")
        s.failFrom = nil
        let tag = try await c.download("x", to: f, expectedSize: 900, resume: true)
        XCTAssertEqual(tag, "c2")
        XCTAssertEqual(try Data(contentsOf: f), v2)
    }

    func testWithoutResumeNothingIsNotedAndTheFileStartsClean() async throws {
        let s = RangeServer(bytes(200))
        let f = dir.appendingPathComponent("a")
        try Data(repeating: 0xFF, count: 5000).write(to: f)        // an older, longer file
        try await client(s).download("x", to: f, expectedSize: 200)
        XCTAssertEqual(try Data(contentsOf: f), s.content)
    }

    func testEmptyFileWithAHintOfZero() async throws {
        let s = RangeServer(Data(), etag: "c4")
        let f = dir.appendingPathComponent("e")
        let tag = try await client(s).download("x", to: f, expectedSize: 0, resume: true)
        XCTAssertEqual(tag, "c4")
        XCTAssertEqual(try Data(contentsOf: f).count, 0)
    }

    func testOneLaneStillWorks() async throws {
        let s = RangeServer(bytes(300))
        let c = client(s, lanes: 0)                           // no lanes: the main transport
        let f = dir.appendingPathComponent("a")
        try await c.download("x", to: f)
        XCTAssertEqual(try Data(contentsOf: f), s.content)
        XCTAssertEqual(s.lanesUsed, [-1])
    }
}
