# releases.json: the update check

Loft apps that don't update through an app store (Android, Windows, Linux, and Mac builds before Sparkle) fetch
`https://loft.incremental.no/releases.json` at most once a day. The Mac reads `appcast.xml` instead
(`docs/mac-updates.md`). These are the only requests a Loft app makes to Incremental.
The request carries no device ID, no account and no gateway details: just a plain GET with an app-name User-Agent
(the Android app adds its version). Each app has a setting that switches the check off. The iPhone app never
checks: the App Store updates it.

## Format

One object, with a key per platform. A platform without a release is left out (or `null`); the apps then
report nothing.

```json
{
  "android": {"version": "0.2.0", "version_code": 1, "url": "https://loft.incremental.no/download/#android",
              "sha256": "…", "notes": "https://loft.incremental.no/download/#android"},
  "windows": {"version": "0.2.0", "url": "https://github.com/Incremental-AS/loft/releases/download/v0.2.0/Loft-windows-x64-0.2.0.zip",
              "sha256": "…", "notes": "…"},
  "linux":   {"version": "0.2.0", "url": "https://loft.incremental.no/download/#linux", "notes": "…"},
  "macos":   {"version": "0.2.0", "url": "https://loft.incremental.no/download/#mac", "sha256": "…"}
}
```

| Field | | Meaning |
|---|---|---|
| `version` | required | Semantic version (`0.2.1`). Apps compare it with their own; a newer one shows a notice. |
| `version_code` | Android | Integer `versionCode`; Android compares this instead of `version`. |
| `url` | required | `https://` only. Where the notice points: a download page or the file itself. |
| `sha256` | optional | Hex SHA-256 of the download, shown next to it (Windows builds are unsigned for now). |
| `notes` | optional | Release notes URL or a short line. |

`macos` is for Mac builds up to 0.2.0 (12): they show "Loft x.y.z is available…" in the menu, which opens `url`.
From build 13 the Mac updates through Sparkle and `appcast.xml` instead (`docs/mac-updates.md`); keep `macos` current
for the older builds. Apps ignore
unknown keys and refuse a non-`https` `url`. Parsers: `android/core/.../UpdateCheck.kt`,
`windows/src/Loft.Core/UpdateChecker.cs`, `linux/internal/update/update.go`,
`apple/Packages/LoftCore/Sources/LoftCore/UpdateCheck.swift` (all four also accept the same objects under a
top-level `"platforms"` key; the site publishes the flat form). The Mac sends a fixed `Accept-Language: *`, so the
system doesn't add the user's languages; settings switch: Settings › General › Check for updates.

Made with ❤️ in 🇳🇴 by Incremental (www.incremental.no)
