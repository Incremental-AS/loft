// The Mac app's first run, end to end against a gateway: gateway/scripts/dev_server.py on
// this Mac's loopback (apple/scripts/mac-ui-test.sh sets everything up). The app runs with
// -LoftUITest (UITestMode, Debug builds only): its own state, the token in memory, no
// Finder location, no update check, so an installed Loft on the same Mac is left alone.
// Values come from the environment, never from the repo:
//   TEST_RUNNER_LOFT_PAIR_LINK                         (https://loft.incremental.no/pair#…)
//   TEST_RUNNER_LOFT_ADDRESS, TEST_RUNNER_LOFT_PIN     ("Set up manually")
//   TEST_RUNNER_LOFT_USER, TEST_RUNNER_LOFT_PASSWORD   (password sign-in)
//   TEST_RUNNER_LOFT_GATEWAY_NAME                      (default "Dev gateway")
import AppKit
import XCTest

final class LoftMacUITests: XCTestCase {
    let env = ProcessInfo.processInfo.environment
    var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
        app = XCUIApplication()
        // The tests look for English labels; testNorwegianPairing asks for nb itself.
        app.launchArguments = ["-LoftUITest", "-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
    }

    override func tearDown() {
        if let app, app.state != .notRunning { app.terminate() }
    }

    func shot(_ name: String) {
        let a = XCTAttachment(screenshot: app.windows.firstMatch.exists
                              ? app.windows.firstMatch.screenshot() : XCUIScreen.main.screenshot())
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    var gatewayName: String { env["LOFT_GATEWAY_NAME"] ?? "Dev gateway" }

    /// A system prompt left on the build Mac's screen (another process's Local Network
    /// question, say) can sit over part of the window. Drag the window clear of it rather
    /// than answer a prompt that isn't ours.
    func makeHittable(_ e: XCUIElement) {
        guard e.exists, !e.isHittable else { return }
        let prompts = XCUIApplication(bundleIdentifier: "com.apple.UserNotificationCenter").dialogs
        let screen = NSScreen.main?.frame.size ?? CGSize(width: 1920, height: 1080)
        for i in 0..<prompts.count where !e.isHittable {
            let d = prompts.element(boundBy: i).frame, w = app.windows.firstMatch.frame
            guard d.intersects(w) else { continue }
            // Beside the prompt, on whichever side has room; same height.
            let x = d.maxX + 12 + w.width <= screen.width ? d.maxX + 12 : max(0, d.minX - 12 - w.width)
            let bar = app.windows.firstMatch.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0))
                .withOffset(CGVector(dx: 0, dy: 12))
            bar.click(forDuration: 0.3, thenDragTo: bar.withOffset(CGVector(dx: x - w.minX, dy: 0)))
        }
        XCTAssertTrue(e.isHittable, "\(e) is covered")
    }

    /// Click, after moving the window out from under a foreign prompt if one covers `e`.
    func click(_ e: XCUIElement) {
        makeHittable(e)
        e.click()
    }

    /// First run opens the welcome window by itself (menu bar app, no Dock icon).
    func startPairing() {
        app.launch()
        let start = app.buttons["Get started"]
        XCTAssertTrue(start.waitForExistence(timeout: 20), "welcome window")
        click(start)
        XCTAssertTrue(app.textFields["pairing-link"].waitForExistence(timeout: 10), "pairing step")
    }

    /// The confirm step names the gateway and its home address; Pair goes on to sign-in.
    func confirmPairing(name: String, lanHost: String? = nil) {
        let title = app.staticTexts["Pair with \u{201C}\(name)\u{201D}?"]
        XCTAssertTrue(title.waitForExistence(timeout: 10), "confirm step")
        if let h = lanHost {
            XCTAssertTrue(app.staticTexts["At home: \(h)"].exists, "home address shown")
        }
        shot("confirm")
        click(app.buttons["Pair"])
    }

    func signIn(name: String) throws {
        let user = try XCTUnwrap(env["LOFT_USER"], "TEST_RUNNER_LOFT_USER")
        let pw = try XCTUnwrap(env["LOFT_PASSWORD"], "TEST_RUNNER_LOFT_PASSWORD")
        let userField = app.textFields["nas-user"]
        XCTAssertTrue(userField.waitForExistence(timeout: 15), "sign-in step")
        let gw = app.staticTexts.matching(NSPredicate(format: "value BEGINSWITH %@ OR label BEGINSWITH %@",
                                                          "Gateway: \(name)", "Gateway: \(name)"))
        XCTAssertGreaterThan(gw.count, 0, "paired gateway shown on sign-in")
        click(userField)
        userField.typeText(user)
        let pwField = app.secureTextFields["nas-password"]
        click(pwField)
        pwField.typeText(pw)
        shot("sign-in")
        click(app.buttons["Sign in"])
        // Signed in, folder set (all shares): the last step of the first run.
        XCTAssertTrue(app.staticTexts["You're all set."].waitForExistence(timeout: 30), "signed in")
        shot("done")
    }

    func testPairByPastedLinkThenSignIn() throws {
        let link = try XCTUnwrap(env["LOFT_PAIR_LINK"], "TEST_RUNNER_LOFT_PAIR_LINK")
        startPairing()
        shot("pair")
        // The Paste button reads the clipboard, as a user's ⌘C from the gateway's terminal.
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(link, forType: .string)
        click(app.buttons["Paste"])
        confirmPairing(name: gatewayName)
        try signIn(name: gatewayName)
    }

    func testManualSetupThenSignIn() throws {
        let address = try XCTUnwrap(env["LOFT_ADDRESS"], "TEST_RUNNER_LOFT_ADDRESS")
        let pin = try XCTUnwrap(env["LOFT_PIN"], "TEST_RUNNER_LOFT_PIN")
        startPairing()
        // A link-styled button: found by its identifier, whatever element type macOS gives it.
        let manual = app.descendants(matching: .any)["set-up-manually"].firstMatch
        click(manual)
        let a = app.textFields["manual-address"]
        XCTAssertTrue(a.waitForExistence(timeout: 10), "manual step")
        // A pin that isn't one is refused before anything else happens.
        click(a); a.typeText(address)
        let p = app.textFields["manual-pin"]
        click(p); p.typeText("not-a-pin")
        click(app.buttons["Continue"])
        let err = app.staticTexts["pairing-error"]
        XCTAssertTrue(err.waitForExistence(timeout: 5), "bad pin refused")
        XCTAssertEqual(err.value as? String, "That doesn't look like a key fingerprint. Copy it from loft-gateway pair.")
        click(p); p.typeKey("a", modifierFlags: .command); p.typeText(pin)
        shot("manual")
        click(app.buttons["Continue"])
        // A manual pairing is named after its host until /v1/identity says otherwise.
        let host = String(address.split(separator: ":")[0])
        confirmPairing(name: host, lanHost: address)
        try signIn(name: host)
    }

    func testBadLinkIsRefused() {
        startPairing()
        let field = app.textFields["pairing-link"]
        click(field)
        field.typeText("https://example.org/not-a-pairing-link")
        click(app.buttons["Continue"])
        XCTAssertTrue(app.staticTexts["pairing-error"].waitForExistence(timeout: 5), "error shown")
        XCTAssertFalse(app.buttons["Pair"].exists, "no confirm step")
    }

    /// The 0.2 strings in Norwegian (nb.lproj): the pairing step and its error.
    func testNorwegianPairing() {
        app.launchArguments = ["-LoftUITest", "-AppleLanguages", "(nb)", "-AppleLocale", "nb_NO"]
        app.launch()
        let start = app.buttons["Get started"]          // a 0.1 string, still English
        XCTAssertTrue(start.waitForExistence(timeout: 20), "welcome window")
        click(start)
        XCTAssertTrue(app.staticTexts["Par med gatewayen din"].waitForExistence(timeout: 10), "nb title")
        XCTAssertTrue(app.buttons["Lim inn"].exists && app.buttons["Fortsett"].exists, "nb buttons")
        let manual = app.descendants(matching: .any)["set-up-manually"].firstMatch
        XCTAssertTrue(manual.exists, "manual setup link")
        XCTAssertTrue([manual.label, manual.title, manual.value as? String ?? ""].contains("Sett opp manuelt …"),
                      "nb manual setup: \(manual.label) / \(manual.title)")
        let field = app.textFields["pairing-link"]
        click(field); field.typeText("noe annet")
        click(app.buttons["Fortsett"])
        XCTAssertTrue(app.staticTexts["Det er ikke en paringslenke for Loft. Kopier hele lenken som loft-gateway pair viser."]
                      .waitForExistence(timeout: 5), "nb error")
        shot("pair-nb")
    }
}
