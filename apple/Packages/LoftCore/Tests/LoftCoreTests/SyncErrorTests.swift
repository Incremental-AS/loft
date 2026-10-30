import XCTest
@testable import LoftCore

final class SyncErrorTests: XCTestCase {
    private func e(_ code: String) -> APIError { APIError(error: code) }

    func testGatewayCodesMapToNorasCases() {
        XCTAssertEqual(SyncError(syncLoopFailure: e("nas_credentials_invalid")), .nasPasswordChanged)
        XCTAssertEqual(SyncError(syncLoopFailure: e("token_expired")), .signInExpired)
        XCTAssertEqual(SyncError(syncLoopFailure: e("invalid_token")), .signInExpired)
        XCTAssertEqual(SyncError(syncLoopFailure: e("device_revoked")), .deviceRemoved)
        XCTAssertEqual(SyncError(syncLoopFailure: e("access_denied")), .noAccess)
        XCTAssertEqual(SyncError(syncLoopFailure: e("root_missing")), .noAccess)
        XCTAssertEqual(SyncError(syncLoopFailure: e("no_root_selected")), .stopped(code: "no_root_selected"))
    }

    func testTransientCodesAreNotAnErrorState() {
        for c in ["nas_unavailable", "nas_error", "anchor_expired"] {
            XCTAssertNil(SyncError(syncLoopFailure: e(c)), c)
        }
        XCTAssertTrue(e("nas_unavailable").isNASUnreachable)
        XCTAssertTrue(e("nas_error").isNASUnreachable)
        XCTAssertFalse(e("anchor_expired").isNASUnreachable)
    }

    func testOnlyAuthCasesAskForSignIn() {
        XCTAssertTrue(e("device_revoked").needsSignIn)
        XCTAssertTrue(e("token_expired").needsSignIn)
        XCTAssertFalse(e("access_denied").needsSignIn)
        XCTAssertFalse(e("nas_unavailable").needsSignIn)
    }
}
