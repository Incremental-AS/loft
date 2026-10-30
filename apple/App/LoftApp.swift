// Host app: menu bar, first run (pairing, sign-in, folder), Settings. Every string lives in
// Copy.swift.
// The work itself (domain, change watcher, route, eviction, tokens) is in AppModel.swift.
import SwiftUI
import LoftCore

enum SyncStatus {
    case upToDate(Route)
    case syncing(files: Int, Route)
    case offline(waiting: Int)
    case cantReachHome   // no answer from the gateway
    case cantReachNAS    // gateway answers, NAS behind it doesn't (APIError.isNASUnreachable)
    case paused(waiting: Int)
    case error(SyncError, waiting: Int)

    var line: String {
        switch self {
        case .upToDate(let r): Copy.upToDate(r)
        case .syncing(let n, let r): Copy.syncing(files: n, r)
        case .offline(let n): Copy.offline(waiting: n)
        case .cantReachHome: Copy.cantReachHome
        case .cantReachNAS: Copy.cantReachNAS
        case .paused(let n): Copy.paused(waiting: n)
        case .error(let e, _): Copy.error(e)
        }
    }

    /// The smaller line under the status. Sign-in errors show the backlog when there is one.
    func secondLine(lastSynced: Date?) -> String? {
        let last = lastSynced.map(Copy.lastSynced)
        switch self {
        case .error(.deviceRemoved, _): return Copy.deviceRemovedDetail
        case .error(.noAccess, _): return Copy.noAccessDetail
        case .error(.stopped, _): return last
        case .error(_, let n) where n > 0: return Copy.waiting(n)
        default: return last
        }
    }
}

enum SettingsTab: Hashable { case account, folder, storage, general }

/// The one action item an error puts at the top of the menu (Copy.errorAction).
struct ErrorActionItem: View {
    let error: SyncError
    @ObservedObject var model: MenuModel

    var body: some View {
        Button(Copy.errorAction(error)) {
            if error == .noAccess {
                model.showSettings(.folder)
            } else {
                model.errorAction()
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        MainActor.assumeIsolated {
            NSApp.servicesProvider = StreamService()
            MenuModel.shared.start()
        }
    }

    // loft://stream?items=… from the extension's "Stream in VLC" (StreamLink), and
    // loft://pair#… pairing links (docs/pairing.md).
    func application(_ application: NSApplication, open urls: [URL]) {
        MainActor.assumeIsolated {
            for u in urls {
                if Pairing.isPairingURL(u) { MenuModel.shared.openPairingLink(u) }
                else { MenuModel.shared.streamInVLC(u) }
            }
        }
    }

    // Universal link https://loft.incremental.no/pair#… Needs the Associated Domains
    // entitlement (applinks:loft.incremental.no), which a Developer ID app only gets
    // through its provisioning profile: see apple/README.md. Until then Safari opens the
    // /pair page, and loft:// or a pasted link do the job.
    func application(_ application: NSApplication, continue userActivity: NSUserActivity,
                     restorationHandler: @escaping ([any NSUserActivityRestoring]) -> Void) -> Bool {
        guard userActivity.activityType == NSUserActivityTypeBrowsingWeb,
              let u = userActivity.webpageURL, Pairing.isPairingURL(u) else { return false }
        MainActor.assumeIsolated { MenuModel.shared.openPairingLink(u) }
        return true
    }

    // Opening Loft again while it runs (Finder, Spotlight, Launchpad). A menu-bar app has
    // no Dock icon and no window to bring back, so without this nothing happened: Ole
    // Henrik closed sign-in and could not get it back (2026-10-29).
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        MainActor.assumeIsolated {
            let m = MenuModel.shared
            if m.isSetUp { m.showSettings(m.settingsTab) } else { m.showOnboarding() }
        }
        return false
    }
}

/// "Stream in VLC" as a macOS Service too (NSServices in project.yml), for the keyboard
/// shortcut: a File Provider action can't have one, and Finder doesn't list those in its
/// File menu where an App Shortcut could reach them (macOS 27, build 8). Services see every
/// video, not only Loft's; elsewhere it is just "open in VLC".
final class StreamService: NSObject {
    @objc func streamInVLC(_ pboard: NSPasteboard, userData: String?,
                           error: AutoreleasingUnsafeMutablePointer<NSString?>) {
        let files = pboard.readObjects(forClasses: [NSURL.self],
                                       options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        MainActor.assumeIsolated { MenuModel.shared.openInVLC(files) }
    }
}

@main
struct LoftApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @ObservedObject private var model = MenuModel.shared

    var body: some Scene {
        MenuBarExtra {
            if model.isSetUp {
                Text(model.shown.line)
                if let second = model.shown.secondLine(lastSynced: model.lastSynced) { Text(second) }
                Divider()
                if let e = model.error {
                    ErrorActionItem(error: e, model: model)
                    Button(Copy.openInFinder) { model.openInFinder() }
                } else {
                    Button(Copy.openInFinder) { model.openInFinder() }
                    Button(model.isPaused ? Copy.resumeSyncing : Copy.pauseSyncing) { model.togglePause() }
                }
            } else if model.pairing == nil {
                Button(Copy.pairEllipsis) { model.showOnboarding(step: .pair) }
            } else {
                Button(Copy.signInAgain) { model.showOnboarding() }
            }
            Divider()
            if let u = model.availableUpdate {
                Button(Copy.updateAvailable(u)) { model.openUpdate() }
            }
            Button(Copy.settings) { model.showSettings(model.settingsTab) }
            Button(Copy.quit) { NSApplication.shared.terminate(nil) }
        } label: {
            Image(model.glyph)
        }
    }
}
