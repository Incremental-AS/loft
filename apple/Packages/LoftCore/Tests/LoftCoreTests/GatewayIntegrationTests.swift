import XCTest
@testable import LoftCore
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Runs the client against the real gateway code (scripts/dev_server.py, LocalBackend).
/// Skipped unless apple/scripts/core-it.sh set it up:
///   LOFT_IT_PUBLIC  http://127.0.0.1:18080   (public listener; login was done over direct)
///   LOFT_IT_TOKEN   a device token with a root selected
///   LOFT_IT_NAS     the folder the root points at, to check bytes on "the NAS"
final class GatewayIntegrationTests: XCTestCase {
    var c: GatewayClient!
    var nas: URL!
    var work: URL!
    var root: RootInfo!

    override func setUp() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let pub = env["LOFT_IT_PUBLIC"], let tok = env["LOFT_IT_TOKEN"], let n = env["LOFT_IT_NAS"] else {
            throw XCTSkip("LOFT_IT_* not set (apple/scripts/core-it.sh)")
        }
        let cfg = RouteConfig(direct: URL(string: "https://127.0.0.1:1")!, publicURL: URL(string: pub)!, pins: [])
        let store = MemoryTokenStore(StoredToken(token: tok, deviceId: "", expiresAt: Date().addingTimeInterval(3600)))
        c = GatewayClient(transport: URLSessionTransport(session: URLSession(configuration: .ephemeral)),
                          tokens: store, selector: RouteSelector(config: cfg))
        c.chunkLimit = 1000          // force several chunks
        c.downloadSlice = 1024       // and several Range slices
        nas = URL(fileURLWithPath: n)
        work = FileManager.default.temporaryDirectory.appendingPathComponent("loft-it-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
        root = try await c.me().root
        XCTAssertNotNil(root)
        // each test in its own folder
        let dir = try await c.mkdir(parentId: root.rootItemId, name: "t-\(name.filter(\.isLetter))-\(UUID().uuidString.prefix(6))")
        folder = dir
    }
    var folder: Item!

    private func file(_ bytes: Data) throws -> URL {
        let u = work.appendingPathComponent(UUID().uuidString)
        try bytes.write(to: u)
        return u
    }

    private func bytes(_ n: Int, seed: UInt8 = 1) -> Data {
        Data((0..<n).map { UInt8(truncatingIfNeeded: $0 &* 31 &+ Int(seed)) })
    }

    private func onNAS(_ name: String) throws -> Data {
        try Data(contentsOf: nas.appendingPathComponent(folder.name).appendingPathComponent(name))
    }

    func testCreateDownloadModifyConflict() async throws {
        let v1 = bytes(3500)
        let r1 = try await c.uploadAndCommit(file: try file(v1), CommitRequest(parentId: folder.id, name: "a.bin"))
        XCTAssertEqual(r1.result, .created)
        XCTAssertEqual(try onNAS("a.bin"), v1)
        XCTAssertEqual(r1.item.size, 3500)

        let dl = work.appendingPathComponent("dl")
        let tag = try await c.download(r1.item.id, to: dl)
        XCTAssertEqual(tag, r1.item.contentEtag)
        XCTAssertEqual(try Data(contentsOf: dl), v1)

        let v2 = bytes(2100, seed: 9)
        let r2 = try await c.uploadAndCommit(file: try file(v2),
                                             CommitRequest(itemId: r1.item.id, ifMatch: r1.item.contentEtag))
        XCTAssertEqual(r2.result, .modified)
        XCTAssertEqual(r2.item.id, r1.item.id)
        XCTAssertNotEqual(r2.item.contentEtag, r1.item.contentEtag)
        XCTAssertEqual(try onNAS("a.bin"), v2)

        // a second Mac still based on v1: never overwrites, lands as a conflict copy
        let v3 = bytes(10, seed: 3)
        let r3 = try await c.uploadAndCommit(file: try file(v3),
                                             CommitRequest(itemId: r1.item.id, ifMatch: r1.item.contentEtag))
        XCTAssertEqual(r3.result, .conflict)
        XCTAssertNotEqual(r3.item.id, r1.item.id)
        XCTAssertTrue(r3.item.name.contains("(conflict, "), r3.item.name)
        XCTAssertEqual(try onNAS("a.bin"), v2)
        XCTAssertEqual(try onNAS(r3.item.name), v3)
    }

    func testEmptyFileRoundTrip() async throws {
        let r = try await c.uploadAndCommit(file: try file(Data()), CommitRequest(parentId: folder.id, name: "empty.txt"))
        XCTAssertEqual(r.item.size, 0)
        let dl = work.appendingPathComponent("e")
        let tag = try await c.download(r.item.id, to: dl)
        XCTAssertEqual(try Data(contentsOf: dl).count, 0)
        XCTAssertEqual(tag, r.item.contentEtag)      // was "" (the loop ended on the 416)
    }

    func testSafeSaveCreateBeforeDeleteIsOneVersion() async throws {
        let x = try await c.uploadAndCommit(file: try file(bytes(500)), CommitRequest(parentId: folder.id, name: "Report.docx"))
        // Word: writes a temp, renames it over the original -> createItem(N) arrives while X is live
        let y = try await c.uploadAndCommit(file: try file(bytes(700, seed: 5)),
                                            CommitRequest(parentId: folder.id, name: "Report.docx",
                                                          ifMatch: x.item.contentEtag, replaces: x.item.id))
        XCTAssertEqual(y.result, .replaced)
        XCTAssertEqual(y.replacedId, x.item.id)
        let d = try await c.delete(x.item.id, ifMatch: x.item.contentEtag)   // the later deleteItem(X)
        XCTAssertEqual(d.result, "superseded")
        let kids = try await c.children(folder.id, cursor: nil).items.map(\.name)
        XCTAssertEqual(kids, ["Report.docx"])
    }

    func testRenameMoveDeleteAndStaleDelete() async throws {
        let sub = try await c.mkdir(parentId: folder.id, name: "Sub")
        let f = try await c.uploadAndCommit(file: try file(bytes(10)), CommitRequest(parentId: folder.id, name: "x.txt"))
        let renamed = try await c.patch(f.item.id, name: "y.txt", ifMatch: f.item.metaEtag)
        XCTAssertEqual(renamed.name, "y.txt")
        let moved = try await c.patch(f.item.id, parentId: sub.id)
        XCTAssertEqual(moved.parentId, sub.id)
        XCTAssertEqual(try Data(contentsOf: nas.appendingPathComponent(folder.name).appendingPathComponent("Sub/y.txt")),
                       bytes(10))
        do {
            _ = try await c.mkdir(parentId: folder.id, name: "sub")    // case-insensitive collision
            XCTFail()
        } catch GatewayError.api(let e) {
            XCTAssertEqual(e.error, "name_exists"); XCTAssertEqual(e.existingId, sub.id)
        }
        do {
            _ = try await c.delete(f.item.id, ifMatch: "c999")
            XCTFail()
        } catch GatewayError.api(let e) {
            XCTAssertEqual(e.status, 412); XCTAssertEqual(e.current?.id, f.item.id)
        }
        let d = try await c.delete(sub.id)
        XCTAssertEqual(d.result, "trashed")
        let kids = try await c.children(folder.id, cursor: nil).items
        XCTAssertTrue(kids.isEmpty)
    }

    func testChangeFeedSeesWritesAndLongPollReturnsEarly() async throws {
        let a0 = try await c.latestAnchor()
        let f = try await c.uploadAndCommit(file: try file(bytes(5)), CommitRequest(parentId: folder.id, name: "n.txt"))
        let p = try await c.changes(anchor: a0)
        XCTAssertTrue(p.updated.contains { $0.id == f.item.id })
        // a long-poll that a write wakes up
        let a1 = p.anchor
        let started = Date()
        async let page = c.changes(anchor: a1, wait: 20)
        try await Task.sleep(nanoseconds: 500_000_000)
        _ = try await c.delete(f.item.id)
        let p2 = try await page
        XCTAssertTrue(p2.deleted.contains(f.item.id))
        XCTAssertLessThan(Date().timeIntervalSince(started), 10)
        do { _ = try await c.changes(anchor: "nope.0"); XCTFail() } catch GatewayError.api(let e) {
            XCTAssertTrue(e.isAnchorExpired)
        }
    }

    func testUploadResumesFromARememberedSession() async throws {
        let data = bytes(4200, seed: 7)
        let src = try file(data)
        let sha = try Hasher256.hex(ofFile: src)
        let shared = SharedState(suiteName: "loft-it-\(UUID().uuidString)")
        // an earlier attempt got 2 chunks in before the extension was killed
        let st = try await c.uploadCreate(size: 4200, sha256: sha)
        _ = try await c.uploadChunk(st.uploadId, offset: 0, data: data.prefix(1000))
        _ = try await c.uploadChunk(st.uploadId, offset: 1000, data: data.subdata(in: 1000..<2000))
        shared.setResumableUpload(st.uploadId, sha256: sha, size: 4200)
        let r = try await c.uploadAndCommit(file: src, CommitRequest(parentId: folder.id, name: "big.bin"), shared: shared)
        XCTAssertEqual(r.result, .created)
        XCTAssertEqual(try onNAS("big.bin"), data)
        XCTAssertNil(shared.resumableUpload(sha256: sha, size: 4200))
    }

    func testChildrenPaging() async throws {
        for i in 0..<7 { _ = try await c.mkdir(parentId: folder.id, name: "d\(i)") }
        var names: [String] = []
        var cursor: String?
        repeat {
            let p = try await c.children(folder.id, cursor: cursor, limit: 3)
            names += p.items.map(\.name)
            cursor = p.nextCursor
        } while cursor != nil
        XCTAssertEqual(names, (0..<7).map { "d\($0)" })
    }
}
