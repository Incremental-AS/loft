// SHA-256 for upload hashes (gateway checks per chunk and per file) and the direct-route
// SPKI pin. CryptoKit on Apple platforms; a small pure-Swift fallback so the package
// still builds and tests on Linux.
import Foundation
#if canImport(CryptoKit)
import CryptoKit
#endif

public struct Hasher256 {
    #if canImport(CryptoKit)
    private var h = CryptoKit.SHA256()
    #else
    private var h = PureSHA256()
    #endif

    public init() {}

    public mutating func update(_ data: Data) { h.update(data: data) }

    public mutating func finalizeHex() -> String { Hasher256.hex(finalizeBytes()) }

    public mutating func finalizeBytes() -> [UInt8] {
        #if canImport(CryptoKit)
        return Array(h.finalize())
        #else
        return h.finalize()
        #endif
    }

    public static func hex(_ bytes: [UInt8]) -> String {
        bytes.map { String(format: "%02x", $0) }.joined()
    }

    public static func hex(of data: Data) -> String {
        var x = Hasher256(); x.update(data); return x.finalizeHex()
    }

    public static func digest(of data: Data) -> [UInt8] {
        var x = Hasher256(); x.update(data); return x.finalizeBytes()
    }

    /// Streams the file in 4 MB reads: a 5 GB upload must not be loaded into memory.
    public static func hex(ofFile url: URL, onProgress: ((Int64) -> Void)? = nil) throws -> String {
        let fh = try FileHandle(forReadingFrom: url)
        defer { try? fh.close() }
        var x = Hasher256()
        var done: Int64 = 0
        while true {
            let chunk = try fh.read(upToCount: 4 << 20) ?? Data()
            if chunk.isEmpty { break }
            x.update(chunk)
            done += Int64(chunk.count)
            onProgress?(done)
        }
        return x.finalizeHex()
    }
}

/// FIPS 180-4, only used where CryptoKit is missing (Linux test host).
struct PureSHA256 {
    private var state: [UInt32] = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]
    private var buffer: [UInt8] = []
    private var length: UInt64 = 0

    private static let k: [UInt32] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]

    mutating func update(data: Data) {
        length &+= UInt64(data.count)
        buffer.append(contentsOf: data)
        var off = 0
        while buffer.count - off >= 64 {
            block(buffer, off)
            off += 64
        }
        if off > 0 { buffer.removeFirst(off) }
    }

    mutating func finalize() -> [UInt8] {
        let bits = length &* 8
        var tail = buffer
        tail.append(0x80)
        while tail.count % 64 != 56 { tail.append(0) }
        for i in (0..<8).reversed() { tail.append(UInt8((bits >> (UInt64(i) * 8)) & 0xff)) }
        var off = 0
        while off < tail.count { block(tail, off); off += 64 }
        buffer = []
        return state.flatMap { v in (0..<4).reversed().map { UInt8((v >> (UInt32($0) * 8)) & 0xff) } }
    }

    private mutating func block(_ b: [UInt8], _ off: Int) {
        @inline(__always) func rotr(_ x: UInt32, _ n: UInt32) -> UInt32 { (x >> n) | (x << (32 - n)) }
        var w = [UInt32](repeating: 0, count: 64)
        for i in 0..<16 {
            let j = off + i * 4
            w[i] = UInt32(b[j]) << 24 | UInt32(b[j + 1]) << 16 | UInt32(b[j + 2]) << 8 | UInt32(b[j + 3])
        }
        for i in 16..<64 {
            let s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3)
            let s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10)
            w[i] = w[i - 16] &+ s0 &+ w[i - 7] &+ s1
        }
        var a = state[0], bb = state[1], c = state[2], d = state[3]
        var e = state[4], f = state[5], g = state[6], h = state[7]
        for i in 0..<64 {
            let s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
            let ch = (e & f) ^ (~e & g)
            let t1 = h &+ s1 &+ ch &+ PureSHA256.k[i] &+ w[i]
            let s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
            let maj = (a & bb) ^ (a & c) ^ (bb & c)
            let t2 = s0 &+ maj
            h = g; g = f; f = e; e = d &+ t1
            d = c; c = bb; bb = a; a = t1 &+ t2
        }
        state[0] &+= a; state[1] &+= bb; state[2] &+= c; state[3] &+= d
        state[4] &+= e; state[5] &+= f; state[6] &+= g; state[7] &+= h
    }
}

/// Direct-route pin: base64(sha256(DER SubjectPublicKeyInfo)), the same value
/// `loft-gateway init` prints and /v1/identity serves. SecKey hands out the raw EC point
/// (04||X||Y), so the SPKI header is put back in front before hashing.
public enum SPKIPin {
    static let p256Header: [UInt8] = [0x30, 0x59, 0x30, 0x13, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce,
                                      0x3d, 0x02, 0x01, 0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d,
                                      0x03, 0x01, 0x07, 0x03, 0x42, 0x00]
    static let p384Header: [UInt8] = [0x30, 0x76, 0x30, 0x10, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce,
                                      0x3d, 0x02, 0x01, 0x06, 0x05, 0x2b, 0x81, 0x04, 0x00, 0x22,
                                      0x03, 0x62, 0x00]

    /// nil for anything but an uncompressed P-256/P-384 point: the gateway key is P-256
    /// (docs/design.md §C), and an unknown key type must fail closed.
    public static func pin(ecPoint: Data) -> String? {
        let header: [UInt8]
        switch ecPoint.count {
        case 65: header = p256Header
        case 97: header = p384Header
        default: return nil
        }
        guard ecPoint.first == 0x04 else { return nil }
        return Data(Hasher256.digest(of: Data(header) + ecPoint)).base64EncodedString()
    }
}
