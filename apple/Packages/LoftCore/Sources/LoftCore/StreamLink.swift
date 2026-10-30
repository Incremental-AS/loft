// Finder's "Stream in VLC". Finder downloads a file that isn't
// on this Mac in full before it hands it to ANY app, whichever way it is opened from
// Finder (tested on a Mac with a stub app as the handler: the 439 MB came first, then the
// app). An app that opens the file itself reads it in parts, which streams.
//
// So the menu item is a File Provider custom action. The extension is sandboxed and
// cannot reach the file's Finder path or start VLC, so it hands the item ids to the
// host app as loft://stream?items=<id>,<id>; the app looks up the paths and opens them
// in VLC the way dragging onto VLC does.
//
// Anything can open a loft:// link; all it can do is open Loft files, by id, in VLC.
import Foundation

public enum StreamLink {
    /// NSExtensionFileProviderActionIdentifier in project.yml (LoftFileProvider).
    public static let action = "no.incremental.loft.stream-in-vlc"
    public static let scheme = "loft"
    public static let vlcBundleId = "org.videolan.vlc"
    static let maxItems = 50

    public static func url(items: [String]) -> URL? {
        let ids = items.filter(isItemId)
        guard !ids.isEmpty, ids.count <= maxItems else { return nil }
        var c = URLComponents()
        c.scheme = scheme
        c.host = "stream"
        c.queryItems = [URLQueryItem(name: "items", value: ids.joined(separator: ","))]
        return c.url
    }

    /// The item ids in a link from `url(items:)`; nil for anything else.
    public static func items(from url: URL) -> [String]? {
        guard let c = URLComponents(url: url, resolvingAgainstBaseURL: false),
              c.scheme == scheme, c.host == "stream",
              let v = c.queryItems?.first(where: { $0.name == "items" })?.value else { return nil }
        let ids = v.split(separator: ",").map(String.init)
        guard !ids.isEmpty, ids.count <= maxItems, ids.allSatisfy(isItemId) else { return nil }
        return ids
    }

    /// Gateway item ids are uuid4 hex; this also keeps the system's own identifiers
    /// (root, trash) out.
    static func isItemId(_ s: String) -> Bool {
        s.count == 32 && s.allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }
}
