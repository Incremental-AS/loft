// Names that never leave the Mac (docs/design.md §D.3). The File Provider extension
// answers createItem for these with NSFileProviderError(.excludedFromSync),
// so they stay local-only; the gateway refuses them as a second wall.
// Keep in step with EXCLUDED in gateway/loft_gateway/service.py — the
// client list is a superset (Office safe-save temps are client-side only).
import Foundation

public enum Exclusions {
    static let prefixes = ["~$", "._", ".~lock.", "Word Work File ", "~WRL", "~WRD", ".loft"]
    static let exact: Set<String> = [".DS_Store", "Icon\r", "Thumbs.db", "desktop.ini"]

    public static func isExcluded(_ name: String) -> Bool {
        if exact.contains(name) { return true }
        if prefixes.contains(where: { name.hasPrefix($0) }) { return true }
        if name.hasSuffix(".tmp") { return true }
        if name.contains(".sb-") { return true }   // NSDocument safe-save scratch
        return false
    }
}
