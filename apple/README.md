# Loft for Mac and iPhone

Swift. `project.yml` (XcodeGen) makes `Loft.xcodeproj`: the Mac menu bar app with its File Provider extension
(`App/`, `FileProvider/`), the iPhone app with its File Provider and share extensions (`iOS/`), and the
`LoftCore` package (`Packages/LoftCore`, Foundation only, builds and tests on Linux too).

## Pairing

The apps carry no gateway address and no key pin. A device is paired with one gateway (docs/pairing.md):

- iPhone: scan the QR code from `loft-gateway pair`, open its link (universal link, needs the AASA file from
  `docs/aasa.json` on loft.incremental.no), paste or type the link, or *Set Up Manually* (address + key pin).
- Mac: open the `loft://pair#…` link, paste the link, or *Set up manually…*. The universal link handler is in
  place but needs the Associated Domains entitlement in the Mac's provisioning profile (see below).
- Both show the gateway's name and addresses and ask before pairing. *Pair with another gateway* replaces the
  pairing and signs out (unless the new code shares a key with the current one: same gateway).

`LoftCore/Pairing.swift` parses and validates the code; the pairing is stored in the app group's shared defaults
(`SharedState.pairing`, next to the pins refreshed from `/v1/identity`), so the extensions read the same one. It
feeds `RouteSelector` and the pinned session: the password only goes to a listener whose key matches a pin (the
home address, or a public address in `direct` mode), never through a tunnel.

## 0.1.x migration (private builds only)

0.1.x had one gateway built in. A device that was signed in with 0.1.x gets a stored pairing for that gateway on
the first launch of 0.2 (`SharedState.migrateLegacyPairing`, run by `LoftEnvironment`). The values are not in this
source tree: `LegacyPairing.swift` only declares a hook, and a private build adds

    Packages/LoftCore/Sources/LoftCore/LegacyPairing+Ops.swift      (gitignored)

with `apple/scripts/gen-legacy-pairing.py <ops-dir>`, which reads the deployment's `gateway.toml` (name and
addresses) and `client-pins.txt` (the pins 0.1.x had built in). The private build scripts run it (the Mac only when asked:
its releases are public downloads, so a public Mac build leaves the file out) and remove the file otherwise. Without
it there is nothing to migrate, which is right for every public build. `swift test` with `LOFT_EXPECT_LEGACY=1` fails if a build that
should carry it doesn't.

## Build and test (on a Mac with Xcode and XcodeGen)

    (cd apple && xcodegen generate)
    xcodebuild -project apple/Loft.xcodeproj -scheme Loft -configuration Debug \
      CODE_SIGNING_ALLOWED=NO CODE_SIGN_IDENTITY="" build                          # Mac app
    xcodebuild -project apple/Loft.xcodeproj -scheme LoftPhone \
      -destination "platform=iOS Simulator,name=iPhone 17" build-for-testing       # iPhone app

Debug builds this Mac's architecture only; Release (`ops/build/mac-compile.sh`) is universal.

A gateway for tests, no NAS needed: a venv from `gateway/requirements.lock`, then

    apple/scripts/core-it.sh .venv/bin/python        # LoftCore: unit tests + client against dev_server.py
    apple/scripts/ios-ui-test.sh .venv/bin/python    # iPhone UI tests: pair by link / manually, sign in, browse
    apple/scripts/mac-ui-test.sh .venv/bin/python    # Mac UI tests: pair by pasted link / manually, sign in
    python3 apple/scripts/check-strings.py           # every Norwegian string still matches the code

They start `gateway/scripts/dev_server.py` on loopback and make the pairing link with the gateway's own encoder
(`apple/scripts/pair-link.py`). `LOFT_IT_PORTS` / `LOFT_UI_PORTS="<direct> <public>"` move the dev server when
another job holds the default ports. `.github/workflows/apple.yml` runs LoftCore on Linux and macOS and the builds;
the UI tests need a Mac with a logged-in user (they run over ssh in that user's session).

The Mac UI tests run a Debug copy of the app under its own bundle id and URL scheme
(`no.incremental.loft.uitest`, `loft-uitest`) with `-LoftUITest` (`UITestMode` in `AppModel.swift`, Debug builds
only): its own defaults, the token in memory, no Finder location, no update check. So they leave an installed Loft
on the same Mac alone, and the script unregisters and deletes the test build afterwards.

## Updates (Mac only)

The Mac app is not in the App Store, so it updates itself with Sparkle (`App/Updater.swift`, docs/mac-updates.md):
once a day it reads `https://loft.incremental.no/appcast.xml`, and a newer build shows Sparkle's *Install Update*
window, or *Loft x.y.z is available…* in the menu, which opens it. Installing verifies the EdDSA signature and the
Developer ID, swaps the app and reopens it. Settings › General › *Check for updates* switches the check off;
`LOFT_NO_UPDATE_CHECK=1` too (CI and UI tests never start Sparkle). Builds before 0.2.0 (13) used
`releases.json` (`LoftCore/UpdateCheck.swift`, still used by the docs' format tests). The iPhone app has no check:
the App Store updates it.

## Norwegian

English is in the code (`App/Copy.swift`, `iOS/Shared/PhoneCopy.swift`, the iPhone views) and is the key; Norwegian
is in `App/nb.lproj/Localizable.strings` and `iOS/App/nb.lproj/Localizable.strings`: the 0.2 pairing, manual setup,
confirmation, the pairing and sign-in errors and (Mac) the update check. The rest of the apps is English for now.
Rule: capital letter after a colon; words as on the website (gateway, paring, paringslenke, fingeravtrykk).

## Signing notes

- iPhone: `com.apple.developer.associated-domains = applinks:loft.incremental.no` is in the app's entitlements.
  The App ID needs the Associated Domains capability before an App Store export.
- Mac (Developer ID): Associated Domains is a profile-backed entitlement; adding it to the signed entitlements
  without a profile that allows it stops the app from launching. Until the profile has it, `loft://pair#…` and a
  pasted link are the Mac's ways in.
- The team id (`5A3JX4D37B`) in `project.yml`, the entitlements and `LoftConfig.swift` is Incremental's; a build
  under another team changes those.
