// NSFileProviderReplicatedExtension against gateway v0 (docs/design.md §D).
//
//   item(for:)              GET /v1/items/{id}            (.rootContainer = root_item_id)
//   enumerator              GET /v1/items/{id}/children   (pages); .workingSet = change feed
//   enumerateChanges        GET /v1/changes?anchor=        (410 -> .syncAnchorExpired)
//   fetchContents           GET /v1/items/{id}/content    (Range slices, resumable)
//   createItem / modifyItem POST /v1/uploads -> PUT chunks -> commit (CAS, conflict copies)
//   rename / move           PATCH /v1/items/{id}
//   deleteItem              DELETE /v1/items/{id}?if_match=  (gateway trash, 30 days)
//   fetchPartialContents    GET /v1/items/{id}/content, one Range of >= 8 MiB (streaming)
//   fetchThumbnails         GET /v1/items/{id}/thumbnail?px= (404 no_thumbnail = icon)
//   performAction           "Stream in VLC": hands the ids to the app (StreamLink)
//
// The host app long-polls the feed and calls signalEnumerator(for: .workingSet); this
// process never waits on the feed. It never writes the token either (only re-reads it).
#if os(macOS)
import AppKit
#endif
import FileProvider
import UniformTypeIdentifiers
import os
import LoftCore

private let log = Logger(subsystem: "no.incremental.loft", category: "fileprovider")

final class FileProviderExtension: NSObject, NSFileProviderReplicatedExtension, @unchecked Sendable {
    let domain: NSFileProviderDomain
    let env: LoftEnvironment
    let mirror: Mirror?
    private var cachedRoot: String?
    private let lock = NSLock()

    var client: GatewayClient { env.client }
    var shared: SharedState { env.shared }

    required init(domain: NSFileProviderDomain) {
        self.domain = domain
        env = LoftEnvironment()
        mirror = Mirror(domainId: domain.identifier.rawValue)
        super.init()
        env.shared.resetTransfers()
        log.info("extension up for domain \(domain.identifier.rawValue, privacy: .public)")
    }

    func invalidate() {}

    // MARK: helpers

    /// The gateway id of the chosen root. Shared state first (the app writes it when the
    /// folder is chosen), /v1/me if the extension starts before that is visible.
    func rootId() async throws -> String {
        if let r = lock.withLock({ cachedRoot }) { return r }
        var r = shared.root?.rootItemId
        if r == nil { r = try await client.me().root?.rootItemId }
        guard let r else { throw NSFileProviderError(.notAuthenticated) }
        lock.withLock { cachedRoot = r }
        return r
    }

    func gid(_ id: NSFileProviderItemIdentifier) async throws -> String {
        switch id {
        case .rootContainer: return try await rootId()
        case .trashContainer, .workingSet: throw NSFileProviderError(.noSuchItem)
        default: return id.rawValue
        }
    }

    func wrap(_ it: Item) async throws -> LoftItem { LoftItem(it, root: try await rootId()) }

    /// Runs `work` and hands its result to `done` exactly once; cancelling the Progress
    /// cancels the work (URLSession requests included).
    @discardableResult
    func run<T>(_ progress: Progress, _ what: String, _ work: @escaping () async throws -> T,
                done: @escaping (Result<T, Error>) -> Void) -> Progress {
        let task = Task {
            do { done(.success(try await work())) } catch {
                log.error("\(what, privacy: .public) failed: \(String(describing: error), privacy: .public)")
                done(.failure(FileProviderExtension.fpError(error)))
            }
        }
        progress.cancellationHandler = { task.cancel() }
        return progress
    }

    /// Gateway/transport errors -> what the system knows how to act on.
    static func fpError(_ e: Error) -> Error {
        let mapped = mapError(e)
        if let f = mapped as? NSFileProviderError, f.code == .serverUnreachable {
            SharedState().noteUnreachableReported()       // the app clears it once home answers
        }
        return mapped
    }

    private static func mapError(_ e: Error) -> Error {
        if e is NSFileProviderError { return e }
        if e is CancellationError { return CocoaError(.userCancelled) }
        if e is URLError { return NSFileProviderError(.serverUnreachable) }
        guard let g = e as? GatewayError else { return e }
        switch g {
        case .unreachable, .changedDuringTransfer: return NSFileProviderError(.serverUnreachable)
        case .notSignedIn, .notPaired: return NSFileProviderError(.notAuthenticated)
        case .badResponse: return NSFileProviderError(.cannotSynchronize)
        case .api(let a):
            if a.needsSignIn { return NSFileProviderError(.notAuthenticated) }
            if a.isNASUnreachable { return NSFileProviderError(.serverUnreachable) }
            switch a.error {
            case "not_found", "root_missing": return NSFileProviderError(.noSuchItem)
            case "name_exists", "name_exists_on_nas": return NSFileProviderError(.filenameCollision)
            case "invalid_or_excluded_name": return NSFileProviderError(.excludedFromSync)
            case "no_root_selected": return NSFileProviderError(.notAuthenticated)
            case "access_denied", "share_is_fixed", "share_blocked": return CocoaError(.fileWriteNoPermission)
            case "upload_incomplete", "file_hash_mismatch", "chunk_hash_mismatch", "no_such_upload":
                return NSFileProviderError(.serverUnreachable)        // the system retries
            default: return NSFileProviderError(.cannotSynchronize)
            }
        }
    }

    // MARK: items

    func item(for identifier: NSFileProviderItemIdentifier, request: NSFileProviderRequest,
              completionHandler: @escaping (NSFileProviderItem?, Error?) -> Void) -> Progress {
        run(Progress(totalUnitCount: 1), "item") { [self] () -> LoftItem in
            let it = try await client.item(try await gid(identifier))
            mirror?.upsert([it])
            return try await wrap(it)
        } done: { r in
            switch r {
            case .success(let i): completionHandler(i, nil)
            case .failure(let e): completionHandler(nil, e)
            }
        }
    }

    func fetchContents(for itemIdentifier: NSFileProviderItemIdentifier,
                       version requestedVersion: NSFileProviderItemVersion?,
                       request: NSFileProviderRequest,
                       completionHandler: @escaping (URL?, NSFileProviderItem?, Error?) -> Void) -> Progress {
        let progress = Progress(totalUnitCount: 100)
        return run(progress, "fetchContents") { [self] () -> (URL, LoftItem) in
            let id = try await gid(itemIdentifier)
            let meta = try await client.item(id)
            guard !meta.isDirectory else { throw NSFileProviderError(.noSuchItem) }
            guard let mgr = NSFileProviderManager(for: domain) else { throw NSFileProviderError(.providerNotFound) }
            let tmp = try mgr.temporaryDirectoryURL().appendingPathComponent(UUID().uuidString)
            progress.totalUnitCount = max(meta.size, 1)
            shared.transferStarted()
            defer { shared.transferEnded() }
            let tag = try await client.download(id, to: tmp) { done, total in
                progress.totalUnitCount = max(total, 1)
                progress.completedUnitCount = done
            }
            let size = (try? FileManager.default.attributesOfItem(atPath: tmp.path)[.size] as? NSNumber)?
                .int64Value ?? meta.size
            let got = Self.versionOfBytes(meta, etag: tag, size: size)
            mirror?.upsert([got])
            return (tmp, try await wrap(got))
        } done: { r in
            switch r {
            case .success(let (url, item)): completionHandler(url, item, nil)
            case .failure(let e): completionHandler(nil, nil, e)
            }
        }
    }

    /// Report the version the bytes ARE, not the one we asked for: if the NAS copy moved
    /// on between the metadata call and the content call, the system must not file new
    /// bytes under the old version (or the reverse).
    static func versionOfBytes(_ meta: Item, etag: String, size: Int64) -> Item {
        etag == meta.contentEtag ? meta
            : Item(id: meta.id, parentId: meta.parentId, name: meta.name, type: meta.type, size: size,
                   mtimeNs: meta.mtimeNs, contentEtag: etag, metaEtag: meta.metaEtag, sha256: nil)
    }

    // MARK: writes

    func createItem(basedOn itemTemplate: NSFileProviderItem, fields: NSFileProviderItemFields,
                    contents url: URL?, options: NSFileProviderCreateItemOptions = [],
                    request: NSFileProviderRequest,
                    completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void) -> Progress {
        let name = itemTemplate.filename
        let type = itemTemplate.contentType ?? .data
        // Office/Finder scratch stays on this Mac (docs/design.md §D.3). Packages and symlinks
        // too: the gateway stores plain files and folders only (v0).
        if Exclusions.isExcluded(name) || type.conforms(to: .symbolicLink)
            || (type.conforms(to: .package) && !type.conforms(to: .folder) && url.map { self.isDirectory($0) } == true) {
            completionHandler(nil, [], false, NSFileProviderError(.excludedFromSync))
            return Progress(totalUnitCount: 0)
        }
        let progress = Progress(totalUnitCount: 100)
        return run(progress, "createItem") { [self] () -> LoftItem in
            let parent = try await gid(itemTemplate.parentItemIdentifier)
            if type.conforms(to: .folder) {
                let it: Item
                do {
                    it = try await client.mkdir(parentId: parent, name: name)
                } catch GatewayError.api(let e) where e.error == "name_exists" && e.existingId != nil {
                    it = try await client.item(e.existingId!)   // same folder made elsewhere: merge
                    guard it.isDirectory else { throw NSFileProviderError(.filenameCollision) }
                }
                mirror?.upsert([it])
                return try await wrap(it)
            }
            let file = try url ?? emptyFile()
            // Re-import after a domain reset: the same bytes are already on the NAS.
            if options.contains(.mayAlreadyExist),
               let ex = try await findChild(parent, name), !ex.isDirectory,
               let sha = ex.sha256, sha == (try? Hasher256.hex(ofFile: file)) {
                mirror?.upsert([ex])
                return try await wrap(ex)
            }
            var req = CommitRequest(parentId: parent, name: name)
            if let x = mirror?.live(parent: parent, name: name), !x.isDir {
                req.replaces = x.id                  // safe-save, create-before-delete (docs/design.md §D.3)
                req.ifMatch = x.contentEtag
            }
            let r = try await upload(file, req, progress)
            log.info("create \(r.result.rawValue, privacy: .public) \(r.item.id, privacy: .public)")
            if let old = r.replacedId { mirror?.markDeleted([old]) }
            return try await wrap(r.item)            // a conflict copy comes back renamed
        } done: { r in
            switch r {
            case .success(let i): completionHandler(i, [], false, nil)
            case .failure(let e): completionHandler(nil, [], false, e)
            }
        }
    }

    func modifyItem(_ item: NSFileProviderItem, baseVersion version: NSFileProviderItemVersion,
                    changedFields: NSFileProviderItemFields, contents newContents: URL?,
                    options: NSFileProviderModifyItemOptions = [], request: NSFileProviderRequest,
                    completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void) -> Progress {
        let progress = Progress(totalUnitCount: 100)
        return run(progress, "modifyItem") { [self] () -> (LoftItem, Bool) in
            let id = try await gid(item.itemIdentifier)
            var current: Item?
            if changedFields.contains(.filename) || changedFields.contains(.parentItemIdentifier) {
                let newName = changedFields.contains(.filename) ? item.filename : nil
                let newParent = changedFields.contains(.parentItemIdentifier)
                    ? try await gid(item.parentItemIdentifier) : nil
                do {
                    current = try await client.patch(id, name: newName, parentId: newParent, ifMatch: version.metaEtag)
                } catch GatewayError.api(let e) where e.status == 412 {
                    // Renamed elsewhere since: names are last-writer-wins (content is CAS).
                    current = try await client.patch(id, name: newName, parentId: newParent)
                }
                mirror?.upsert([current!])
            }
            if changedFields.contains(.contents), let file = newContents {
                let r = try await upload(file, CommitRequest(itemId: id, ifMatch: version.contentEtag), progress)
                log.info("modify \(r.result.rawValue, privacy: .public) \(id, privacy: .public)")
                if r.result == .conflict {
                    // The NAS copy changed since this Mac's base version. Our bytes are now
                    // "Name (conflict, <Mac>, <time>)" (arrives through the feed); this item
                    // keeps the NAS content, so the system must fetch it again.
                    let nas = try await client.item(id)
                    mirror?.upsert([nas])
                    return (try await wrap(nas), true)
                }
                current = r.item
            }
            // Other fields (dates, tags, xattrs, flags) are not stored by gateway v0;
            // acknowledge them so they don't stay pending forever.
            let final: Item
            if let current { final = current } else { final = try await client.item(id) }
            return (try await wrap(final), false)
        } done: { r in
            switch r {
            case .success(let (i, refetch)): completionHandler(i, [], refetch, nil)
            case .failure(let e): completionHandler(nil, [], false, e)
            }
        }
    }

    func deleteItem(identifier: NSFileProviderItemIdentifier, baseVersion version: NSFileProviderItemVersion,
                    options: NSFileProviderDeleteItemOptions = [], request: NSFileProviderRequest,
                    completionHandler: @escaping (Error?) -> Void) -> Progress {
        run(Progress(totalUnitCount: 1), "deleteItem") { [self] () -> Void in
            let id = try await gid(identifier)
            var isDir = mirror?.isDirectory(id)
            if isDir == nil { isDir = (try? await client.item(id))?.isDirectory }
            if isDir == true, !options.contains(.recursive),
               let kids = try? await client.children(id, cursor: nil, limit: 1), !kids.items.isEmpty {
                // The NAS has children this Mac doesn't know about: never take them along.
                throw NSFileProviderError(.directoryNotEmpty)
            }
            do {
                let r = try await client.delete(id, ifMatch: isDir == true ? nil : version.contentEtag)
                log.info("delete \(r.result, privacy: .public) \(id, privacy: .public)")
            } catch GatewayError.api(let e) where e.error == "not_found" {
                // already gone
            } catch GatewayError.api(let e) where e.status == 412 {
                // Changed on the NAS after this Mac last saw it: keep the newer content.
                throw NSFileProviderError(.deletionRejected)
            }
            mirror?.markDeleted([id])
        } done: { r in
            switch r {
            case .success: completionHandler(nil)
            case .failure(let e): completionHandler(e)
            }
        }
    }

    // MARK: enumeration

    func enumerator(for containerItemIdentifier: NSFileProviderItemIdentifier,
                    request: NSFileProviderRequest) throws -> NSFileProviderEnumerator {
        switch containerItemIdentifier {
        case .trashContainer: throw NSFileProviderError(.noSuchItem)
        case .workingSet: return Enumerator(ext: self, folder: nil)
        default: return Enumerator(ext: self, folder: containerItemIdentifier)
        }
    }

    // MARK: upload plumbing

    private func upload(_ file: URL, _ req: CommitRequest, _ progress: Progress) async throws -> CommitResponse {
        shared.transferStarted()
        defer { shared.transferEnded() }
        let r = try await client.uploadAndCommit(file: file, req, shared: shared) { done, total in
            progress.totalUnitCount = max(total, 1)
            progress.completedUnitCount = done
        }
        mirror?.upsert([r.item])
        // Acknowledged by the gateway: from here the app may evict the local copy (docs/design.md §E.1).
        if r.result != .conflict || req.itemId == nil { shared.journalUploaded(r.item.id) }
        return r
    }

    private func findChild(_ parent: String, _ name: String) async throws -> Item? {
        let key = Mirror.nameKey(name)
        var cursor: String?
        repeat {
            let p = try await client.children(parent, cursor: cursor, limit: 2000)
            if let hit = p.items.first(where: { Mirror.nameKey($0.name) == key }) { return hit }
            cursor = p.nextCursor
        } while cursor != nil
        return nil
    }

    private func emptyFile() throws -> URL {
        guard let mgr = NSFileProviderManager(for: domain) else { throw NSFileProviderError(.providerNotFound) }
        let u = try mgr.temporaryDirectoryURL().appendingPathComponent(UUID().uuidString)
        try Data().write(to: u)
        return u
    }

    private func isDirectory(_ url: URL) -> Bool {
        (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true
    }
}

// MARK: partial reads (streaming)

/// Without this, any read of a file that isn't downloaded makes the system fetch the whole
/// file first (fetchContents), so a 2 GB video plays only after 2 GB came over. With it, a
/// read fetches the part around what was asked for; the rest of the file stays sparse and
/// is fetched when something reads it. Finder's "Download Now" still uses fetchContents.
#if os(macOS)   // the protocol is macOS-only; iOS apps read files whole
extension FileProviderExtension: NSFileProviderPartialContentFetching {
    /// At least this much per request, so a player reading on gets a few seconds of video
    /// per round trip, and a file smaller than this comes whole in one request.
    static let partialChunk: Int64 = 8 << 20

    func fetchPartialContents(for itemIdentifier: NSFileProviderItemIdentifier,
                              version requestedVersion: NSFileProviderItemVersion,
                              request: NSFileProviderRequest,
                              minimalRange requestedRange: NSRange,
                              aligningTo alignment: Int,
                              options: NSFileProviderFetchContentsOptions = [],
                              completionHandler: @escaping (URL?, NSFileProviderItem?, NSRange,
                                                            NSFileProviderMaterializationFlags, Error?) -> Void) -> Progress {
        let align = Int64(max(alignment, 1))
        let start = Int64(requestedRange.location) / align * align
        var end = max(Int64(requestedRange.location + requestedRange.length), start + Self.partialChunk)
        end = (end + align - 1) / align * align          // the gateway cuts it at EOF
        let progress = Progress(totalUnitCount: end - start)
        return run(progress, "fetchPartialContents") { [self] () -> (URL, LoftItem, NSRange) in
            let id = try await gid(itemIdentifier)
            guard let mgr = NSFileProviderManager(for: domain) else { throw NSFileProviderError(.providerNotFound) }
            let tmp = try mgr.temporaryDirectoryURL().appendingPathComponent(UUID().uuidString)
            shared.transferStarted()
            defer { shared.transferEnded() }
            async let metaCall = client.item(id)
            let r = try await client.downloadRange(id, offset: start, length: end - start, into: tmp)
            let meta = try await metaCall
            guard !meta.isDirectory else { throw NSFileProviderError(.noSuchItem) }
            // Strict: the system already holds other parts of this version and would
            // otherwise mix two versions in one file.
            if options.contains(.strictVersioning), r.etag != requestedVersion.contentEtag {
                try? FileManager.default.removeItem(at: tmp)
                throw NSFileProviderError(.versionNoLongerAvailable)
            }
            progress.completedUnitCount = r.count
            let got = Self.versionOfBytes(meta, etag: r.etag, size: r.total)
            mirror?.upsert([got])
            log.debug("partial \(start)+\(end - start): got \(r.start)+\(r.count) of \(r.total)")
            return (tmp, try await wrap(got), NSRange(location: Int(r.start), length: Int(r.count)))
        } done: { r in
            switch r {
            case .success(let (url, item, range)): completionHandler(url, item, range, [], nil)
            case .failure(let e): completionHandler(nil, nil, requestedRange, [], e)
            }
        }
    }
}
#endif

// MARK: thumbnails

/// Finder asks here for files that aren't downloaded (a downloaded file it renders
/// itself). The gateway renders them in a sandbox, so only the small image crosses the
/// network: photos, PDFs, videos and the previews inside iWork/Office files. Anything
/// else (or an error) gets no thumbnail and Finder shows the file-type icon.
extension FileProviderExtension: NSFileProviderThumbnailing {
    func fetchThumbnails(for itemIdentifiers: [NSFileProviderItemIdentifier],
                         requestedSize size: CGSize,
                         perThumbnailCompletionHandler: @escaping (NSFileProviderItemIdentifier, Data?, Error?) -> Void,
                         completionHandler: @escaping (Error?) -> Void) -> Progress {
        let progress = Progress(totalUnitCount: Int64(itemIdentifiers.count))
        let px = min(max(Int(max(size.width, size.height).rounded(.up)), 16), 1024)
        log.debug("thumbnails: \(itemIdentifiers.count) at \(Int(size.width))x\(Int(size.height))")
        let task = Task { [self] in
            // Three at a time: the gateway decodes two at once, the third is in flight.
            await withTaskGroup(of: (NSFileProviderItemIdentifier, Data?, Error?).self) { group in
                var queue = itemIdentifiers[...]
                func add() {
                    guard let id = queue.popFirst() else { return }
                    group.addTask { [self] in
                        do { return (id, try await client.thumbnail(try await gid(id), px: px), nil) }
                        catch { return (id, nil, Task.isCancelled ? nil : FileProviderExtension.fpError(error)) }
                    }
                }
                for _ in 0..<3 { add() }
                while let (id, data, error) = await group.next() {
                    perThumbnailCompletionHandler(id, data, error)
                    progress.completedUnitCount += 1
                    if !Task.isCancelled { add() }
                }
            }
            completionHandler(Task.isCancelled ? CocoaError(.userCancelled) : nil)
        }
        progress.cancellationHandler = { task.cancel() }
        return progress
    }
}

#if os(macOS)
// MARK: custom actions (Finder's right-click menu)

/// Declared under NSExtensionFileProviderActions in project.yml, which also decides
/// which files get the item (video file names only).
extension FileProviderExtension: NSFileProviderCustomAction {
    func performAction(identifier actionIdentifier: NSFileProviderExtensionActionIdentifier,
                       onItemsWithIdentifiers itemIdentifiers: [NSFileProviderItemIdentifier],
                       completionHandler: @escaping (Error?) -> Void) -> Progress {
        let progress = Progress(totalUnitCount: 1)
        guard actionIdentifier.rawValue == StreamLink.action else {
            completionHandler(CocoaError(.featureUnsupported))
            return progress
        }
        guard let link = StreamLink.url(items: itemIdentifiers.map(\.rawValue)) else {
            completionHandler(NSFileProviderError(.noSuchItem))
            return progress
        }
        log.info("stream in VLC: \(itemIdentifiers.count) item(s)")
        let cfg = NSWorkspace.OpenConfiguration()
        cfg.activates = false                 // VLC comes forward; Loft has nothing to show
        NSWorkspace.shared.open(link, configuration: cfg) { _, error in
            if let error { log.error("stream in VLC: \(String(describing: error), privacy: .public)") }
            progress.completedUnitCount = 1
            completionHandler(error)
        }
        return progress
    }
}

#endif

/// Folder enumerator (children pages) and the working set (change feed). For a
/// replicated extension the changes are global, so both report the same feed.
final class Enumerator: NSObject, NSFileProviderEnumerator, @unchecked Sendable {
    let ext: FileProviderExtension
    let folder: NSFileProviderItemIdentifier?
    private var tasks: [Task<Void, Never>] = []
    private let lock = NSLock()

    init(ext: FileProviderExtension, folder: NSFileProviderItemIdentifier?) {
        self.ext = ext
        self.folder = folder
    }

    func invalidate() { lock.withLock { tasks.forEach { $0.cancel() }; tasks = [] } }

    private func spawn(_ body: @escaping @Sendable () async -> Void) {
        let t = Task { await body() }
        lock.withLock { tasks.append(t) }
    }

    private static let cursorPrefix = "loft-cursor:"

    func enumerateItems(for observer: NSFileProviderEnumerationObserver, startingAt page: NSFileProviderPage) {
        guard let folder else {
            // Working set: the system learns items from folder listings; the feed
            // (enumerateChanges) tells it about everything after that.
            observer.finishEnumerating(upTo: nil)
            return
        }
        let raw = String(data: page.rawValue, encoding: .utf8) ?? ""
        let cursor = raw.hasPrefix(Enumerator.cursorPrefix) ? String(raw.dropFirst(Enumerator.cursorPrefix.count)) : nil
        spawn { [ext] in
            do {
                let id = try await ext.gid(folder)
                let p = try await ext.client.children(id, cursor: cursor)
                ext.mirror?.upsert(p.items)
                let root = try await ext.rootId()
                observer.didEnumerate(p.items.map { LoftItem($0, root: root) })
                observer.finishEnumerating(upTo: p.nextCursor.map {
                    NSFileProviderPage(rawValue: Data((Enumerator.cursorPrefix + $0).utf8))
                })
            } catch {
                observer.finishEnumeratingWithError(FileProviderExtension.fpError(error))
            }
        }
    }

    func enumerateChanges(for observer: NSFileProviderChangeObserver, from anchor: NSFileProviderSyncAnchor) {
        let a = String(data: anchor.rawValue, encoding: .utf8) ?? ""
        spawn { [ext] in
            do {
                let p = try await ext.client.changes(anchor: a)
                let root = try await ext.rootId()
                ext.mirror?.upsert(p.updated)
                ext.mirror?.markDeleted(p.deleted)
                if !p.updated.isEmpty { observer.didUpdate(p.updated.map { LoftItem($0, root: root) }) }
                let gone = p.deleted.filter { $0 != root }.map { NSFileProviderItemIdentifier($0) }
                if !gone.isEmpty { observer.didDeleteItems(withIdentifiers: gone) }
                observer.finishEnumeratingChanges(upTo: NSFileProviderSyncAnchor(rawValue: Data(p.anchor.utf8)),
                                                  moreComing: p.more)
            } catch GatewayError.api(let e) where e.isAnchorExpired {
                observer.finishEnumeratingWithError(NSFileProviderError(.syncAnchorExpired))
            } catch {
                observer.finishEnumeratingWithError(FileProviderExtension.fpError(error))
            }
        }
    }

    func currentSyncAnchor(completionHandler: @escaping (NSFileProviderSyncAnchor?) -> Void) {
        spawn { [ext] in
            let a = try? await ext.client.latestAnchor()
            completionHandler(a.map { NSFileProviderSyncAnchor(rawValue: Data($0.utf8)) })
        }
    }
}
