# Loft for Windows

A tray app for Windows 10 (1809+) and 11. The Loft folder is a Cloud Files sync root, so it sits in
Explorer's navigation pane like OneDrive: files are placeholders until you open them, "Always keep on
this device" and "Free up space" work, and saves go back to your gateway.

| Project | Target | What it is |
|---|---|---|
| `src/Loft.Core` | net8.0 (any OS) | Pairing (`docs/pairing.md`), SPKI-pinned routes, the gateway API client (token rotation, change feed, chunked resumable uploads, Range downloads), sync decisions, cache and offline-delete rules, update check |
| `src/Loft.Sync` | net8.0-windows | Cloud Files API (via CsWin32), the sync engine, Credential Manager token store, tray icon, `loft:` protocol |
| `src/Loft.App` | net8.0-windows, WinUI 3 | Onboarding (pair, sign in, choose folder), settings, tray menu |
| `tests/Loft.Core.Tests` | net8.0 | xUnit; runs on Linux. Also checks the app's UI strings (`src/Loft.App/Strings/`, see its README). Set `LOFT_DEV_LAN`, `LOFT_DEV_PUBLIC`, `LOFT_DEV_PIN` to also run against `gateway/scripts/dev_server.py` |
| `tests/Loft.Sync.Tests` | net8.0-windows | xUnit; Windows only: a real sync root driven by an in-memory gateway. Run it from a desktop session: Credential Manager has no store in a key-based SSH session (win32 error 1312) |

## Security

- A password is only sent over TLS to a server whose SPKI SHA-256 matches a pin from the pairing:
  the home address, or an outside address in `direct` mode. A `tunnel` outside address uses normal web
  PKI and device tokens only; sign-in there is refused before anything is sent.
- The device token lives in Windows Credential Manager (this user, this PC). Never the password.
- The only request to Incremental: `https://loft.incremental.no/releases.json`, at most once a day, and
  it can be switched off in Settings.

## Local space

- Opening a file downloads it. A file saved here goes to the NAS and its local bytes are dropped
  10 minutes later, unless it's kept on this device.
- "Space for downloaded files" (Settings, default 5 GB): over that, Loft frees the files used longest
  ago. Never a file kept on this device ("Always keep on this device", on it or a folder above it), one
  with changes not yet uploaded, one that's open, or one used in the last 5 minutes.
- A delete made while the gateway can't be reached happens here at once and is sent later. If the file
  changed on the NAS meanwhile, it isn't deleted there (nor are the folders above it) and it comes back.

## Packaging

Unpackaged and self-contained (a folder with `Loft.exe`, zipped), for x64 and ARM64. Builds are
unsigned for now, so SmartScreen warns on first start of a downloaded zip; the download page lists the
SHA-256. An unsigned MSIX can't be installed by ordinary users, so there is no MSIX until there is a
signing certificate.

## Build

```
dotnet test tests/Loft.Core.Tests                 # any OS
dotnet build src/Loft.Sync                        # any OS (compile check)
dotnet test tests/Loft.Sync.Tests                 # Windows, from a desktop session
dotnet publish src/Loft.App -c Release -r win-x64 --self-contained -p:Platform=x64 -o out/Loft       # Windows
dotnet publish src/Loft.App -c Release -r win-arm64 --self-contained -p:Platform=ARM64 -o out/Loft   # Windows, ARM64
```

CI: `.github/workflows/windows.yml` (Loft.Core tests on Linux; on Windows Server 2022 the Loft.Sync
tests, the x64 build and a start-up smoke test; the ARM64 build is cross-compiled and not run).

## Verified on a Windows 11 desktop

Windows 11 Enterprise (build 26300), a real interactive session over RDP, Loft built on the PC with
the .NET 8 SDK, against `gateway/scripts/dev_server.py` (October 2026). Screenshots were taken in the
session.

- Install: unzip into `%LOCALAPPDATA%\Programs\Loft` and start `Loft.exe`. A zip made on the same PC
  has no mark of the web, so SmartScreen didn't show; a downloaded zip will warn (not tested).
- Pairing: a `loft://pair#…` link opened with `Start-Process` goes to the running Loft (one process)
  and fills in the pairing page; pasting an `https://loft.incremental.no/pair#…` link works too.
- Sign-in: a wrong password says so; the right one signs in over the pinned address. Folder picker
  lists shares and folders.
- Explorer: "Loft" in the navigation pane with the Loft icon; placeholders with cloud status; opening
  a file in Notepad downloads it; Ctrl+S uploads the edit (checked on the gateway's disk); a new file
  and a new folder upload; rename, move and delete reach the gateway (a delete goes to its trash);
  "Always keep on this device" downloads the whole file (same SHA-256 as the NAS), "Free up space"
  drops it; a change made on another device appears in an open Explorer window within seconds.
- A path inside a folder never opened (e.g. `Deep\Level1\Level2\deep.txt`) opens: Windows asks Loft to
  list each folder on the way.
- Tray: the house glyph, black on a light taskbar and white on a dark one (switches live); the menu
  shows the state ("Up to date · Direct", "Can't reach home. Retrying…", changes waiting), Pause,
  Open in Explorer, Settings and Quit. The menu itself stays light in dark mode.
- Settings: sign out (the folder leaves Explorer's navigation pane; downloaded files stay as plain
  files), the update-check switch, start at sign-in (a `Run` value with `--background`; after a Windows
  sign-out and sign-in Loft starts silently in the tray), the cache size.
- Norwegian: with Windows' display language set to Norwegian (bokmål), the tray menu and Settings
  are in Norwegian.
- Signing in again on the same folder matches files that are the same as on the NAS instead of
  uploading them as conflict copies, and lists what's only on the NAS.
- Offline: deletes are queued and sent on reconnect; a file changed on the NAS meanwhile comes back.
- Cache: with a 20 MB limit, the least recently used downloads were freed and a pinned file was kept.

- A Word-style safe save (temp file, renames, delete) ends as one new version of the same file.

Not verified on a desktop yet: SmartScreen on a downloaded zip, Windows 10, ARM64 hardware, a real
NAS behind the gateway, Office itself, the dark-mode tray menu (it stays light).
