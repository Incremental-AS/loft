// Migration from Loft 0.1.x, which had one gateway's address and pins built in. A device
// that was signed in with 0.1.x gets a stored pairing for that gateway on the first launch
// of 0.2, so nobody has to pair again. New installs never use it.
//
// The public source carries NO address or pin. A private build adds them with a generated,
// gitignored file next to this one (apple/scripts/gen-legacy-pairing.py writes
// `LegacyPairing+Ops.swift`):
//
//     extension LegacyPairing: LegacyPairingSource {
//         public static let legacyPayload = "<base64url pairing payload>"
//     }
//
// Without that file the conformance doesn't exist, `LegacyPairing.pairing` is nil and
// there is nothing to migrate (every public build). See apple/README.md.
import Foundation

public protocol LegacyPairingSource {
    static var legacyPayload: String { get }
}

public enum LegacyPairing {
    /// The pairing 0.1.x devices are migrated to, if this build carries one.
    public static var pairing: Pairing? {
        guard let src = (LegacyPairing.self as Any.Type) as? LegacyPairingSource.Type else { return nil }
        return try? Pairing.decode(payload: src.legacyPayload)
    }
}
