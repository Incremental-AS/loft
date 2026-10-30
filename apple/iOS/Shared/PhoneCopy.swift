// User-facing words. English first; Norwegian in iOS/App/nb.lproj/Localizable.strings (the English text is the key).
import Foundation
import LoftCore

enum PhoneCopy {
    static let notAtHome = String(localized: "You're not at home right now. Connect to the home network for the first sign-in. After that, Loft works from anywhere.")
    static let wrongPassword = String(localized: "That username and password didn't work on the NAS. Check them and try again.")
    static let accountNotAllowed = String(localized: "This account can't use Loft.")
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
