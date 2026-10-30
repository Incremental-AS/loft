# Loft for Linux

Your NAS folders in `~/Loft`, through your own Loft gateway. One static Go binary, `loft`, with a
FUSE file system (pure Go, [hanwen/go-fuse](https://github.com/hanwen/go-fuse) v2).

## Use

```sh
loft pair 'https://loft.incremental.no/pair#…'   # the link `loft-gateway pair` prints
loft login                                       # NAS user name + password
loft mount                                       # ~/Loft (or: systemctl --user enable --now loft)
```

| Command | |
|---|---|
| `loft pair <link\|code>` | Pair with a gateway. Also `loft://pair#…` and the bare code. |
| `loft pair --manual <url> <pin>` | Pair by hand: the gateway's home address **and** its key pin. There is no address-only pairing. |
| `loft login [--user U] [--password-stdin] [--share S --path P]` | Sign in. The password goes only to a pinned route (home address, or an outside address in `direct` mode); never through a tunnel. |
| `loft logout [--force]` | Removes this computer on the gateway, deletes the token and the cache. Refuses while changes wait for upload. |
| `loft status [--json]` | Gateway, account, folder, route, uploads, cache, token store. |
| `loft mount [dir] [--foreground]` | Mounts (default `~/Loft`) and returns once mounted; logs to `~/.local/state/loft/loft.log`. |
| `loft unmount [--lazy]` | |
| `loft daemon [--no-tray]` | Mount + sync + tray + daily update check. What `loft.service` runs. |
| `loft devices [revoke <id>]` | Devices signed in to your account. |
| `loft root [share[/folder]\|'*']` | The folder on the NAS that Loft shows. |
| `loft pin\|unpin <file>` | Keep a file on this computer (sets the `user.loft.pinned` xattr). |
| `loft config [set <key> <value>]` | `mountpoint`, `cache-limit` (5G), `evict-after` (10 min), `update-check` (on), `tray` (on). |

## How it behaves

- **Listings** come from the gateway, are cached for offline use, and are kept current by the change feed
  (`/v1/changes`, 25 s long-poll). A folder is read again when it is opened after 30 s, which also makes the
  gateway look at that NAS folder.
- **Reads** are on demand: Range requests in 1 MiB blocks (8 MiB read-ahead) into `~/.cache/loft`. Nothing is
  mirrored. The cache has a cap (default 5 GB); unpinned, unused files leave first. Uploaded files leave
  after `evict-after` minutes unused. Files with unsent changes, open files and pinned files never leave.
- **Writes** go to the cache and are uploaded when the file is closed (after 1 s, so an editor's
  write-temp-then-rename is one save) or right away on `fsync`, in resumable upload sessions (32 MiB chunks).
  A temp file renamed over a file becomes a new version of that file, not a delete and a create.
- **Conflicts:** every save is compare-and-swap on the version this computer had. If the NAS copy changed
  meanwhile, the gateway keeps both: your bytes land as `Name (conflict, <computer>, YYYY-MM-DD HHMM).ext`
  and the original keeps the NAS content.
- **Offline:** cached files stay readable and writable; new files can be made; changes wait in a queue that
  survives restarts. Folders can't be made, renamed or deleted offline. Uncached files can't be opened.
- **Names that never leave this computer:** Office lock files (`~$…`), `*.tmp`, `.DS_Store`, `._*`,
  `Thumbs.db`, `desktop.ini`, `.~lock.*`. The NAS is case-insensitive, so two names that differ only by case
  can't be made in one folder.
- **Routes:** home address first (pinned TLS), then the outside address. A `tunnel` outside address uses normal
  web PKI and carries device tokens only; sign-in needs the home address.
- **Token:** in the Secret Service (GNOME Keyring, KWallet, KeePassXC…). Without one it goes in
  `~/.config/loft/token.json` (0600) and Loft says so. The password is never stored.
- **Tray:** a StatusNotifierItem (KDE, GNOME with the AppIndicator extension, Xfce, Cinnamon, waybar…) with
  Loft's symbolic icons sent by name, so the desktop recolours them. Plain GNOME shows no tray; `loft status`
  says so.
- **Update check:** `https://loft.incremental.no/releases.json`, at most once a day, only from `loft daemon`;
  `loft config set update-check off` or `LOFT_NO_UPDATE_CHECK=1` turns it off. Expected shape (to be agreed
  with the website): `{"linux": {"version": "0.2.1", "url": "https://…", "notes": "…"}}` or the same under
  `{"platforms": {"linux": …}}`.

Files: `~/.config/loft/config.json` (pairing, account without secrets, settings), `~/.cache/loft/` (contents,
`index.json`, `meta.json`), `~/.local/state/loft/` (`pending.json` upload queue and change anchor,
`status.json`, `loft.log`, `update.json`).

## Build, test, package

Go (see `go.mod`), no C compiler: everything builds with `CGO_ENABLED=0`.

```sh
scripts/build.sh                 # dist/loft-linux-{amd64,arm64}/loft
go test ./...                    # unit tests (httptest, a private dbus-daemon for the tray test)
scripts/it.sh                    # integration test against gateway/scripts/dev_server.py (needs FUSE + Python 3.11+)
scripts/package.sh               # .deb, .rpm (nfpm), AppImage (appimagetool), tarball, SHA256SUMS
```

The tarball has `install.sh` (to `~/.local`, no root). Packages depend on `fuse3`.
