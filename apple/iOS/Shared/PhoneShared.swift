// Shared by the iPhone app and its "Save to Loft" share extension: the gateway
// environment, the all-shares root, and uploading a local file into a folder.
import Foundation
import LoftCore

enum Phone {
    /// The iPhone signs in to every share the account can open (the Mac's default too),
    /// so the app, Files and the share sheet all start from the list of shares.
    static let allSharesRoot = (share: RootInfo.allShares, path: "")

    /// The last folder something was saved to, offered first next time.
    static var lastFolder: SavedFolder? {
        get { SharedState().defaults.data(forKey: "lastSaveFolder").flatMap { try? JSONDecoder().decode(SavedFolder.self, from: $0) } }
        set { SharedState().defaults.set(newValue.flatMap { try? JSONEncoder().encode($0) }, forKey: "lastSaveFolder") }
    }

    /// Upload a local file into `folderId` under `name`. A name that's taken gets " 2", " 3"
    /// … before the extension, like Files does, instead of overwriting.
    @discardableResult
    static func save(_ file: URL, as name: String, into folderId: String, env: LoftEnvironment,
                     progress: (@Sendable (Int64, Int64) -> Void)? = nil) async throws -> Item {
        _ = await env.selector.ensureFresh { await RouteSelector.probeDirect(env.probeTransport, env.selector.config) }
        // A taken name would come back as a conflict copy ("x (conflict, iPhone, …)");
        // for a save, "x 2" is what people expect, so pick a free name first.
        var taken = Set<String>()
        var cursor: String?
        repeat {
            let page = try await env.client.children(folderId, cursor: cursor)
            page.items.forEach { taken.insert($0.name.precomposedStringWithCanonicalMapping.lowercased()) }
            cursor = page.nextCursor
        } while cursor != nil
        var candidate = name, n = 2
        while taken.contains(candidate.precomposedStringWithCanonicalMapping.lowercased()) && n < 1000 {
            candidate = numbered(name, n); n += 1
        }
        return try await env.client.uploadAndCommit(
            file: file, CommitRequest(parentId: folderId, name: candidate), progress: progress).item
    }

    static func numbered(_ name: String, _ n: Int) -> String {
        let ext = (name as NSString).pathExtension, base = (name as NSString).deletingPathExtension
        return ext.isEmpty ? "\(base) \(n)" : "\(base) \(n).\(ext)"
    }

    /// File names from other apps can hold anything; the NAS refuses some characters.
    static func safeName(_ s: String) -> String {
        let bad = CharacterSet(charactersIn: "/\\:*?\"<>|").union(.controlCharacters)
        let t = s.components(separatedBy: bad).joined(separator: "-").trimmingCharacters(in: .whitespaces)
        return t.isEmpty || t.hasPrefix(".") ? "Untitled" + t : String(t.prefix(200))
    }

    static func photoName(_ date: Date = Date(), ext: String = "heic") -> String {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH.mm.ss"
        return "Photo \(f.string(from: date)).\(ext)"
    }
}

struct SavedFolder: Codable, Equatable, Hashable {
    let id: String
    let path: String          // shown to the user, e.g. "Photos/2026"
}
