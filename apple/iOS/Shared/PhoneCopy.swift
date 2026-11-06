// User-facing words. English first; Norwegian in iOS/App/nb.lproj/Localizable.strings (the English text is the key).
import Foundation
import LoftCore

enum PhoneCopy {
    static let notAtHome = String(localized: "You're not at home right now. Connect to the home network for the first sign-in. After that, Loft works from anywhere.")
    static let wrongPassword = String(localized: "That username and password didn't work on the NAS. Check them and try again.")
    static let accountNotAllowed = String(localized: "This gateway doesn't allow that account. Sign in with your own NAS account.")
    static let tooManyAttempts = String(localized: "Too many tries. Wait 15 minutes, then try again.")
    static let cantReachNAS = String(localized: "Loft can't reach the NAS right now. Try again in a moment.")
    static let signInFailed = String(localized: "Sign-in didn't work. Try again.")
    static let invalidCode = String(localized: "That code isn't valid. Check it and try again.")
    static let codeExpired = String(localized: "That code has expired or been used up.")
    static let deviceRemoved = String(localized: "This iPhone was removed from Loft. Sign in again.")
    static let signInExpired = String(localized: "Your sign-in has expired. Sign in again.")
    static let uploadFailed = String(localized: "Couldn't save. Tap to try again.")
    static let notSignedIn = String(localized: "Open Loft and sign in first.")

    // Pairing (Loft 0.2). Same wording as the website's pairing page.
    static let pairingTitle = String(localized: "Pair with your gateway")
    static let notPaired = String(localized: "This iPhone isn't paired with a gateway yet.")
    static let pairingNotALink = String(localized: "That isn't a Loft pairing link. Use the whole link or QR code that loft-gateway pair shows.")
    static let pairingNewer = String(localized: "This pairing code is from a newer gateway. Update Loft, then try again.")
    static let pairingNoPin = String(localized: "This pairing code has no valid key fingerprint. Make a new one with loft-gateway pair.")
    static let pairingNotHTTPS = String(localized: "Loft only connects to a gateway over https.")
    static let manualBadAddress = String(localized: "Check the address. It should look like nas-gateway.local:8443.")
    static let manualBadPin = String(localized: "That doesn't look like a key fingerprint. Copy it from loft-gateway pair.")
    static let cameraDenied = String(localized: "Loft can't use the camera. Allow it in Settings › Loft, or open the pairing link instead.")
    static let noCamera = String(localized: "There's no camera to scan with. Open the pairing link on this iPhone, or paste it.")

    // Help for people who have no gateway yet: the setup guide on the website, in the app's language.
    static let noGatewayYet = String(localized: "No gateway yet? Loft needs one at home, next to your NAS.")
    static let setUpGateway = String(localized: "Set up a gateway")

    /// https://loft.incremental.no/<prefix>/gateway/ for the language the app is showing (website/build.py LANGUAGES).
    static var gatewayGuideURL: URL { websiteURL("gateway") }

    /// A page of the website (`gateway`, `how-it-works`, `privacy`, `source`) in the app's language.
    static func websiteURL(_ page: String) -> URL {
        let prefix: [String: String] = ["nb": "/no", "de": "/de", "es": "/es", "fr": "/fr", "it": "/it", "nl": "/nl",
                                        "pt-BR": "/pt", "ja": "/ja", "ko": "/ko", "zh-Hans": "/zh"]
        let lang = Bundle.main.preferredLocalizations.first ?? "en"
        return URL(string: "https://loft.incremental.no\(prefix[lang] ?? "")/\(page)/")!
    }

    // About Loft (0.2.1). Same story as the website's home page and How it works.
    static let aboutTitle = String(localized: "About Loft")
    static let aboutHowTitle = String(localized: "How Loft works")
    static let aboutHowFiles = String(localized: "Loft shows the shared folders on your NAS on this iPhone. Your files stay on the NAS, at home. No one else stores them.")
    static let aboutHowParts = String(localized: "Loft has two parts: this app, and a gateway that runs at home, next to your NAS. The app only ever talks to your gateway, and the gateway only ever talks to your NAS.")
    static let aboutHowSignIn = String(localized: "You sign in with the username and password you already have on the NAS, and the NAS's own permissions decide what you can see and change. Sign in at home the first time. After that, Loft works from anywhere.")
    static let aboutGatewayTitle = String(localized: "You need a gateway")
    static let aboutGateway = String(localized: "Loft doesn't work on its own. It needs a Loft gateway at home: a small program on a Linux machine next to the NAS. You set it up once, then pair this iPhone with the QR code or link it shows.")
    static let aboutFree = String(localized: "Loft is free and open source.")
    // The website's own menu words (website/content/<lang>.toml [nav]).
    static let linkHowItWorks = String(localized: "How it works")
    static let linkPrivacy = String(localized: "Privacy")
    static let linkSource = String(localized: "Source")

    /// "12.3 MB of 84.5 MB" under a download's progress bar.
    static func downloaded(_ done: Int64, of total: Int64) -> String {
        let f = { (n: Int64) in ByteCountFormatter.string(fromByteCount: n, countStyle: .file) }
        return String(localized: "\(f(done)) of \(f(total))")
    }

    /// "0.2.0 (15)": version and build, as App Store Connect and TestFlight show them.
    static var appVersion: String {
        let i = Bundle.main.infoDictionary
        let v = i?["CFBundleShortVersionString"] as? String ?? "?"
        let b = i?["CFBundleVersion"] as? String
        return b.map { "\(v) (\($0))" } ?? v
    }

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

    static func message(forSignIn code: String) -> String {
        switch code {
        case "invalid_credentials", "missing_credentials": wrongPassword
        case "account_not_allowed": accountNotAllowed
        case "too_many_attempts": tooManyAttempts
        case "password_login_direct_only": notAtHome
        case "nas_unavailable": cantReachNAS
        case "invalid_code": invalidCode
        case "code_expired": codeExpired
        default: signInFailed
        }
    }
}
