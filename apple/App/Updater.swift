// Mac updates through Sparkle (docs/mac-updates.md): it reads the appcast once a day, shows
// "Install Update", downloads the DMG, checks our EdDSA signature and the Developer ID, quits
// Loft, swaps the app, deletes the download and opens Loft again. The DMG isn't updated by the
// App Store, and the iPhone app has none of this.
import AppKit
import Sparkle

final class Updater: NSObject, SPUUpdaterDelegate, SPUStandardUserDriverDelegate {
    /// Testing only: a feed other than SUFeedURL (`defaults write no.incremental.loft
    /// LoftUpdateFeedURL …`). An update from it must still carry our EdDSA signature and our
    /// Developer ID, so it can't install anyone else's code.
    static let feedOverrideKey = "LoftUpdateFeedURL"

    /// A newer version Sparkle found but didn't show (a background check while Loft wasn't in
    /// front). The menu shows "Loft x.y.z is available…" until the user opens it.
    var onAvailable: (String?) -> Void = { _ in }

    private var controller: SPUStandardUpdaterController!

    override init() {
        super.init()
        controller = SPUStandardUpdaterController(startingUpdater: false, updaterDelegate: self,
                                                  userDriverDelegate: self)
    }

    func start() {
        // 0.2.0 build 12 and earlier read releases.json; carry its off switch over once.
        let d = UserDefaults.standard
        if d.object(forKey: "updateCheckEnabled") as? Bool == false {
            controller.updater.automaticallyChecksForUpdates = false
        }
        for k in ["updateCheckEnabled", "updateCheckedAt", "updateLatest", "updateLastError"] {
            d.removeObject(forKey: k)
        }
        // Same as releases.json's check: nothing about this Mac but the app's name and version.
        // Without it the system adds the user's languages (Accept-Language: nb-NO,…).
        controller.updater.httpHeaders = ["Accept-Language": "*"]
        controller.startUpdater()
    }

    var automaticallyChecks: Bool {
        get { controller.updater.automaticallyChecksForUpdates }
        set { controller.updater.automaticallyChecksForUpdates = newValue }
    }

    /// Shows Sparkle's window: the update it found, with Install Update.
    func checkForUpdates() {
        NSApp.activate(ignoringOtherApps: true)   // a menu bar app's window opens behind otherwise
        controller.checkForUpdates(nil)
    }

    // MARK: SPUUpdaterDelegate

    func feedURLString(for updater: SPUUpdater) -> String? {
        guard let s = UserDefaults.standard.string(forKey: Self.feedOverrideKey),
              let u = URL(string: s) else { return nil }
        let local = u.host == "127.0.0.1" || u.host == "localhost"
        return u.scheme == "https" || (u.scheme == "http" && local) ? s : nil
    }

    // MARK: SPUStandardUserDriverDelegate (gentle reminders: Loft has no Dock icon)

    var supportsGentleScheduledUpdateReminders: Bool { true }

    func standardUserDriverShouldHandleShowingScheduledUpdate(_ update: SUAppcastItem,
                                                              andInImmediateFocus immediateFocus: Bool) -> Bool {
        immediateFocus
    }

    func standardUserDriverWillHandleShowingUpdate(_ handleShowingUpdate: Bool, forUpdate update: SUAppcastItem,
                                                   state: SPUUserUpdateState) {
        if !handleShowingUpdate { onAvailable(update.displayVersionString) }
    }

    func standardUserDriverDidReceiveUserAttention(forUpdate update: SUAppcastItem) { onAvailable(nil) }

    func standardUserDriverWillFinishUpdateSession() { onAvailable(nil) }
}
