// Gateway Item -> NSFileProviderItem (docs/design.md §D.1).
//   itemIdentifier = gateway item id; the root's id <-> .rootContainer
//   itemVersion    = (content_etag, meta_etag) as UTF-8 bytes
import FileProvider
import UniformTypeIdentifiers
import LoftCore

final class LoftItem: NSObject, NSFileProviderItem {
    let item: Item
    let rootItemId: String

    init(_ item: Item, root: String) {
        self.item = item
        self.rootItemId = root
    }

    private var isRoot: Bool { item.id == rootItemId }

    var itemIdentifier: NSFileProviderItemIdentifier {
        isRoot ? .rootContainer : NSFileProviderItemIdentifier(item.id)
    }

    var parentItemIdentifier: NSFileProviderItemIdentifier {
        guard !isRoot, let p = item.parentId, p != rootItemId else { return .rootContainer }
        return NSFileProviderItemIdentifier(p)
    }

    var filename: String { isRoot ? "Loft" : item.name }

    var contentType: UTType {
        if item.isDirectory { return .folder }
        let ext = (item.name as NSString).pathExtension
        return ext.isEmpty ? .data : (UTType(filenameExtension: ext) ?? .data)
    }

    var capabilities: NSFileProviderItemCapabilities {
        // All-shares root: it holds only the shares, and they stay as UniFi Drive made them.
        if isRoot { return item.isFixed ? [.allowsContentEnumerating]
                                        : [.allowsContentEnumerating, .allowsAddingSubItems] }
        if item.isFixed { return [.allowsContentEnumerating, .allowsAddingSubItems] }
        if item.isDirectory {
            return [.allowsContentEnumerating, .allowsAddingSubItems, .allowsRenaming,
                    .allowsReparenting, .allowsDeleting]
        }
        // No .allowsEvicting: deprecated since macOS 13 in favour of contentPolicy below.
        return [.allowsReading, .allowsWriting, .allowsRenaming, .allowsReparenting,
                .allowsDeleting]
    }

    // Eviction is decided by the content policy. Left undeclared, every item is "system"
    // policy and the system refuses evictItem with .nonEvictable ("content policy
    // prevention"): on macOS 27 (998751f) no downloaded or saved file could be
    // evicted, by AppModel or by hand. Root downloads lazily; the rest inherit, so a
    // folder the user marks Keep Downloaded still governs what is inside it.
    #if os(macOS)   // iOS has no content policies: it evicts on its own
    var contentPolicy: NSFileProviderContentPolicy { isRoot ? .downloadLazily : .inherited }
    #endif

    var itemVersion: NSFileProviderItemVersion {
        NSFileProviderItemVersion(contentVersion: Data(item.contentEtag.utf8),
                                  metadataVersion: Data(item.metaEtag.utf8))
    }

    var documentSize: NSNumber? { item.isDirectory ? nil : NSNumber(value: item.size) }
    var contentModificationDate: Date? { item.modified }
    var creationDate: Date? { item.modified }    // v0: the gateway has no birth time
}

extension NSFileProviderItemVersion {
    /// "c<N>" / "m<N>" back out of the version the system hands us.
    var contentEtag: String? { String(data: contentVersion, encoding: .utf8) }
    var metaEtag: String? { String(data: metadataVersion, encoding: .utf8) }
}
