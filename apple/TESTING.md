# Loft client v0 — first test on a Mac

Build: `Loft-0.1.0-7c55121.dmg` (0.1.0 build 3, notarized, stapled; sha256 `53db80b1413f…486377042`).
Needs macOS 14 or later. Start at home, on the same network as the NAS: the first
sign-in only works on the direct route.

Keep a log running in Terminal for the whole test; it is what I need when something
goes wrong:

    log stream --info --predicate 'subsystem == "no.incremental.loft"' | tee ~/Desktop/loft.log

## 1. Install and sign in (at home)
- [ ] Open the dmg, drag Loft to Applications, open it. Gatekeeper opens it without a warning.
- [ ] macOS asks for local network access: **Allow**. (If it never asks and sign-in says
      you're not at home, check System Settings › Privacy & Security › Local Network.)
- [ ] First-run window: Welcome → Pair with your gateway (open the `loft://pair#…` link from
      `loft-gateway pair`, or paste it) → confirm the gateway → Sign in with the NAS account → Done.
      A Mac that ran 0.1.x skips pairing: it is already paired with the same gateway.
- [ ] The first time Finder shows the Loft location it says «Loft» is not enabled, with an
      **Enable** button: click it (or System Settings › General › Login Items &
      Extensions, turn Loft on). Until then the location stays empty.
- [ ] "Open Loft in Finder": the Loft location shows in the Finder sidebar with the
      folder's contents. The menu bar icon settles on idle, the menu says up to date.
- [ ] Log has no `-34018` (keychain group missing from the provisioning profile).

## 2. Everyday files
- [ ] Open a file that is on the NAS: it downloads, then opens.
- [ ] Open a large file (1 GB+): Finder shows download progress.
- [ ] Copy a new file in; it shows on the NAS (another computer, or DSM File Station).
- [ ] Edit a text file in TextEdit and save; the NAS copy has the change.
- [ ] Word, Excel, PowerPoint: open, edit, save a few times (AutoSave on and off).
      On the NAS there is still exactly one file, with the new content, and no
      `~$…` / `.tmp` leftovers.
- [ ] Rename a file, move it to another folder, make a new folder: the NAS follows.
- [ ] Delete a file: it disappears from the NAS share (it goes to the gateway trash, 30 days).
- [ ] Change or add a file on the NAS from another computer: it appears on the Mac
      within about 5 minutes, without touching anything (the gateway rescans the NAS
      every 5 minutes).

## 3. Conflicts
- [ ] Open the same file on the Mac and on another computer, save on the other one
      first, then on the Mac. Expected: the NAS keeps the other computer's version, and
      the Mac's version appears next to it as `Name (conflict, <Mac>, <time>)`.
- [ ] Delete a file on the Mac that was just changed on the NAS: the delete is refused
      and the file comes back.

## 4. Away from home
- [ ] Switch the Mac to a phone hotspot. Browsing, opening and saving keep working
      (now through the gateway's outside address); the log shows `route publicInternet`.
- [ ] Back on home Wi-Fi: within 5 minutes the log shows `route direct`.
- [ ] Wi-Fi off: the menu says offline with the number of changes waiting; save
      something; Wi-Fi on: it uploads by itself.

## 5. Space, pause, account
- [ ] Settings › Storage › "Free up space after" = 5 min. Save a file, leave it alone
      ≥ 6 min: its cloud icon comes back (local copy removed, file still listed).
- [ ] Right-click a file › Keep Downloaded: it is never removed, however long you wait.
- [ ] Pause syncing: Finder says syncing is paused; changes on the NAS don't arrive.
      Resume: they do.
- [ ] Quit and reopen Loft; restart the Mac: it comes back without asking anything
      (with "Open Loft at login" on).
- [ ] Settings › Account shows this Mac (and other devices if any). Sign out: the Loft
      location goes away. Sign in again with the same folder: same location, nothing
      downloads twice.

## Not in v0 (don't report these)
5 GB cache cap · the Mac's own modified date (the NAS sets it) · bundle documents
(.rtfd, .pages packages) and symlinks stay on the Mac only · no versions window.

## Send back
`~/Desktop/loft.log`, plus for each failed box: what you did, what you expected,
what happened (a screenshot of the menu or Finder helps).
