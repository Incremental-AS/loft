// Pairing (docs/pairing.md): which gateway this device talks to and the key
// pins that make the very first connection authenticated. Replaces the addresses and pins
// 0.1 had built in. Foundation-only, so the parser is tested on Linux too.
//
//   https://loft.incremental.no/pair#<payload>     universal link / QR code / pasted link
//   loft://pair#<payload>                          desktop URL scheme
//   <payload>                                      the bare code, pasted
//
// <payload> = base64url (no padding) of compact UTF-8 JSON:
//   {"v":1,"name":"Home","lan":"https://10.0.0.20:8443","pins":["…","…"],
//    "pub":"https://files.example.org","mode":"tunnel"|"direct"}
// Refused: an unknown v, a non-https URL, no pins (or a pin that isn't a SHA-256), pub
// without a known mode. `pub` carries the password only when mode is direct.
import Foundation

/// How the public (outside) address reaches the gateway.
public enum PublicMode: String, Codable, Sendable, Equatable {
    /// A TLS-ending proxy (Cloudflare Tunnel): normal web PKI, device tokens only, no sign-in.
    case tunnel
    /// A port forward to the direct listener: same pinned key, sign-in allowed.
    case direct
}

public enum PairingError: Error, Equatable, Sendable, CustomStringConvertible {
    case empty
    case notAPairingLink            // a URL, but not …/pair#… or loft://pair#…
    case badEncoding                // not base64url, or not UTF-8 JSON
    case unsupportedVersion(Int)    // v other than 1: a newer gateway, update the app
    case missingField(String)
    case notHTTPS(String)           // which field: lan / pub / address
    case badURL(String)
    case noPins
    case badPin(String)
    case badMode(String)

    public var description: String {
        switch self {
        case .empty: "empty"
        case .notAPairingLink: "not a pairing link"
        case .badEncoding: "bad encoding"
        case .unsupportedVersion(let v): "unsupported version \(v)"
        case .missingField(let f): "missing \(f)"
        case .notHTTPS(let f): "\(f) is not https"
        case .badURL(let f): "bad \(f)"
        case .noPins: "no pins"
        case .badPin(let p): "bad pin \(p)"
        case .badMode(let m): "bad mode \(m)"
        }
    }
}

public struct Pairing: Equatable, Sendable {
    public static let version = 1
    public static let linkPrefix = "https://loft.incremental.no/pair#"
    public static let schemePrefix = "loft://pair#"
    /// The gateway's default direct port (gateway config), for "Set up manually".
    public static let defaultDirectPort = 8443

    public let name: String
    /// The direct listener on the home network. Pinned.
    public let lan: URL
    /// base64 SHA-256 of the direct key's SubjectPublicKeyInfo: live first, then standby.
    public let pins: [String]
    /// Outside address, with how it reaches the gateway. nil = home network only.
    public let publicURL: URL?
    public let publicMode: PublicMode?

    public init(name: String, lan: URL, pins: [String], publicURL: URL? = nil,
                publicMode: PublicMode? = nil) throws {
        let n = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !n.isEmpty else { throw PairingError.missingField("name") }
        try Self.checkHTTPS(lan, field: "lan")
        guard !pins.isEmpty else { throw PairingError.noPins }
        for p in pins where !Self.isPin(p) { throw PairingError.badPin(p) }
        if let publicURL {
            try Self.checkHTTPS(publicURL, field: "pub")
            guard publicMode != nil else { throw PairingError.badMode("") }
        }
        self.name = n
        self.lan = Self.trimmed(lan)
        var seen = Set<String>()
        self.pins = pins.filter { seen.insert($0).inserted }
        self.publicURL = publicURL.map(Self.trimmed)
        self.publicMode = publicURL == nil ? nil : publicMode
    }

    // MARK: derived

    /// The password may go to the public address only when it reaches the pinned key.
    public var publicAllowsSignIn: Bool { publicURL != nil && publicMode == .direct }

    /// Shown to the user before they confirm, and in Settings: "Home (10.0.0.20)".
    public var lanHost: String { lan.host ?? lan.absoluteString }

    public var routeConfig: RouteConfig {
        RouteConfig(direct: lan, publicURL: publicURL, publicMode: publicMode ?? .tunnel, pins: Set(pins))
    }

    // MARK: parse

    /// A link (https://…/pair#…, loft://pair#… or loft://pair?…), or the bare code.
    public static func parse(_ text: String) throws -> Pairing {
        let s = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { throw PairingError.empty }
        return try decode(payload: try payload(in: s))
    }

    /// Accepts a universal link / loft:// URL handed to the app.
    public static func parse(url: URL) throws -> Pairing { try parse(url.absoluteString) }

    /// True if the URL is meant for pairing (so the app routes it here, not to StreamLink).
    public static func isPairingURL(_ url: URL) -> Bool {
        let scheme = url.scheme?.lowercased()
        if scheme == "loft" { return url.host?.lowercased() == "pair" }
        if scheme == "https" {
            return url.host?.lowercased() == "loft.incremental.no" && url.path.hasSuffix("/pair")
        }
        return false
    }

    static func payload(in s: String) throws -> String {
        let lower = s.lowercased()
        if lower.hasPrefix("https://") || lower.hasPrefix("http://") {
            // The code is in the fragment (it never reaches a server). Which page carries
            // it doesn't matter: trust comes from the pins inside, shown before confirming.
            guard let hash = s.firstIndex(of: "#") else { throw PairingError.notAPairingLink }
            return String(s[s.index(after: hash)...])
        }
        if lower.hasPrefix("loft:") {
            guard let u = URLComponents(string: s), u.host?.lowercased() == "pair" else {
                throw PairingError.notAPairingLink
            }
            if let f = u.fragment, !f.isEmpty { return f }
            if let q = u.percentEncodedQuery, !q.isEmpty {
                // loft://pair?<payload> or loft://pair?p=<payload>
                if let p = u.queryItems?.first(where: { $0.name == "p" || $0.name == "payload" })?.value {
                    return p
                }
                return q
            }
            throw PairingError.notAPairingLink
        }
        return s
    }

    private struct Version: Decodable { let v: Int }
    private struct Wire: Codable {
        let v: Int
        let name: String?
        let lan: String?
        let pins: [String]?
        let pub: String?
        let mode: String?
    }

    /// The base64url payload itself.
    public static func decode(payload: String) throws -> Pairing {
        guard let data = base64URLDecode(payload) else { throw PairingError.badEncoding }
        let dec = JSONDecoder()
        guard let ver = try? dec.decode(Version.self, from: data) else {
            if (try? JSONSerialization.jsonObject(with: data)) is [String: Any] {
                throw PairingError.missingField("v")
            }
            throw PairingError.badEncoding
        }
        guard ver.v == version else { throw PairingError.unsupportedVersion(ver.v) }
        guard let w = try? dec.decode(Wire.self, from: data) else { throw PairingError.badEncoding }
        guard let name = w.name else { throw PairingError.missingField("name") }
        guard let lanS = w.lan else { throw PairingError.missingField("lan") }
        guard let pins = w.pins, !pins.isEmpty else { throw PairingError.noPins }
        let lan = try url(lanS, field: "lan")
        var pub: URL?, mode: PublicMode?
        if let p = w.pub, !p.isEmpty {
            pub = try url(p, field: "pub")
            guard let m = w.mode, let pm = PublicMode(rawValue: m) else {
                throw PairingError.badMode(w.mode ?? "")
            }
            mode = pm
        }
        return try Pairing(name: name, lan: lan, pins: pins, publicURL: pub, publicMode: mode)
    }

    // MARK: "Set up manually"

    /// Address + pin typed by hand. The address may leave out https:// and the port
    /// (8443, the gateway's default). Pins: one or more, separated by spaces, commas or
    /// new lines; `sha256//` prefixes (curl's form) are accepted. No public route: pair by
    /// code to get one.
    public static func manual(address: String, pins: String, name: String? = nil) throws -> Pairing {
        var a = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !a.isEmpty else { throw PairingError.missingField("address") }
        if !a.contains("://") { a = "https://" + a }
        guard var c = URLComponents(string: a), c.host?.isEmpty == false else {
            throw PairingError.badURL("address")
        }
        guard c.scheme?.lowercased() == "https" else { throw PairingError.notHTTPS("address") }
        if c.port == nil { c.port = defaultDirectPort }
        guard let lan = c.url else { throw PairingError.badURL("address") }
        let list = pins.components(separatedBy: CharacterSet(charactersIn: " ,;\n\t"))
            .map { $0.hasPrefix("sha256//") ? String($0.dropFirst(8)) : $0 }
            .filter { !$0.isEmpty }
        guard !list.isEmpty else { throw PairingError.noPins }
        let n = name?.trimmingCharacters(in: .whitespacesAndNewlines)
        return try Pairing(name: (n?.isEmpty == false ? n! : c.host!), lan: lan, pins: list)
    }

    // MARK: encode

    /// The base64url payload (what `loft-gateway pair` makes), for storage and tests.
    public var payload: String {
        let w = Wire(v: Self.version, name: name, lan: lan.absoluteString, pins: pins,
                     pub: publicURL?.absoluteString, mode: publicMode?.rawValue)
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let data = (try? enc.encode(w)) ?? Data()
        return data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    public var link: String { Self.linkPrefix + payload }

    // MARK: helpers

    /// base64 of exactly 32 bytes.
    public static func isPin(_ p: String) -> Bool { Data(base64Encoded: p)?.count == 32 }

    static func base64URLDecode(_ s: String) -> Data? {
        var b = s.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        b = b.replacingOccurrences(of: "=", with: "")
        guard !b.isEmpty, b.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "+" || $0 == "/") })
        else { return nil }
        b += String(repeating: "=", count: (4 - b.count % 4) % 4)
        return Data(base64Encoded: b)
    }

    private static func url(_ s: String, field: String) throws -> URL {
        guard let u = URL(string: s) else { throw PairingError.badURL(field) }
        try checkHTTPS(u, field: field)
        return u
    }

    private static func checkHTTPS(_ u: URL, field: String) throws {
        guard u.scheme?.lowercased() == "https" else { throw PairingError.notHTTPS(field) }
        guard let h = u.host, !h.isEmpty, u.user == nil, u.password == nil,
              u.query == nil, u.fragment == nil else { throw PairingError.badURL(field) }
    }

    private static func trimmed(_ u: URL) -> URL {
        var s = u.absoluteString
        while s.hasSuffix("/") { s.removeLast() }
        return URL(string: s) ?? u
    }
}
