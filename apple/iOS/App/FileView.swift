// Opening a file: video and audio stream (the player asks for byte ranges, nothing is
// stored); everything else is downloaded once into the cache and shown with Quick Look,
// whose share button covers "Open in…", AirDrop and printing.
import AVKit
import QuickLook
import SwiftUI
import UniformTypeIdentifiers
import LoftCore

struct FileView: View {
    let item: Item
    private var type: UTType? { UTType(filenameExtension: (item.name as NSString).pathExtension) }

    var body: some View {
        if let t = type, t.conforms(to: .audiovisualContent), StreamPlayer.canPlay(t) {
            StreamView(item: item, type: t)
        } else {
            DownloadedPreview(item: item)
        }
    }
}

// MARK: streaming

struct StreamView: View {
    @EnvironmentObject var model: PhoneModel
    let item: Item
    let type: UTType
    @State private var player: StreamPlayer?

    var body: some View {
        Group {
            if let player { PlayerController(player: player.player).ignoresSafeArea(edges: .bottom) }
            else { ProgressView() }
        }
        .navigationTitle(item.name).navigationBarTitleDisplayMode(.inline)
        .task {
            let p = StreamPlayer(item: item, type: type, client: model.client)
            player = p
            p.player.play()
        }
        .onDisappear { player?.player.pause() }
    }
}

struct PlayerController: UIViewControllerRepresentable {
    let player: AVPlayer
    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let vc = AVPlayerViewController(); vc.player = player; return vc
    }
    func updateUIViewController(_ vc: AVPlayerViewController, context: Context) {}
}

/// AVPlayer can't send our Authorization header, so the asset gets a private URL scheme
/// and this loader answers its byte-range requests from the gateway.
final class StreamPlayer: NSObject, AVAssetResourceLoaderDelegate, @unchecked Sendable {
    static let scheme = "loftstream"
    static let slice: Int64 = 4 << 20      // per gateway request; a few seconds of HD video
    let player: AVPlayer
    private let item: Item
    private let type: UTType
    private let client: GatewayClient
    private let queue = DispatchQueue(label: "loft.stream")
    private var tasks: [ObjectIdentifier: Task<Void, Never>] = [:]

    static func canPlay(_ t: UTType) -> Bool {
        AVURLAsset.audiovisualTypes().contains { AVFileType(rawValue: t.identifier) == $0 }
    }

    init(item: Item, type: UTType, client: GatewayClient) {
        self.item = item; self.type = type; self.client = client
        var c = URLComponents(); c.scheme = Self.scheme; c.host = item.id; c.path = "/" + item.name
        let asset = AVURLAsset(url: c.url!)
        player = AVPlayer()
        super.init()
        asset.resourceLoader.setDelegate(self, queue: queue)
        player.replaceCurrentItem(with: AVPlayerItem(asset: asset))
    }

    func resourceLoader(_ loader: AVAssetResourceLoader,
                        shouldWaitForLoadingOfRequestedResource req: AVAssetResourceLoadingRequest) -> Bool {
        if let info = req.contentInformationRequest {
            info.contentType = type.identifier
            info.contentLength = item.size
            info.isByteRangeAccessSupported = true
            if req.dataRequest == nil { req.finishLoading(); return true }
        }
        guard let dr = req.dataRequest else { return false }
        let start = dr.currentOffset != 0 ? dr.currentOffset : dr.requestedOffset
        let end = dr.requestsAllDataToEndOfResource ? item.size : min(item.size, dr.requestedOffset + Int64(dr.requestedLength))
        let key = ObjectIdentifier(req)
        tasks[key] = Task { [client, item] in
            var at = start
            do {
                while at < end, !Task.isCancelled {
                    let n = min(Self.slice, end - at)
                    let r = try await client.rangeData(item.id, offset: at, length: n)
                    let usable = r.start == at ? r.data : r.data.subdata(in: Int(at - r.start)..<min(r.data.count, Int(at - r.start + n)))
                    if usable.isEmpty { break }
                    dr.respond(with: usable)
                    at += Int64(usable.count)
                }
                if !Task.isCancelled { req.finishLoading() }
            } catch {
                if !Task.isCancelled { req.finishLoading(with: error) }
            }
            self.queue.async { self.tasks[key] = nil }
        }
        return true
    }

    func resourceLoader(_ loader: AVAssetResourceLoader, didCancel req: AVAssetResourceLoadingRequest) {
        tasks.removeValue(forKey: ObjectIdentifier(req))?.cancel()
    }
}

// MARK: download + Quick Look

enum OpenCache {
    static var dir: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("open", isDirectory: true)
    }
    /// One folder per item version, so the file keeps its own name for Quick Look and sharing.
    static func url(for item: Item) -> URL {
        dir.appendingPathComponent("\(item.id)-\(item.contentEtag)", isDirectory: true)
            .appendingPathComponent(item.name)
    }
}

struct DownloadedPreview: View {
    @EnvironmentObject var model: PhoneModel
    let item: Item
    @State private var local: URL?
    @State private var progress: Double = 0
    @State private var failed = false

    var body: some View {
        Group {
            if let local {
                QuickLookView(url: local).ignoresSafeArea(edges: .bottom)
            } else if failed {
                ContentUnavailableView("Couldn't open “\(item.name)”", systemImage: "wifi.exclamationmark",
                                       description: Text("Check your connection and try again."))
            } else {
                VStack(spacing: 12) {
                    ProgressView(value: progress).frame(maxWidth: 240)
                    Text("Downloading \(ByteCountFormatter.string(fromByteCount: item.size, countStyle: .file))")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle(item.name).navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if let local { ToolbarItem(placement: .topBarTrailing) { ShareLink(item: local) } }
        }
        .task { await fetch() }
    }

    private func fetch() async {
        let dest = OpenCache.url(for: item)
        if FileManager.default.fileExists(atPath: dest.path) { local = dest; return }
        do {
            try FileManager.default.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
            let part = dest.appendingPathExtension("part")
            try await model.client.download(item.id, to: part) { done, total in
                Task { @MainActor in progress = total > 0 ? Double(done) / Double(total) : 0 }
            }
            try FileManager.default.moveItem(at: part, to: dest)
            local = dest
        } catch {
            failed = true
        }
    }
}

struct QuickLookView: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> QLPreviewController {
        let c = QLPreviewController(); c.dataSource = context.coordinator; return c
    }
    func updateUIViewController(_ c: QLPreviewController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(url: url) }
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let url: URL
        init(url: URL) { self.url = url }
        func numberOfPreviewItems(in c: QLPreviewController) -> Int { 1 }
        func previewController(_ c: QLPreviewController, previewItemAt i: Int) -> QLPreviewItem { url as NSURL }
    }
}
