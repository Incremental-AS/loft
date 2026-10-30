# Security

Please report security problems privately to **hello@incremental.no**, not in a public issue. Write "Loft security"
in the subject. We answer within a few working days and keep you posted until it is fixed. We credit reporters in
the release notes unless you'd rather we didn't.

Please include what you found, how to reproduce it, which version (gateway `GET /healthz`, app "About") and what an
attacker could do with it. Give us reasonable time to ship a fix before you publish.

## In scope

* The gateway (`gateway/`): authentication, device tokens, the wrapped SMB credential, path handling (escaping a
  share or a blocked path), the two listeners and which one allows sign-in, the thumbnail sandbox, login limits.
* The pairing code and pin checks (`docs/pairing.md`): any way to make an app send a password to a server that does
  not hold a pinned key.
* The apps (`apple/`, `android/`, `windows/`, `linux/`): token storage, pin checks, TLS, what reaches disk.
* The API (`api/openapi.yaml`) as implemented, and the container image and `.deb` we publish.
* `loft.incremental.no` where it serves the pairing page, downloads and `releases.json`.

## Out of scope

* Your SMB server, its accounts and permissions (Loft signs in as the user; the server decides what they may open).
* A TLS-ending proxy you put in front of the public listener (`tunnel` mode): by design it can read that traffic.
* Attacks that need root on the gateway host, or an already unlocked device.
* Denial of service by volume, missing hardening headers without an exploit, reports from automated scanners
  without a demonstrated problem.

## Supported versions

The latest release of each component. Fixes are not back-ported.
