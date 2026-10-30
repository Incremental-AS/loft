# Loft design: trust model, sign-in, routes and sync

How Loft works and why. The code points here with "docs/design.md §X". The wire format of the API is
`api/openapi.yaml`; the pairing code is `docs/pairing.md`; the update check is `docs/releases-json.md`.

## A. Overview

Loft has two parts:

- **The gateway** (`gateway/`, Python): runs on a small Linux machine on the home or office network, in front of
  an SMB NAS. It signs in to the NAS **as the user**, so the NAS's own permissions apply: a user sees exactly what
  their NAS account can open, nothing more. The gateway keeps an index, a change feed, versions and a trash on top.
- **The apps** (macOS, iOS, Android, Windows, Linux): talk to the gateway over one REST API and show the NAS in the
  system's file manager. File contents are fetched when opened and dropped again after upload (§E.1).

### Routes and encryption

| Leg | `tunnel` mode | `direct` mode |
|---|---|---|
| App to gateway, home network | TLS, pinned | TLS, pinned |
| App to gateway, outside | TLS to the proxy (e.g. Cloudflare Tunnel), which **can read the traffic**; then the proxy's tunnel | TLS, pinned, **end to end** |
| Sign-in with the password, outside | Refused | Allowed: the connection is pinned |
| Gateway to NAS | SMB3, encryption per `smb_encryption` | same |

`public_mode = "none"` (the default) means home network or VPN only. The password is only ever sent over TLS to a
server whose key matches a pin from the pairing (§C). There is no trust on first use.

## B. The gateway

### B.1 Stack

- FastAPI + uvicorn in one process with **two listeners**: `direct` (TLS with the gateway's own key, default
  `0.0.0.0:8443`) and `public` (plain HTTP on `127.0.0.1:8080`, for a tunnel daemon on the same host).
- SMB in user space (`smbprotocol`), one connection cache per signed-in device, so sessions never mix and the NAS
  applies that user's permissions. SMB 3.1.1.
- Index: SQLite (WAL) on the gateway's local disk. SQLite on a network mount fails to lock, so the database never
  sits on the NAS.
- Thumbnails are decoded only in a separate, sandboxed worker (`loft-thumb@.service`), never in the gateway process.

### B.2 Credentials and device tokens

The gateway has to open SMB sessions as the user, and NTLM needs the password. Three designs were compared:

| Option | If the gateway's database and master key leak (e.g. from a backup) | Verdict |
|---|---|---|
| Password encrypted with a gateway-held key | Every stored password decrypts | Not good enough |
| Service account + a copy of the NAS's permissions | No passwords stored, but the gateway must re-implement the NAS's permission checks; one bug and user A reads user B's files | Rejected |
| **Per-device double wrap** | **Nothing decrypts**: the token secret is not in the database | Built |

How it works (`gateway/loft_gateway/crypto.py`):

```
device token = "nst_<device_id>.<32 random bytes>"     -> the device's secure store only
database     = sha256(secret)                           (no KDF needed: 256-bit random)
wrapped      = AESGCM(master_key,
                 AESGCM(HKDF(secret, salt=device_id), password, aad=device|user),
               aad=device|user)                         one blob per device
master_key   = a systemd credential (TPM-bound where the host has one), or a 0600 file in the container
```

- The password is decrypted only while a request presents that device's token. The SMB session it opens is cached
  in memory per device (30 min idle). Nothing about it is written to disk.
- **Revoke** (API or `loft-admin`) deletes that device's blob. When a user's last device is revoked, nothing is left
  that could decrypt to the password.
- **Rotation**: `/v1/auth/refresh` issues a new secret and re-wraps in one transaction. The previous token stays valid
  for 120 s with its own blob, so a lost response does not sign the device out. Lifetime 90 days, sliding.
- **NAS password changed**: SMB sign-in fails, the API answers `401 nas_credentials_invalid`, and the app asks the
  user to sign in again on a pinned route.
- **Why a token is dead**: `401 device_revoked`, `401 token_expired`, else `401 invalid_token`. The first two are
  only returned when the token's secret matches, so a forged token for a guessed device id learns nothing.
- **Brute force**: 5 failures per NAS user and 20 per IP in 15 min give 429. Many NAS boxes have no SMB lockout, so
  these limits may be the only wall; where the NAS has one, keep the gateway's limit below it so the gateway trips
  first. The audit log records sign-ins, failures, refreshes, revokes and every write, delete, move and restore.
- **Accounts**: `allowed_users_re` / `denied_users_re` choose which NAS accounts may sign in. Service accounts that
  can open everything (backup, replication) belong in `denied_users_re`.
- **Residual risk, stated plainly**: an attacker inside the running gateway process can capture the passwords of
  users who are active at that time. Every design where a gateway signs in as the user over NTLM has this; only
  Kerberos delegation avoids it. Mitigations: run nothing else on the gateway host, and use the hardened systemd
  units in `gateway/packaging/systemd/`.

### B.3 Housekeeping without a service account

Trash purge (30 days), version pruning (10 versions, 30 days), expired uploads and change-log pruning run lazily
under the user's own session while that user is active. Thirty days is a minimum, not a deadline, so nothing has
to act while nobody is connected.

Each chosen root has a hidden area `<root>/.loft/{uploads,versions,trash}/`. It lives inside the root, so a user
never needs write access above the folder they picked. It is hidden from every listing and refused as a name.
Upload staging is on the NAS, so the final step is a rename on the same share and the gateway's disk stays small.

`shares`, `blocked_shares` and `blocked_paths` can keep shares or folders off every device, whatever the account
can open (e.g. a backup share, even when an admin account signs in). A folder above a blocked path cannot be
renamed, moved or deleted from a device. Paths are compared case- and NFC-insensitively, `..` never climbs past the
share, and SMB 8.3 short names (`ABCDEF~1`) are refused, so an alias cannot reach a blocked folder.

### B.4 Route enforcement: by listener, never by header

- **direct**: TLS with the pinned key. Password sign-in exists only here. A request carrying `CF-Connecting-IP` on
  this listener is refused.
- **public**: plain HTTP for a tunnel daemon on the same host. `/v1/auth/login` answers 403 here.
- The service layer checks the route again on sign-in, so a wiring mistake cannot open password sign-in to the
  internet. In `public_mode = "direct"` the outside address is a port forward to the **direct** listener: same key,
  same pins, so sign-in works from outside too.
- Sign-in codes (`code_users_re`, empty by default): a device signed in on the direct route can mint a code that
  signs in another device over the internet, for example for an app store reviewer.

### B.5 Running a gateway

- `loft-gateway init` asks for the SMB server, `smb_encryption`, gateway name and addresses, writes `gateway.toml`,
  makes the TLS key and a standby key and the master key, and prints the pairing QR code.
- `loft-gateway doctor [--user U]` checks the config, keys, pins, certificate dates, SMB reachability, dialect,
  encryption and clock skew; with `--user` also a real sign-in.
- `loft-gateway pair` prints the pairing link and QR code (§C).
- `loft-admin` operates a running gateway: devices, audit log, sign-in codes, `config --effective`.
- `smb_encryption = "required"` (default): every SMB message is encrypted; a NAS that can't (SMB < 3, guest mapping)
  fails the sign-in. `"server"`: the NAS decides. There is no "preferred": the gateway never falls back on its own.
- The gateway never updates itself.

## C. Direct-route TLS, pins and pairing

The gateway has its own EC P-256 key with a self-signed certificate, and the apps pin the SPKI hash
(`base64(sha256(SubjectPublicKeyInfo))`). There are two pins, live and an offline standby, so a key rotation
needs no app update.

- **Pairing** (`docs/pairing.md`): `loft-gateway pair` prints a QR code and a link that carry the gateway's name,
  its home address, an optional public address with its mode, and the pins. The pins arrive with the address, so
  the very first connection is authenticated. No app has an address or pin built in, and there is no "type an
  address" field without a pin. The link's fragment never reaches any server.
- **Pin refresh**: `GET /v1/identity` over an already trusted route returns the current pins.
- **Probe**: `GET <direct>/healthz` with a 1.5 s timeout over a connection that enforces the pin. It must answer
  `{"service":"loft","route":"direct"}`. A pin mismatch is a failed handshake, which means the other route is used;
  it never means "accept any certificate".
- **Why not the public certificate on the home network (split-horizon DNS)**: the gateway would need the public
  hostname's private key and automated DNS renewal on a LAN machine; with one hostname for both routes the app
  can't tell which route it is on, and it must know, because password sign-in is direct-only; and it depends on
  every device using the router's DNS. With pinning, the only dependency is a stable LAN address.

## D. Sync

### D.1 Item identity

- An item's id is a **gateway-minted UUID**, stable across renames and moves.
- An item's version is a pair (`content_etag`, `meta_etag`): content and metadata are versioned separately, so a
  rename on one device and an edit on another do not collide.
- The index keeps a `name_key` (NFC + casefold, because SMB is case-insensitive) and the server's file id. The file
  id is a **move hint only**: when a rescan finds an item gone from its old path and the same file id at a new
  path, that is a move (same item id), not delete + create. Folders keep their children's ids.

### D.2 Change feed

- `GET /v1/changes` returns the latest state of every item changed since an anchor (`"<root epoch>.<seq>"`). An
  unknown epoch, or a seq older than the pruned window (30 days), answers **410 `anchor_expired`**, and the app
  re-enumerates.
- No push service: the app long-polls `GET /v1/changes?wait=25` (well under a proxy's 100 s limit) and refreshes
  when something arrives.
- **Changes made outside Loft** (over SMB): every folder listing reconciles that folder, and a client request
  triggers a background rescan of the root when the last one is more than 5 min old. The rescan runs as that user.

### D.3 Writes, conflicts and Office safe-save

- **Every write is compare-and-swap.** Before placing an upload the gateway re-reads the destination folder from
  the NAS, so an edit made over SMB after the app fetched the file is not silently overwritten.
- **On a conflict both copies are kept**: the upload lands as `Name (conflict, <device name>, YYYY-MM-DD HHMM).ext`.
  The original is never overwritten.
- **Office safe-save** (write a temp file, rename it over the original, delete the temp) reaches the app in three
  shapes. All three end as **one new version of the file**: the old content goes to versions, nothing goes to the
  trash, and the audit log shows one write.

  | What the system does | The app sends | The gateway does |
  |---|---|---|
  | modify in place | commit with `item_id`, `if_match` | modify, compare-and-swap |
  | create N **before** delete X (X still has name N locally) | commit with `replaces=X`, `if_match=X.etag` | the successor Y replaces X atomically; X's versions move to Y; the later `DELETE X` answers `superseded` |
  | delete X **before** create N | nothing special | DELETE moves X to the trash; a create of the same name in the same folder within 60 s pulls X out of the trash as Y's previous version |

  Temp and lock files (`~$*`, `*.tmp`, `Word Work File*`, `.~lock.*#`, `.DS_Store`, `._*` ...) never leave the
  device, and the gateway refuses them too. Side effect: a user's real `.tmp` files do not sync.

### D.4 Versions and trash

- **Modify**: the current file is renamed into `versions/`, then the upload is renamed into place: two renames on
  the same share, nothing copied. If a step fails the gateway reverses its renames. Kept: the newest 10 versions,
  none older than 30 days. **Restore** swaps current and chosen with two renames.
- **Delete** moves the item to the trash (30 days). A stale `if_match` answers 412, so a delete never removes
  content newer than what the device saw.
- Versions and trash are not a backup. Back up the NAS.

### D.5 Large files

Uploads go in chunks of at most 32 MB, each with a SHA-256 header. A wrong offset answers 409 with `received`, so
the app resumes from there; this survives gateway restarts because the partial file is on the NAS. At commit the
gateway reads the file back from the NAS and checks the whole-file SHA-256 before the atomic rename. Downloads
support `Range`, including suffix ranges.

### D.6 Known limits

- The NAS's mtime is used; the device's original mtime is not preserved yet.
- Long-polls each hold a worker thread (40 by default): fine for a household or a small office.
- SMB I/O for a commit runs inside the database lock, which serialises writes.

## E. Apps

### E.1 Local cache

- Items appear as placeholders. Content is fetched only when a program opens a file.
- A save is uploaded; once the gateway has acknowledged it and the file has not been open for `evict_after`
  (default 10 min), the local copy is dropped again. Pinned items ("keep on this device") are never dropped.
- A cache cap (default 5 GB) drops the least recently used, already uploaded, unpinned items first.
- Content the gateway has not acknowledged is never dropped. Offline, cached and open files can be edited;
  uploads wait and retry.

### E.2 Route choice

On start, on a network change and every 5 min the app probes the home address (§C). If it answers with a pinned
key, the app uses it; otherwise the public address from the pairing, if there is one. The app shows which route
it is on. The API is the same on both routes.

### E.3 What a device stores

Only the device token, in the platform's secure store (Keychain, Android Keystore, Windows Credential Manager,
Secret Service on Linux with a 0600 file fallback that says so), never the password. Sign-in with the password
needs a pinned route: the home network, a VPN, or a `direct` public address.

### E.4 Update check

Apps outside an app store fetch `https://loft.incremental.no/releases.json` (`docs/releases-json.md`) at most once a
day. It is the only request Loft makes to Incremental, and it can be switched off.

Made with ❤️ in 🇳🇴 by Incremental (www.incremental.no)
