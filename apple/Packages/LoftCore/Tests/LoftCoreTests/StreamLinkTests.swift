import XCTest
@testable import LoftCore

final class StreamLinkTests: XCTestCase {
    let a = "52bbb1927bef469fb9c1771f72bc1e35", b = "26758648ddec40c2bc7e4486748ecc78"

    func testRoundTrip() {
        let u = StreamLink.url(items: [a, b])!
        XCTAssertEqual(u.absoluteString, "loft://stream?items=\(a),\(b)")
        XCTAssertEqual(StreamLink.items(from: u), [a, b])
    }

    func testSystemIdentifiersAreDropped() {
        XCTAssertNil(StreamLink.url(items: ["NSFileProviderRootContainerItemIdentifier"]))
        XCTAssertEqual(StreamLink.url(items: ["NSFileProviderRootContainerItemIdentifier", a])
            .flatMap(StreamLink.items(from:)), [a])
    }

    func testOtherLinksAreRefused() {
        for s in ["loft://stream", "loft://stream?items=", "loft://other?items=\(a)",
                  "http://stream?items=\(a)", "loft://stream?items=\(a),../../etc",
                  "loft://stream?items=\(a.uppercased())"] {
            XCTAssertNil(StreamLink.items(from: URL(string: s)!), s)
        }
        let many = Array(repeating: a, count: StreamLink.maxItems + 1)
        XCTAssertNil(StreamLink.url(items: many))
    }
}
