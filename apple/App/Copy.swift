// Every user-facing string lives here. Rules: sentence case, an ellipsis
// only on items that open a window, name the place ("home", "the NAS", "this Mac"),
// never the technology, and never call Loft "cloud".
import Foundation
import LoftCore

enum Copy {
    static let productName = "Loft"
    /// NSFileProviderDomain display name (Finder sidebar). Just the name, no folder suffix.
    static let domainName = productName

    // MARK: Menu (status item)
    static func upToDate(_ r: Route) -> String { "Up to date · \(routeName(r))" }
    static func syncing(files n: Int, _ r: Route) -> String {
        "Syncing \(n) \(n == 1 ? "file" : "files") · \(routeName(r))"
    }
    static func offline(waiting n: Int) -> String { "Offline · \(waiting(n))" }
    static let cantReachHome = "Can't reach home. Retrying…"     // the gateway doesn't answer
    static let cantReachNAS = "Can't reach the NAS. Retrying…"   // the gateway does, the NAS doesn't
    static func paused(waiting n: Int) -> String { "Paused · \(waiting(n))" }
    static func lastSynced(_ d: Date) -> String {
        "Last synced \(d.formatted(.dateTime.hour().minute()))"
    }

    // Error state (error glyph; error > offline > syncing). The action item replaces
    // "Pause syncing" at the top of the items. Never an error code here: codes go to the log.
    static func error(_ e: SyncError) -> String {
        switch e {
        case .nasPasswordChanged: "Sign in again · NAS password changed"
        case .signInExpired: "Sign in again · Sign-in expired"
        case .deviceRemoved: "Signed out · This Mac was removed"
        case .noAccess: "No access · Loft folder unavailable"
        case .stopped: "Sync stopped · Something went wrong"
        }
    }
    static let deviceRemovedDetail = "Removed from another device or by the admin"
    static let noAccessDetail = "Ask the NAS admin, or choose another folder"
    static func errorAction(_ e: SyncError) -> String {
        if e == .noAccess { return chooseFolder }   // opens Settings › Folder; "Try again" wouldn't fix it
        return e.needsSignIn ? signInAgain : tryAgain
    }
    static let signInAgain = "Sign in…"
    static let chooseFolder = "Choose folder…"
    static let tryAgain = "Try again"

    static let openInFinder = "Open Loft in Finder"
    static let pauseSyncing = "Pause syncing"
    static let resumeSyncing = "Resume syncing"
    static let settings = "Settings…"
    static let quit = "Quit Loft"

    static func routeName(_ r: Route) -> String { r == .direct ? "Direct" : "Internet" }  // .publicInternet
    static func waiting(_ n: Int) -> String {
        "\(n) \(n == 1 ? "change" : "changes") waiting to upload"
    }

    // MARK: First run (640 pt wide; hero image on step 1 only, 64 pt app icon on 2–4)
    static let welcomeTitle = "Your files, kept at home."
    static let welcomeBody = "Loft puts the NAS at home in your Finder. Files stream in when you open them and go back home when you save, so nothing piles up on this Mac."
    static let getStarted = "Get started"

    static let signInTitle = "Sign in to your NAS"
    static let signInBody = "Use the username and password you already have on the NAS. The first sign-in has to happen at home, so your password never travels over the internet. After that, Loft works from anywhere."
    static let usernameField = "NAS username"
    static let usernamePlaceholder = "yourname"
    /// Under the fields.
    static let accountHelp = "Loft shows every shared folder this account can open. No account yet? Ask whoever runs the NAS."
    static let passwordField = "Password"
    static let signIn = "Sign in"
    static let notAtHome = String(localized: "You're not at home right now. Connect to the home network for the first sign-in. After that, Loft works from anywhere.")
    static let wrongPassword = String(localized: "That username and password didn't work on the NAS. Check them and try again.")
    /// Gateway 403 account_not_allowed: the owner's allowed_users_re / denied_users_re refused this account
    /// (by default only guest). Don't name admin here: the gateway allows it unless the owner denies it.
    static let accountNotAllowed = String(localized: "This gateway doesn't allow that account. Sign in with your own NAS account.")

    static let pickFolderTitle = "Choose your Loft folder"
    static let pickFolderBody = "This folder on the NAS becomes Loft in Finder. You can change it later in Settings."
    static let useThisFolder = "Use this folder"

    static let doneTitle = "You're all set."
    static let doneBody = "Loft is in the Finder sidebar. Your files show up right away and download when you open them. To keep something on this Mac for offline use, right-click it and choose Keep Downloaded."
    static let openLoftInFinder = openInFinder

    // MARK: Settings (tabs: Account · Folder · Storage · General)
    static let tabAccount = "Account"
    static let tabFolder = "Folder"
    static let tabStorage = "Storage"
    static let tabGeneral = "General"
    static func signedInAs(_ user: String) -> String { "Signed in as \(user)" }
    static func thisMac(_ device: String) -> String { "This Mac: \(device)" }
    static let signOut = "Sign out"
    static func loftFolder(_ path: String) -> String { "Loft folder: \(path)" }
    static let allShares = "All your shared folders"
    static let useAllShares = "Use all shared folders"
    static func rootName(_ r: RootInfo) -> String { r.isAllShares ? allShares : r.displayPath }
    static let change = "Change…"
    static let storageHeading = "Storage on this Mac"
    static let cacheLimit = "Cache limit"                 // default 5 GB (docs/design.md §E.1)
    static let freeUpSpaceAfter = "Free up space after"   // default 10 min (docs/design.md §E.1)
    static let storageHelp = "Files you keep downloaded are never removed. Everything else goes back to the NAS once it's saved."
    static let officeAutoSaveNote = "Office AutoSave only works with OneDrive. Save with ⌘S. Office's AutoRecover still covers you if something crashes."
    static let openAtLogin = "Open Loft at login"

    // MARK: Errors and limits (same rules: sentence case, name the place).
    /// Gateway 429 too_many_attempts (5 wrong passwords per account per 15 min).
    static let tooManyAttempts = String(localized: "Too many tries. Wait 15 minutes, then try again.")
    /// Anything else that fails during sign-in or folder picking; the code goes to the log.
    static let signInFailed = String(localized: "Something went wrong. Try again.")
    /// Finder shows this on the Loft location while syncing is paused (disconnect reason).
    static let pausedReason = "Syncing is paused. Resume it from the Loft menu."
    static let back = "Back"                                // folder picker, VoiceOver label only
    static let otherDevices = "Other devices"               // Settings › Account list heading
    /// Finder right-click item on video files, and the Service with the shortcut ⌘⇧V.
    /// The menu text itself is in project.yml, twice (NSExtensionFileProviderActionName
    /// and the NSServices NSMenuItem): "Stream in VLC".
    static let vlcMissingTitle = "VLC isn't on this Mac"
    static let vlcMissingBody = "Install VLC, then choose Stream in VLC again."
    static let getVLC = "Get VLC"
    static let cancel = String(localized: "Cancel")    // also on the pairing confirmation
    static func minutes(_ n: Int) -> String { n < 60 ? "\(n) min" : "\(n / 60) h" }

    // MARK: Pairing (Loft 0.2, docs/pairing.md). Same wording as the website's pairing page.
    // Norwegian: nb.lproj/Localizable.strings, keyed by the English text (%@ for each value).
    static let pairTitle = String(localized: "Pair with your gateway")
    static let pairBody = String(localized: "On the gateway, run loft-gateway pair. Open the link it shows on this Mac, or copy it and paste it here. The link carries the gateway's address and its keys, so Loft talks to your gateway from the very first connection.")
    static let pairLinkField = String(localized: "Pairing link")
    static let pairLinkPlaceholder = "https://loft.incremental.no/pair#…"
    static let paste = String(localized: "Paste")
    static let continueButton = String(localized: "Continue")
    static let setUpManually = String(localized: "Set up manually…")
    static let manualTitle = String(localized: "Set up manually")
    static let manualBody = String(localized: "Type the gateway's address and one of its key fingerprints. loft-gateway pair shows both. The fingerprint is how Loft checks it's really your gateway, so there's no setup without it.")
    static let addressField = String(localized: "Address")
    static let addressPlaceholder = "nas-gateway.local:8443"
    static let pinField = String(localized: "Key fingerprint")
    static let pinPlaceholder = String(localized: "Base64, ends in =")
    static let usePairingLink = String(localized: "Use a pairing link instead")
    static func confirmPairTitle(_ name: String) -> String { String(localized: "Pair with \u{201C}\(name)\u{201D}?") }
    static func atHome(_ host: String) -> String { String(localized: "At home: \(host)") }
    static func awayTunnel(_ host: String) -> String { String(localized: "Away from home: \(host), through a tunnel. Sign in at home first.") }
    static func awayDirect(_ host: String) -> String { String(localized: "Away from home: \(host), straight to the gateway.") }
    static let homeOnly = String(localized: "Away from home: not set up. Loft works on the home network only.")
    static func replacesGateway(_ name: String) -> String { String(localized: "This Mac is paired with \(name). If you pair with another gateway, you'll be signed out of \(name).") }
    static let pair = String(localized: "Pair")
    static let pairEllipsis = String(localized: "Pair with your gateway…")
    static let pairAnother = String(localized: "Pair with another gateway…")
    static func pairedWith(_ name: String, _ host: String) -> String { String(localized: "Gateway: \(name) (\(host))") }
    static let notPaired = String(localized: "This Mac isn't paired with a gateway yet.")
    static let pairingNotALink = String(localized: "That isn't a Loft pairing link. Copy the whole link that loft-gateway pair shows.")
    static let pairingNewer = String(localized: "This pairing link is from a newer gateway. Update Loft, then try again.")
    static let pairingNoPin = String(localized: "This pairing link has no valid key fingerprint. Make a new one with loft-gateway pair.")
    static let pairingNotHTTPS = String(localized: "Loft only connects to a gateway over https.")
    static let manualBadAddress = String(localized: "Check the address. It should look like nas-gateway.local:8443.")
    static let manualBadPin = String(localized: "That doesn't look like a key fingerprint. Copy it from loft-gateway pair.")

    // MARK: Update check (Loft 0.2, docs/releases-json.md). The iPhone app has none.
    static func updateAvailable(_ version: String) -> String { String(localized: "Loft \(version) is available…") }
    static let checkForUpdates = String(localized: "Check for updates")
    static let updateCheckHelp = String(localized: "Once a day, Loft asks loft.incremental.no if there's a newer version. It sends nothing about you, this Mac or your files.")

    static func pairingError(_ e: Error, manual: Bool = false) -> String {
        guard let p = e as? PairingError else { return pairingNotALink }
        switch p {
        case .unsupportedVersion: return pairingNewer
        case .noPins, .badPin: return manual ? manualBadPin : pairingNoPin
        case .notHTTPS: return pairingNotHTTPS
        case .badURL, .missingField("address"): return manual ? manualBadAddress : pairingNotALink
        default: return pairingNotALink
        }
    }
}
