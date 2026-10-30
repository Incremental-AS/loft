// Plain AppKit windows around SwiftUI views. A menu-bar-only app (LSUIElement) has no
// Dock icon to bring a SwiftUI Window/Settings scene forward, so first run and Settings
// are hosted here and activated explicitly.
import SwiftUI

@MainActor
final class Windows {
    static let shared = Windows()
    private var open: [String: NSWindow] = [:]

    func show<V: View>(_ id: String, width: CGFloat, @ViewBuilder _ content: () -> V) {
        let host = NSHostingController(rootView: content().frame(width: width))
        let w: NSWindow
        if let existing = open[id] {
            w = existing
            w.contentViewController = host
        } else {
            w = NSWindow(contentViewController: host)
            w.styleMask = [.titled, .closable]
            w.isReleasedWhenClosed = false
            w.title = Copy.productName
            open[id] = w
            w.center()
        }
        NSApp.activate()
        w.makeKeyAndOrderFront(nil)
    }

    func close(_ id: String) {
        open[id]?.close()
    }
}
