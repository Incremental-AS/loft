# Loft for Android

Kotlin + Jetpack Compose. `applicationId` `no.incremental.loft`, minSdk 26, targetSdk 36.
Distributed as an APK from loft.incremental.no, not Google Play.

- `core/`: plain Kotlin/JVM. Pairing payload (`docs/pairing.md`), SPKI pinning, route choice, the gateway API
  client (OkHttp), change feed, update-check parser. Tests run on any JDK.
- `app/`: the Android app. Compose UI, `DocumentsProvider` (`no.incremental.loft.documents`), share target,
  camera upload and update check (WorkManager), device token in an Android Keystore AES-GCM key.

minSdk 26: adaptive icons, `StorageManager.openProxyFileDescriptor` (streaming reads in the provider) and
`java.util.Base64` are all API 26.

## Build and test

    ./gradlew :core:test :app:testDebugUnitTest :app:assembleDebug :app:assembleRelease

Instrumented tests need a running emulator (API 33 or newer) and the dev gateway on the same host
(`gateway/scripts/dev_server.py <nas-dir> <work-dir> 18443 18080`). The emulator reaches the host as 10.0.2.2:

    adb logcat -c
    ./gradlew :app:connectedDebugAndroidTest \
      -Pandroid.testInstrumentationRunnerArguments.loftPin="$(cat <work-dir>/direct.pin)"
    adb logcat -d -s LoftIT:I | scripts/check-disk.sh <nas-dir>/home

Optional runner arguments: `loftLan` (default `https://10.0.2.2:18443`), `loftUser`, `loftPassword`, `loftShare`
(defaults are the dev server's test account). The test runner never schedules periodic work (update check,
camera upload); the tests that need it switch it on for themselves and off again.

The tests write into the folder `android-it` of the dev share and leave their last run's files there.
`scripts/check-disk.sh` reads the `LoftIT` log lines and checks those files byte for byte on the dev server's
disk, and that deleted or renamed ones are gone. The camera upload tests switch the emulator's Wi-Fi off and on
(`svc wifi`); on mobile data the emulator is a metered network.

## What is tested

Unit tests (JVM): `core` 51 (pairing payload, SPKI pins, route choice, the API client against MockWebServer,
change feed, the releases.json parser incl. the `platforms` form), `app` 9 (`QrDecoderTest`: the decoder the
scan screen feeds camera frames to, on generated QR codes of a pairing link in padded Y planes; `CameraLedgerTest`).

Instrumented, on an arm64 API 35 emulator against `dev_server.py`, 24 tests:

| Test | What it checks |
|---|---|
| `GatewayE2ETest` | Pinned TLS (right pin works, wrong pin refused), Keystore token store and rotation, chunked upload, download, ranges, conflict, rename, delete, change feed |
| `DocumentsProviderTest` | Roots, create, write-on-close upload, append, rename, move, delete, 9 MB streaming read with seeks, thumbnails, recents |
| `AppFlowTest` | Pairing link -> sign in -> choose the share -> browse; a bad link is refused |
| `ShareTargetTest` | "Save to Loft": `ACTION_SEND` (one file) and `ACTION_SEND_MULTIPLE` (JPEG, 5 MB binary, MP4) with MediaStore `content://` URIs, folder picked in the chooser UI, files on the gateway byte for byte |
| `BrowseActionsTest` | The upload button (the system picker's answer injected with an `ActivityMonitor`; it must ask for openable files, several at once), new folder, rename and delete through the UI |
| `CameraUploadTest` | JPEG and MP4 written into DCIM by another user (the shell) and scanned into MediaStore, read with the media permissions: uploaded once, nothing on a second run, a later photo alone; photos from before switching on and non-DCIM pictures stay home; a video finished after a later photo still goes; two runs at once give one copy each; "Wi-Fi only" on mobile data waits and goes on Wi-Fi, switched off it goes over mobile data |
| `UpdateCheckWorkerTest` | releases.json from a local HTTPS server: a newer `version_code` notifies (once), the same one doesn't, switched off there is no request at all; the setting schedules and cancels the daily check |

Not tested: decoding a QR code from the live camera. The scan screen opens and shows the emulator's camera
(virtual scene), but its camera can't be pointed at a poster without the emulator window; the decoder the frames
go through is tested on generated codes. App Link verification (needs `assetlinks.json` live on the website).

## Release signing

Either `LOFT_SIGNING_PROPERTIES=/path/to/signing.properties` with `storeFile`, `storePassword`, `keyAlias`,
`keyPassword`, or the Gradle properties `LOFT_RELEASE_STORE_FILE`, `LOFT_RELEASE_STORE_PASSWORD`,
`LOFT_RELEASE_KEY_ALIAS`, `LOFT_RELEASE_KEY_PASSWORD` (for example as `ORG_GRADLE_PROJECT_*` environment variables).
Without either, `assembleRelease` is signed with the debug key (fine for testing, not for publishing).

## Website files

- `docs/assetlinks.json`: serve at `https://loft.incremental.no/.well-known/assetlinks.json` (as
  `application/json`, no redirect). It holds the SHA-256 fingerprint of Loft's release signing certificate.
  Without it, `/pair` links open in the browser instead of the app.
- `docs/releases.example.json`: the format the update check reads from `https://loft.incremental.no/releases.json`.
  The app notifies when `android.version_code` is higher than its own. `url` must be on loft.incremental.no.
