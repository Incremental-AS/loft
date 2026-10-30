// HTTP client for the gateway API (api/openapi.yaml). One instance per process; the base URL
// follows the RouteSelector, the token comes from the TokenStore on every request.
// Foundation-only so the whole protocol (chunked resumable upload, sliced Range
// download, rotation race) is tested on Linux against gateway/scripts/dev_server.py.
import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

public struct URLSessionTransport: HTTPTransport, @unchecked Sendable {
    public let session: URLSession
    public init(session: URLSession) { self.session = session }

    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, resp) = try await session.data(for: request)
        guard let h = resp as? HTTPURLResponse else { throw GatewayError.badResponse(0, "not http") }
        return (data, h)
    }
}

public enum GatewayError: Error, Sendable, CustomStringConvertible {
    case api(APIError)              // the gateway answered with an error body
    case unreachable(String)        // no answer: network, TLS, pin mismatch, timeout
    case badResponse(Int, String)   // answered, but not something we understand
    case notSignedIn                // no token in the keychain
    case notPaired                  // no gateway paired with this device
    case changedDuringTransfer     // the file changed on the NAS under a download

    public var apiError: APIError? { if case .api(let e) = self { e } else { nil } }
    public var isUnreachable: Bool { if case .unreachable = self { true } else { false } }

    public var description: String {
        switch self {
        case .api(let e): "gateway \(e.status) \(e.error)"
        case .unreachable(let s): "unreachable: \(s)"
        case .badResponse(let s, let m): "bad response \(s): \(m)"
        case .notSignedIn: "not signed in"
        case .notPaired: "not paired"
        case .changedDuringTransfer: "changed during transfer"
        }
    }
}

public final class GatewayClient: @unchecked Sendable {
    public let transport: HTTPTransport
    public let tokens: TokenStore
    public let selector: RouteSelector
    /// Re-probe hook: runs the pinned /healthz probe (set by the platform layer).
    public var probe: (@Sendable () async -> Bool)?
    /// Upload chunk / download slice sizes; tests make them small.
    public var chunkLimit: Int64 = 32 << 20
    public var downloadSlice: Int64 = 32 << 20
    public var retryDelays: [UInt64] = [1, 2, 4, 8, 16]   // seconds, transport errors only

    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    public init(transport: HTTPTransport, tokens: TokenStore, selector: RouteSelector) {
        self.transport = transport; self.tokens = tokens; self.selector = selector
    }

    // MARK: core request

    struct Empty: Encodable {}

    /// One request. Retries once on the internet route if the direct route stops
    /// answering mid-session, and once with a freshly read token if the host app
    /// rotated it under us (the old one stays valid 120 s, so this is rare).
    func raw(_ method: String, _ path: String, query: [URLQueryItem] = [], body: Data? = nil,
             contentType: String? = "application/json", headers: [String: String] = [:],
             auth: Bool = true, base: URL? = nil, timeout: TimeInterval = 60) async throws
        -> (Data, HTTPURLResponse)
    {
        if base == nil, let probe { await selector.ensureFresh(probe) }
        var triedPublic = base != nil
        var triedNewToken = false
        while true {
            guard let root = base ?? selector.baseURL else { throw GatewayError.notPaired }
            var comps = URLComponents(url: root.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
            if !query.isEmpty { comps.queryItems = query }
            var r = URLRequest(url: comps.url!)
            r.httpMethod = method
            r.timeoutInterval = timeout
            r.cachePolicy = .reloadIgnoringLocalCacheData
            if let body { r.httpBody = body; if let contentType { r.setValue(contentType, forHTTPHeaderField: "Content-Type") } }
            for (k, v) in headers { r.setValue(v, forHTTPHeaderField: k) }
            var sentToken: String?
            if auth {
                guard let t = tokens.load() else { throw GatewayError.notSignedIn }
                sentToken = t.token
                r.setValue("Bearer \(t.token)", forHTTPHeaderField: "Authorization")
            }
            let data: Data, resp: HTTPURLResponse
            do {
                (data, resp) = try await transport.send(r)
            } catch let e as GatewayError {
                throw e
            } catch {
                if Task.isCancelled { throw CancellationError() }
                if !triedPublic, selector.current == .direct {
                    selector.directFailed()
                    triedPublic = true
                    if selector.hasPublicRoute { continue }   // home only: nowhere else to go
                }
                throw GatewayError.unreachable(String(describing: error))
            }
            if (200..<300).contains(resp.statusCode) { return (data, resp) }
            let decoded = try? decoder.decode(APIError.self, from: data)
            // Cloudflare in front of a dead tunnel answers 502/521/523/530 with HTML:
            // that is "no answer from home", not a gateway verdict.
            if decoded == nil, resp.statusCode >= 500 {
                throw GatewayError.unreachable("edge \(resp.statusCode)")
            }
            var e = decoded ?? APIError(error: "http_\(resp.statusCode)")
            e.status = resp.statusCode
            if resp.statusCode == 401, e.error == "invalid_token", !triedNewToken,
               let fresh = tokens.load(), fresh.token != sentToken {
                triedNewToken = true
                continue
            }
            throw GatewayError.api(e)
        }
    }

    func call<T: Decodable>(_ type: T.Type, _ method: String, _ path: String,
                            query: [URLQueryItem] = [], json: (some Encodable)? = Empty?.none,
                            auth: Bool = true, base: URL? = nil, timeout: TimeInterval = 60) async throws -> T {
        let body = try json.map { try encoder.encode($0) }
        let (data, resp) = try await raw(method, path, query: query, body: body, auth: auth,
                                         base: base, timeout: timeout)
        do { return try decoder.decode(T.self, from: data) } catch {
            throw GatewayError.badResponse(resp.statusCode, "\(T.self): \(error)")
        }
    }

    // MARK: unauthenticated

    /// Pins over a tunnel's WebPKI route when the pairing has one (it still works after a
    /// key rotation this device missed); otherwise over the current pinned route, which the
    /// live + standby pins keep open across one rotation.
    public func identity() async throws -> Identity {
        guard let base = selector.identityURL else { throw GatewayError.notPaired }
        return try await call(Identity.self, "GET", "v1/identity", auth: false, base: base, timeout: 10)
    }

    // MARK: auth

    private struct LoginBody: Encodable { let user: String; let password: String; let device_name: String }

    /// Pinned listeners only (RouteConfig.signInURL): the home address, or a public address
    /// in `direct` mode. A tunnel never sees the password, and it is never retried on
    /// another route. The password is sent once and dropped; only the returned token is stored.
    public func login(user: String, password: String, deviceName: String) async throws -> LoginResult {
        guard let base = selector.signInURL else { throw GatewayError.notPaired }
        return try await call(LoginResult.self, "POST", "v1/auth/login",
                              json: LoginBody(user: user, password: password, device_name: deviceName),
                              auth: false, base: base, timeout: 30)
    }

    private struct CodeBody: Encodable { let code: String; let device_name: String }

    /// Sign in with a code made on the LAN by a device of an account the gateway allows
    /// codes for (e.g. an app store reviewer's account). Works on either route, so it goes to the one
    /// the selector picked.
    public func codeLogin(code: String, deviceName: String) async throws -> LoginResult {
        try await call(LoginResult.self, "POST", "v1/auth/code",
                       json: CodeBody(code: code, device_name: deviceName), auth: false, timeout: 30)
    }

    /// Rotate the device token; stores the new one before returning it.
    @discardableResult
    public func rotateToken() async throws -> StoredToken {
        let r = try await call(RefreshResult.self, "POST", "v1/auth/refresh")
        let t = StoredToken(r)
        try tokens.save(t)
        return t
    }

    public func devices() async throws -> DevicesResponse {
        try await call(DevicesResponse.self, "GET", "v1/auth/devices")
    }

    private struct OK: Decodable { let ok: Bool }

    public func revoke(deviceId: String) async throws {
        _ = try await call(OK.self, "DELETE", "v1/auth/device/\(deviceId)")
    }

    // MARK: roots

    public func me() async throws -> Me { try await call(Me.self, "GET", "v1/me") }

    public func shares() async throws -> [String] {
        try await call(SharesResponse.self, "GET", "v1/roots").shares
    }

    public func folders(share: String, path: String) async throws -> FoldersResponse {
        try await call(FoldersResponse.self, "GET", "v1/roots",
                       query: [URLQueryItem(name: "share", value: share), URLQueryItem(name: "path", value: path)])
    }

    private struct RootBody: Encodable { let share: String; let path: String }

    public func setRoot(share: String, path: String) async throws -> Me {
        try await call(Me.self, "PUT", "v1/me/root", json: RootBody(share: share, path: path))
    }

    // MARK: items

    public func item(_ id: String) async throws -> Item { try await call(Item.self, "GET", "v1/items/\(id)") }

    public func children(_ id: String, cursor: String?, limit: Int = 500) async throws -> ChildrenPage {
        var q = [URLQueryItem(name: "limit", value: String(limit))]
        if let cursor { q.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await call(ChildrenPage.self, "GET", "v1/items/\(id)/children", query: q, timeout: 120)
    }

    public func changes(anchor: String, wait: Int = 0, limit: Int = 500) async throws -> ChangesPage {
        try await call(ChangesPage.self, "GET", "v1/changes",
                       query: [URLQueryItem(name: "anchor", value: anchor),
                               URLQueryItem(name: "wait", value: String(wait)),
                               URLQueryItem(name: "limit", value: String(limit))],
                       timeout: TimeInterval(wait) + 30)
    }

    /// A JPEG or PNG of the file, at most `px` pixels on its longest side, rendered on the
    /// gateway. nil when it has none for this kind of file (or this file won't decode).
    public func thumbnail(_ id: String, px: Int) async throws -> Data? {
        do {
            let (data, _) = try await raw("GET", "v1/items/\(id)/thumbnail",
                                          query: [URLQueryItem(name: "px", value: String(px))],
                                          timeout: 45)
            return data
        } catch GatewayError.api(let e) where e.error == "no_thumbnail" {
            return nil
        }
    }

    public func latestAnchor() async throws -> String {
        try await call(AnchorResponse.self, "GET", "v1/changes/latest").anchor
    }

    private struct FolderBody: Encodable { let parent_id: String; let name: String }

    public func mkdir(parentId: String, name: String) async throws -> Item {
        try await call(Item.self, "POST", "v1/folders", json: FolderBody(parent_id: parentId, name: name))
    }

    private struct PatchBody: Encodable { let name: String?; let parent_id: String?; let if_match: String? }

    public func patch(_ id: String, name: String? = nil, parentId: String? = nil,
                      ifMatch: String? = nil) async throws -> Item {
        try await call(Item.self, "PATCH", "v1/items/\(id)",
                       json: PatchBody(name: name, parent_id: parentId, if_match: ifMatch))
    }

    public func delete(_ id: String, ifMatch: String? = nil) async throws -> DeleteResponse {
        try await call(DeleteResponse.self, "DELETE", "v1/items/\(id)",
                       query: ifMatch.map { [URLQueryItem(name: "if_match", value: $0)] } ?? [])
    }

    // MARK: upload (docs/design.md §D.5)

    /// `near`: the parent (create) or the item (modify). In an all-shares root it picks
    /// the share the temp file goes in, so the commit is a rename, not a copy.
    public func uploadCreate(size: Int64, sha256: String, near: String? = nil) async throws -> UploadState {
        struct B: Encodable { let size: Int64; let sha256: String; let near_id: String? }
        return try await call(UploadState.self, "POST", "v1/uploads",
                              json: B(size: size, sha256: sha256, near_id: near))
    }

    public func uploadStatus(_ id: String) async throws -> UploadState {
        try await call(UploadState.self, "GET", "v1/uploads/\(id)")
    }

    public func uploadChunk(_ id: String, offset: Int64, data: Data) async throws -> UploadState {
        let (d, _) = try await raw("PUT", "v1/uploads/\(id)",
                                   query: [URLQueryItem(name: "offset", value: String(offset))],
                                   body: data, contentType: "application/octet-stream",
                                   headers: ["X-Chunk-Sha256": Hasher256.hex(of: data)], timeout: 300)
        return try decoder.decode(UploadState.self, from: d)
    }

    public func commit(_ uploadId: String, _ req: CommitRequest) async throws -> CommitResponse {
        // The gateway re-reads the whole file from the NAS before it answers (5 GB ≈ 40 s).
        try await call(CommitResponse.self, "POST", "v1/uploads/\(uploadId)/commit", json: req, timeout: 600)
    }

    /// Upload a local file to a finished, hash-checked upload session and return its id.
    /// Resumes: from a session the shared state remembers for this content, from the
    /// gateway's `received` after a 409 wrong_offset, and after transport errors.
    public func upload(file url: URL, near: String? = nil, shared: SharedState? = nil,
                       progress: (@Sendable (Int64, Int64) -> Void)? = nil) async throws -> String {
        let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        let size = (attrs[.size] as? NSNumber)?.int64Value ?? 0
        let sha = try Hasher256.hex(ofFile: url)
        var state: UploadState?
        if let id = shared?.resumableUpload(sha256: sha, size: size) {
            state = try? await uploadStatus(id)
        }
        if state == nil { state = try await uploadCreate(size: size, sha256: sha, near: near) }
        var st = state!
        shared?.setResumableUpload(st.uploadId, sha256: sha, size: size)
        let chunk = min(st.chunkMax ?? chunkLimit, chunkLimit)
        let fh = try FileHandle(forReadingFrom: url)
        defer { try? fh.close() }
        var offset = st.received
        var failures = 0
        var recreated = false
        while offset < size {
            try Task.checkCancellation()
            try fh.seek(toOffset: UInt64(offset))
            let data = try fh.read(upToCount: Int(min(chunk, size - offset))) ?? Data()
            if data.isEmpty { throw GatewayError.changedDuringTransfer }   // file shrank under us
            do {
                st = try await uploadChunk(st.uploadId, offset: offset, data: data)
                offset = st.received
                failures = 0
                progress?(offset, size)
            } catch GatewayError.api(let e) where e.error == "wrong_offset" && e.received != nil {
                offset = e.received!
            } catch GatewayError.api(let e) where e.error == "no_such_upload" && !recreated {
                recreated = true                     // expired (24 h) or gateway rebuilt
                st = try await uploadCreate(size: size, sha256: sha, near: near)
                shared?.setResumableUpload(st.uploadId, sha256: sha, size: size)
                offset = 0
            } catch GatewayError.unreachable(let why) {
                guard failures < retryDelays.count else { throw GatewayError.unreachable(why) }
                try await Task.sleep(nanoseconds: retryDelays[failures] * 1_000_000_000)
                failures += 1
                if let s = try? await uploadStatus(st.uploadId) { offset = s.received }
            }
        }
        return st.uploadId
    }

    /// Upload + commit in one go; forgets the resumable session once committed.
    public func uploadAndCommit(file url: URL, _ req: CommitRequest, shared: SharedState? = nil,
                                progress: (@Sendable (Int64, Int64) -> Void)? = nil) async throws -> CommitResponse {
        let id = try await upload(file: url, near: req.itemId ?? req.parentId, shared: shared,
                                  progress: progress)
        do {
            let r = try await commit(id, req)
            forgetResume(id, shared)
            return r
        } catch GatewayError.api(let e) where e.error == "file_hash_mismatch" || e.error == "no_such_upload" {
            forgetResume(id, shared)
            throw GatewayError.api(e)
        }
    }

    private func forgetResume(_ id: String, _ shared: SharedState?) {
        guard let shared else { return }
        // We only know the id here; the key is content-based, so recompute is not worth it:
        // drop every entry pointing at this id.
        if var r = shared.defaults.dictionary(forKey: "uploadResume") as? [String: String] {
            r = r.filter { $0.value != id }
            shared.defaults.set(r, forKey: "uploadResume")
        }
    }

    // MARK: download (Range, sliced)

    /// Download an item's content into `dest` (created/truncated) in Range slices, so a
    /// dropped connection resumes from the last byte written. Returns the content etag
    /// the bytes belong to ("c<N>"). If the NAS copy changes mid-way it starts over (max 3).
    @discardableResult
    public func download(_ id: String, to dest: URL,
                         progress: (@Sendable (Int64, Int64) -> Void)? = nil) async throws -> String {
        var restarts = 0
        while true {
            if let tag = try await downloadOnce(id, to: dest, progress: progress) { return tag }
            restarts += 1
            if restarts >= 3 { throw GatewayError.changedDuringTransfer }
        }
    }

    /// One Range read into memory, for a player that asks for byte ranges (iPhone app).
    /// `total` is the file size, `start` where the bytes begin (0 if the gateway sent the
    /// whole file), `etag` the content version they belong to.
    public func rangeData(_ id: String, offset: Int64, length: Int64) async throws
        -> (data: Data, etag: String, total: Int64, start: Int64) {
        let (data, resp) = try await raw("GET", "v1/items/\(id)/content",
                                         headers: ["Range": "bytes=\(offset)-\(offset + max(length, 1) - 1)"],
                                         timeout: 120)
        let tag = (resp.value(forHTTPHeaderField: "ETag") ?? "")
            .trimmingCharacters(in: CharacterSet(charactersIn: "\""))
        if resp.statusCode == 206, let cr = resp.value(forHTTPHeaderField: "Content-Range"),
           let slash = cr.lastIndex(of: "/"), let t = Int64(cr[cr.index(after: slash)...]) {
            return (data, tag, t, offset)
        }
        return (data, tag, Int64(data.count), 0)
    }

    /// One Range read for partial materialisation (a player reading part of a video): the
    /// bytes from `offset`, at most `length` of them, written at the same offset in `dest`
    /// (created if missing; the rest of the file stays sparse). Returns the content etag
    /// the bytes belong to, the file's size, and where the bytes landed: `start` is 0 when
    /// the gateway sent the whole file instead. An empty file comes back as total 0.
    public func downloadRange(_ id: String, offset: Int64, length: Int64, into dest: URL)
        async throws -> (etag: String, total: Int64, start: Int64, count: Int64) {
        var headers = ["Range": "bytes=\(offset)-\(offset + max(length, 1) - 1)"]
        var failures = 0
        while true {
            try Task.checkCancellation()
            let data: Data, resp: HTTPURLResponse
            do {
                (data, resp) = try await raw("GET", "v1/items/\(id)/content", headers: headers, timeout: 300)
            } catch GatewayError.unreachable(let why) {
                guard failures < retryDelays.count else { throw GatewayError.unreachable(why) }
                try await Task.sleep(nanoseconds: retryDelays[failures] * 1_000_000_000)
                failures += 1
                continue
            } catch GatewayError.api(let e) where e.status == 416 && offset == 0 && !headers.isEmpty {
                headers = [:]                        // empty file: ask again without a Range
                continue
            } catch GatewayError.api(let e) where e.status == 416 {
                throw GatewayError.changedDuringTransfer   // shorter on the NAS than we were told
            }
            let tag = (resp.value(forHTTPHeaderField: "ETag") ?? "")
                .trimmingCharacters(in: CharacterSet(charactersIn: "\""))
            var start = offset, total = Int64(data.count)
            if resp.statusCode == 206, let cr = resp.value(forHTTPHeaderField: "Content-Range"),
               let slash = cr.lastIndex(of: "/"), let t = Int64(cr[cr.index(after: slash)...]) {
                total = t
            } else {
                start = 0                            // 200: the whole file
            }
            if !FileManager.default.fileExists(atPath: dest.path) {
                guard FileManager.default.createFile(atPath: dest.path, contents: nil) else {
                    throw CocoaError(.fileWriteUnknown)
                }
            }
            let fh = try FileHandle(forWritingTo: dest)
            defer { try? fh.close() }
            try fh.seek(toOffset: UInt64(start))
            try fh.write(contentsOf: data)
            return (tag, total, start, Int64(data.count))
        }
    }

    /// nil = the content changed on the NAS between slices; the caller starts over.
    private func downloadOnce(_ id: String, to dest: URL,
                              progress: (@Sendable (Int64, Int64) -> Void)?) async throws -> String? {
        guard FileManager.default.createFile(atPath: dest.path, contents: nil) else {
            throw CocoaError(.fileWriteUnknown)
        }
        let fh = try FileHandle(forWritingTo: dest)
        defer { try? fh.close() }
        var offset: Int64 = 0
        var total: Int64?
        var etag: String?
        var failures = 0
        var askWhole = false                         // after a 416 at 0: an empty file, ask without a Range
        while askWhole || total == nil || offset < total! {
            try Task.checkCancellation()
            let headers = askWhole ? [:] : ["Range": "bytes=\(offset)-\(offset + downloadSlice - 1)"]
            let data: Data, resp: HTTPURLResponse
            do {
                (data, resp) = try await raw("GET", "v1/items/\(id)/content", headers: headers, timeout: 300)
            } catch GatewayError.unreachable(let why) {
                guard failures < retryDelays.count else { throw GatewayError.unreachable(why) }
                try await Task.sleep(nanoseconds: retryDelays[failures] * 1_000_000_000)
                failures += 1
                continue
            } catch GatewayError.api(let e) where e.status == 416 {
                if offset > 0 || askWhole { return nil }   // shrank on the NAS since the first slice
                askWhole = true                      // empty file: ask again without a Range
                continue
            }
            failures = 0
            askWhole = false
            let tag = (resp.value(forHTTPHeaderField: "ETag") ?? "")
                .trimmingCharacters(in: CharacterSet(charactersIn: "\""))
            if let etag, tag != etag { return nil }
            etag = tag
            if resp.statusCode == 206, let cr = resp.value(forHTTPHeaderField: "Content-Range"),
               let slash = cr.lastIndex(of: "/"), let t = Int64(cr[cr.index(after: slash)...]) {
                total = t
            } else {
                // 200: the gateway sent the whole file; start over the write from zero.
                try fh.truncate(atOffset: 0)
                offset = 0
                total = Int64(data.count)
            }
            try fh.write(contentsOf: data)
            offset += Int64(data.count)
            progress?(offset, total!)
            if data.isEmpty { break }
        }
        return etag ?? ""
    }
}
