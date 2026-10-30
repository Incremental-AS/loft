import XCTest
@testable import LoftCore

final class ExclusionsTests: XCTestCase {
    func testOfficeAndFinderArtifactsAreExcluded() {
        for n in ["~$Report.docx", ".DS_Store", "._a.txt", ".~lock.a.odt#", "Word Work File L_123.tmp",
                  "Icon\r", "a.docx.sb-1234-abc"] {
            XCTAssertTrue(Exclusions.isExcluded(n), n)
        }
    }

    func testRealDocumentsAreNot() {
        for n in ["Report.docx", "budget.xlsx", "a.txt", "dollar$sign.txt"] {
            XCTAssertFalse(Exclusions.isExcluded(n), n)
        }
    }
}
