# Mac updates (Sparkle)

The Mac app (the Developer ID DMG, not an App Store app) updates itself with
[Sparkle](https://sparkle-project.org) 2. Builds 0.2.0 (13) and later; older builds read `releases.json`
(`docs/releases-json.md`) and only show a notice.

## What the user sees

Once a day Loft reads `https://loft.incremental.no/appcast.xml`. When it lists a newer build:

- If Loft was just opened, Sparkle's window appears at once: the new version and **Install Update**.
- Otherwise the menu bar menu shows "Loft x.y.z is available…", which opens that window.

**Install Update** downloads the DMG, verifies it, quits Loft, puts the new Loft.app where the old one was, deletes
the download and opens Loft again. There is no "install automatically" option: Loft asks first. Settings › General ›
Check for updates switches the daily check off (it carries over the old releases.json switch once).

## What is checked

- **The feed** is signed (`SURequireSignedFeed`) with Loft's EdDSA key; an unsigned or altered appcast is refused.
- **The DMG** must match its `sparkle:edSignature` (same key, `SUPublicEDKey` in `apple/project.yml`) before it is
  even opened (`SUVerifyUpdateBeforeExtraction`), and the app inside must be signed with the same Developer ID as the
  running one.
- Requests: the appcast and the DMG, nothing else. No system profile (`SUEnableSystemProfiling` off). The only headers
  that say anything are `User-Agent: Loft/<version> Sparkle/<version>` and `Accept-Language: *` (from build 14; build 13
  sent the Mac's languages).

The private key never leaves the build Mac. Losing it means a release can't be signed for existing installs; Sparkle
then accepts a new key only from an update signed with the old one.

## Building a release

`mac-compile.sh` → `sign-notarize.sh` → `mac-appcast.sh <dmg>`, all on the build Mac in one checkout. The last step
writes `build/appcast/appcast.xml` (one item, the newest build) and `Loft-<version>-<build>.dmg`. Publish in this
order: the DMG to the GitHub release `v<version>`, then `appcast.xml` to `website/static/appcast.xml` and deploy the
site. Add the `macos` entry to `releases.json` too, for builds that predate Sparkle.

Sparkle compares `CFBundleVersion` (the build number), so every release needs a higher one; the Mac and iPhone share
the counter.

## Testing an update locally

Two builds with different numbers, the older one installed in /Applications:

    LOFT_DOWNLOAD_PREFIX=http://127.0.0.1:8765/ ops/build/mac-appcast.sh build/dmg/<newer>.dmg
    (cd build/appcast && python3 -m http.server 8765 --bind 127.0.0.1)
    defaults write no.incremental.loft LoftUpdateFeedURL http://127.0.0.1:8765/appcast.xml

Then open the menu item (or relaunch Loft) and install. `defaults delete no.incremental.loft LoftUpdateFeedURL`
afterwards. The override only takes `https://` or `http://` to this Mac, and the update still has to pass both
signature checks.

Made with ❤️ in 🇳🇴 by Incremental (www.incremental.no)
