// "Save to Loft" in every app's share sheet: pick a folder (the last one is offered
// first), the shared files upload there, the sheet closes. Text and links that come
// without a file are saved as .txt / .webloc.
import SwiftUI
import UIKit
import UniformTypeIdentifiers
import LoftCore

final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        let providers = (extensionContext?.inputItems as? [NSExtensionItem] ?? [])
            .flatMap { $0.attachments ?? [] }
        let host = UIHostingController(rootView: ShareView(providers: providers) { [weak self] cancelled in
            if cancelled {
                self?.extensionContext?.cancelRequest(withError: CocoaError(.userCancelled))
            } else {
                self?.extensionContext?.completeRequest(returningItems: nil)
            }
        })
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
    }
}

struct ShareView: View {
    let providers: [NSItemProvider]
    let finish: (_ cancelled: Bool) -> Void
    private let env = LoftEnvironment()
    @State private var phase: Phase = .picking
    @State private var progress: Double = 0
    enum Phase: Equatable { case picking, saving(String), failed(String) }

    var body: some View {
        NavigationStack {
            Group {
                if env.tokens.load() == nil || env.shared.root == nil {
                    ContentUnavailableView("Not signed in", systemImage: "person.crop.circle.badge.exclamationmark",
                                           description: Text(PhoneCopy.notSignedIn))
                } else {
                    switch phase {
                    case .picking:
                        FolderPicker(env: env, title: String(localized: "Save to Loft"),
                                     action: String(localized: "Save here")) { folder in
                            Task { await save(to: folder) }
                        }
                    case .saving(let what):
                        VStack(spacing: 14) {
                            ProgressView(value: progress).frame(maxWidth: 260)
                            Text(what).font(.footnote).foregroundStyle(.secondary)
                        }
                    case .failed(let m):
                        ContentUnavailableView("Couldn't save", systemImage: "exclamationmark.triangle",
                                               description: Text(m))
                    }
                }
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { finish(true) } }
            }
        }
    }

    private func save(to folder: SavedFolder) async {
        phase = .saving(String(localized: "Preparing…"))
        let work = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: work) }
        do {
            try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
            var files: [(URL, String)] = []
            for (i, p) in providers.enumerated() {
                if let f = try await ShareView.materialise(p, into: work, index: i) { files.append(f) }
            }
            guard !files.isEmpty else { throw ShareFailure.nothing }
            let total = files.reduce(Int64(0)) { $0 + ((try? $1.0.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) ?? 0) }
            var before: Int64 = 0
            for (n, (url, name)) in files.enumerated() {
                phase = .saving(files.count == 1 ? String(localized: "Saving “\(name)” to \(folder.path)")
                                : String(localized: "Saving \(n + 1) of \(files.count) to \(folder.path)"))
                let base = before
                try await Phone.save(url, as: name, into: folder.id, env: env) { done, _ in
                    Task { @MainActor in progress = total > 0 ? Double(base + done) / Double(total) : 0 }
                }
                before += (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) ?? 0
            }
            Phone.lastFolder = folder
            finish(false)
        } catch ShareFailure.nothing {
            phase = .failed(String(localized: "There was nothing Loft could save."))
        } catch {
            phase = .failed(String(localized: "Check your connection and try again."))
        }
    }

    enum ShareFailure: Error { case nothing }

    /// One shared item -> (local file, name to save it as).
    static func materialise(_ p: NSItemProvider, into dir: URL, index: Int) async throws -> (URL, String)? {
        let fileTypes = p.registeredTypeIdentifiers.compactMap(UTType.init)
            .filter { !$0.conforms(to: .url) && !$0.conforms(to: .plainText) || $0.conforms(to: .fileURL) }
        if let t = fileTypes.first(where: { $0.conforms(to: .data) || $0.conforms(to: .package) }) ?? fileTypes.first {
            // Read the name here: the provider isn't Sendable, the completion block runs elsewhere.
            let suggested = p.suggestedName.map { n in
                guard (n as NSString).pathExtension.isEmpty, let ext = t.preferredFilenameExtension else { return n }
                return n + "." + ext
            }
            let url: URL? = try await withCheckedThrowingContinuation { cont in
                _ = p.loadFileRepresentation(forTypeIdentifier: t.identifier) { src, err in
                    guard let src else { return cont.resume(returning: nil) }
                    // The system deletes `src` when this block returns: copy it out now.
                    let name = Phone.safeName(suggested ?? src.lastPathComponent)
                    let dest = dir.appendingPathComponent("\(index)", isDirectory: true).appendingPathComponent(name)
                    do {
                        try FileManager.default.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
                        try FileManager.default.copyItem(at: src, to: dest)
                        cont.resume(returning: dest)
                    } catch { cont.resume(throwing: error) }
                }
            }
            if let url { return (url, url.lastPathComponent) }
        }
        if p.canLoadObject(ofClass: URL.self), let u = try? await load(URL.self, from: p), !u.isFileURL {
            let plist = try PropertyListSerialization.data(fromPropertyList: ["URL": u.absoluteString], format: .xml, options: 0)
            let name = Phone.safeName((u.host ?? "Link") + ".webloc")
            let dest = dir.appendingPathComponent("\(index)-\(name)")
            try plist.write(to: dest)
            return (dest, name)
        }
        if p.canLoadObject(ofClass: String.self), let s = try? await load(String.self, from: p) {
            let first = s.split(whereSeparator: \.isNewline).first.map(String.init) ?? "Note"
            let name = Phone.safeName(String(first.prefix(40))) + ".txt"
            let dest = dir.appendingPathComponent("\(index)-\(name)")
            try Data(s.utf8).write(to: dest)
            return (dest, name)
        }
        return nil
    }

    static func load<T: _ObjectiveCBridgeable>(_ type: T.Type, from p: NSItemProvider) async throws -> T?
        where T._ObjectiveCType: NSItemProviderReading {
        try await withCheckedThrowingContinuation { cont in
            _ = p.loadObject(ofClass: type) { v, e in
                if let e { cont.resume(throwing: e) } else { cont.resume(returning: v) }
            }
        }
    }
}
