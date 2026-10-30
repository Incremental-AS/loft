// End-to-end on the simulator against a gateway: gateway/scripts/dev_server.py on the
// Mac's loopback (apple/scripts/ios-ui-test.sh sets everything up), or a real one.
// Every sign-in pairs first, the way a new user does. Values come from the environment,
// never from the repo:
//   TEST_RUNNER_LOFT_PAIR_LINK                         (https://loft.incremental.no/pair#…)
//   TEST_RUNNER_LOFT_ADDRESS, TEST_RUNNER_LOFT_PIN     ("Set up manually")
//   TEST_RUNNER_LOFT_USER, TEST_RUNNER_LOFT_PASSWORD   (password sign-in)
//   TEST_RUNNER_LOFT_CODE                              (sign-in code path, e.g. an app reviewer)
//   TEST_RUNNER_LOFT_SHARE, TEST_RUNNER_LOFT_OPEN      (share to enter, file path to open)
//   TEST_RUNNER_LOFT_SHOTS=<dir>                       (save screenshots there)
//   TEST_RUNNER_LOFT_LOCALE=en_US                      (format dates/sizes for that locale)
import XCTest

final class LoftPhoneUITests: XCTestCase {
    let env = ProcessInfo.processInfo.environment
    var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
        app = XCUIApplication()
        // The tests look for English labels, and the app has Norwegian (nb.lproj), so it runs in
        // English whatever the simulator's language is. TEST_RUNNER_LOFT_LOCALE=<locale>: dates,
        // sizes and words in that locale (store screenshots).
        let loc = env["LOFT_LOCALE"] ?? "en_US"
        app.launchArguments += ["-AppleLocale", loc, "-AppleLanguages", "(\(loc.prefix(2)))"]
        app.launch()
    }

    func shot(_ name: String) {
        guard let dir = env["LOFT_SHOTS"] else { return }
        try? XCUIScreen.main.screenshot().pngRepresentation
            .write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
    }

    /// iOS offers to save the password after a password sign-in. That's the system's prompt,
    /// not ours, and it sits over the app until answered, so a tap behind it goes nowhere.
    func dismissSavePassword() {
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for _ in 0..<10 {
            for host in [app!, springboard] {
                for label in ["Not Now", "Ikke nå"] where host.buttons[label].exists {
                    // A tap while the prompt is still animating in lands on nothing, so tap
                    // again until it is really gone (the 04:00 store run lost one that way).
                    host.buttons[label].tap()
                    if host.buttons[label].waitForNonExistence(timeout: 3) { sleep(1); return }
                }
            }
            sleep(1)
        }
    }

    /// The accessibility tree as text, next to the screenshots (what a query can match).
    func tree(_ name: String) {
        guard let dir = env["LOFT_SHOTS"] else { return }
        try? app.debugDescription.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).txt"),
                                        atomically: true, encoding: .utf8)
    }

    func signOutIfNeeded() {
        let account = app.buttons["Account"]
        guard account.waitForExistence(timeout: 4) else { return }
        account.tap()
        app.buttons["Sign out"].firstMatch.tap()
        app.buttons.matching(identifier: "Sign out").element(boundBy: 1).tap()
        XCTAssertTrue(app.buttons["Sign in"].waitForExistence(timeout: 15))
    }

    /// From the sign-in screen (paired already) or the pairing screen (new install): pair
    /// by typing the link, confirm, and arrive at sign-in. Re-pairing with the same gateway
    /// keeps nothing stale: it goes through the same screens as a first pairing.
    func pairByLink() throws {
        let link = try XCTUnwrap(env["LOFT_PAIR_LINK"], "TEST_RUNNER_LOFT_PAIR_LINK")
        openPairingScreen()
        let field = app.textFields["pairing-link"]
        XCTAssertTrue(field.waitForExistence(timeout: 10), "pairing screen")
        shot("00-pair")
        field.tap(); field.typeText(link)
        app.buttons["Continue"].tap()
        confirmPairing()
    }

    func openPairingScreen() {
        let pairAnother = app.buttons["Pair with another gateway"]
        if app.textFields["pairing-link"].waitForExistence(timeout: 5) { return }
        if pairAnother.waitForExistence(timeout: 5) { pairAnother.tap() }
    }

    func confirmPairing() {
        let pair = app.buttons["confirm-pair"]
        XCTAssertTrue(pair.waitForExistence(timeout: 10), "confirm sheet")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH 'Pair with'")).firstMatch.exists)
        shot("00-confirm")
        pair.tap()
        XCTAssertTrue(app.buttons["Sign in"].waitForExistence(timeout: 15), "sign-in after pairing")
    }

    func signInWithPassword() throws {
        let user = try XCTUnwrap(env["LOFT_USER"]), pw = try XCTUnwrap(env["LOFT_PASSWORD"])
        app.textFields["Username"].tap(); app.textFields["Username"].typeText(user)
        app.secureTextFields["Password"].tap(); app.secureTextFields["Password"].typeText(pw)
        app.buttons["Sign in"].tap()
    }

    func testPairByLinkThenSignIn() throws {
        signOutIfNeeded()
        try pairByLink()
        let name = env["LOFT_GATEWAY_NAME"] ?? "Dev gateway"
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", name)).firstMatch.exists,
                      "sign-in names the paired gateway")
        try signInWithPassword()
        let share = app.buttons[env["LOFT_SHARE"] ?? "home"]
        XCTAssertTrue(share.waitForExistence(timeout: 30), "share list")
        dismissSavePassword()
        shot("05-paired-shares")
        // The account sheet names the gateway.
        app.buttons["Account"].tap()
        XCTAssertTrue(app.staticTexts["Gateway"].waitForExistence(timeout: 5))
        shot("06-account")
        app.buttons["Done"].tap()
    }

    func testBadLinkIsRefusedThenManualSetup() throws {
        let address = try XCTUnwrap(env["LOFT_ADDRESS"]), pin = try XCTUnwrap(env["LOFT_PIN"])
        signOutIfNeeded()
        openPairingScreen()
        let field = app.textFields["pairing-link"]
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        field.tap(); field.typeText("https://loft.incremental.no/pair#not-a-code")
        app.buttons["Continue"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH \"That isn't a Loft pairing link\""))
                        .firstMatch.waitForExistence(timeout: 5), "bad link refused")
        XCTAssertFalse(app.buttons["confirm-pair"].exists)
        shot("07-bad-link")
        app.buttons["Set up manually"].tap()
        let addr = app.textFields["Address, like nas-gateway.local:8443"]
        XCTAssertTrue(addr.waitForExistence(timeout: 5))
        addr.tap(); addr.typeText(address)
        let pinField = app.textFields["Key fingerprint"]
        pinField.tap(); pinField.typeText("not-a-pin")
        app.buttons["Continue"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH \"That doesn't look like a key fingerprint\""))
                        .firstMatch.waitForExistence(timeout: 5), "bad pin refused")
        pinField.tap()
        pinField.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 20) + pin)
        app.buttons["Continue"].tap()
        confirmPairing()
        try signInWithPassword()
        XCTAssertTrue(app.buttons[env["LOFT_SHARE"] ?? "home"].waitForExistence(timeout: 30), "share list")
        dismissSavePassword()
    }

    func testPasswordSignInBrowseOpen() throws {
        let user = try XCTUnwrap(env["LOFT_USER"]), pw = try XCTUnwrap(env["LOFT_PASSWORD"])
        let shareName = try XCTUnwrap(env["LOFT_SHARE"])
        signOutIfNeeded()
        if env["LOFT_PAIR_LINK"] != nil { try pairByLink() }
        shot("01-signin")
        app.textFields["Username"].tap(); app.textFields["Username"].typeText(user)
        app.secureTextFields["Password"].tap(); app.secureTextFields["Password"].typeText(pw)
        app.buttons["Sign in"].tap()
        let share = app.buttons[shareName]
        XCTAssertTrue(share.waitForExistence(timeout: 30), "share list")
        dismissSavePassword()
        shot("02-shares"); tree("02-shares")
        share.tap()
        let opened = app.navigationBars[shareName].waitForExistence(timeout: 15)
        shot("03-folder"); tree("03-folder")
        XCTAssertTrue(opened, "tapping the share opened it")
        if let file = env["LOFT_OPEN"] {
            // Wait for each folder to load before looking inside it.
            for part in file.split(separator: "/").dropLast() {
                let folder = app.buttons[String(part)].firstMatch
                XCTAssertTrue(folder.waitForExistence(timeout: 15), "folder \(part)")
                folder.tap()
                XCTAssertTrue(app.navigationBars[String(part)].waitForExistence(timeout: 15), "opened \(part)")
            }
            // A file row's label is "name, size · date", so match on the start.
            let target = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@",
                                                          String(file.split(separator: "/").last!))).firstMatch
            XCTAssertTrue(target.waitForExistence(timeout: 15), "file \(file)")
            target.tap()
            sleep(8); shot("04-open")
        }
    }

    /// App Store screenshots (TEST_RUNNER_LOFT_SHOTS required) in the seeded share (TEST_RUNNER_LOFT_SHARE):
    /// the folder, Photos, the PDF and the video playing. Set the status bar with
    /// `simctl status_bar override` before running.
    func testStoreScreenshots() throws {
        let user = try XCTUnwrap(env["LOFT_USER"]), pw = try XCTUnwrap(env["LOFT_PASSWORD"])
        let shareName = try XCTUnwrap(env["LOFT_SHARE"])
        signOutIfNeeded()
        app.textFields["Username"].tap(); app.textFields["Username"].typeText(user)
        app.secureTextFields["Password"].tap(); app.secureTextFields["Password"].typeText(pw)
        app.buttons["Sign in"].tap()
        let share = app.buttons[shareName]
        XCTAssertTrue(share.waitForExistence(timeout: 30), "share list")
        dismissSavePassword()
        share.tap()
        XCTAssertTrue(app.navigationBars[shareName].waitForExistence(timeout: 15), "opened \(shareName)")
        func row(_ name: String) -> XCUIElement {
            let r = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", name)).firstMatch
            XCTAssertTrue(r.waitForExistence(timeout: 15), "row \(name)")
            return r
        }
        func back() { app.navigationBars.buttons.firstMatch.tap(); sleep(1) }
        _ = row("Welcome to Loft.pdf"); sleep(3); shot("store-1-folder")
        row("Photos").tap()
        XCTAssertTrue(app.navigationBars["Photos"].waitForExistence(timeout: 15))
        _ = row("Sample image 3.jpg"); sleep(5); shot("store-2-photos")
        back()
        row("Welcome to Loft.pdf").tap(); sleep(6); shot("store-3-pdf")
        back()
        row("Videos").tap()
        XCTAssertTrue(app.navigationBars["Videos"].waitForExistence(timeout: 15))
        row("Sample video.mp4").tap(); sleep(6); shot("store-4-video")
    }

    func testCodeSignIn() throws {
        let code = try XCTUnwrap(env["LOFT_CODE"])
        signOutIfNeeded()
        app.buttons["I have a sign-in code"].tap()
        let f = app.textFields.firstMatch
        f.tap(); f.typeText(code)
        app.buttons["Sign in"].tap()
        XCTAssertTrue(app.buttons["Account"].waitForExistence(timeout: 30))
        shot("10-code-signed-in")
    }

    /// The 0.2 pairing words in Norwegian (iOS/App/nb.lproj): the way back to pairing from
    /// sign-in, the pairing screen and its error for a link that isn't one.
    func testNorwegianPairing() throws {
        signOutIfNeeded()
        try pairByLink()                      // paired, at sign-in (English)
        app.terminate()
        app.launchArguments = ["-AppleLocale", "nb_NO", "-AppleLanguages", "(nb)"]
        app.launch()
        let again = app.buttons["Par med en annen gateway"]
        XCTAssertTrue(again.waitForExistence(timeout: 10), "nb: pair with another gateway")
        again.tap()
        XCTAssertTrue(app.staticTexts["Par med gatewayen din"].waitForExistence(timeout: 10), "nb title")
        XCTAssertTrue(app.buttons["Skann QR-kode"].exists, "nb scan")
        let field = app.textFields["pairing-link"]
        field.tap(); field.typeText("noe annet")
        app.buttons["Fortsett"].tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Det er ikke en paringslenke"))
                        .firstMatch.waitForExistence(timeout: 5), "nb error")
        shot("20-pair-nb")
        app.buttons["Avbryt"].tap()
    }
}
