# Loft for assistants: the MCP endpoint

The gateway serves the [Model Context Protocol](https://modelcontextprotocol.io) at `/mcp`, so an assistant
(Jeevo, or any MCP client) can read and save files in a user's Loft. The user signs the assistant in to **their own
account**, then chooses in the assistant which folder it works in and what else it may read. Code:
`gateway/loft_gateway/mcp.py` (endpoint, tools, access) and `gateway/loft_gateway/oauth.py` (sign-in).

## Signing in

Standard MCP authorization: OAuth 2.1, PKCE (S256), dynamic client registration. An MCP client that supports it
needs nothing Loft-specific beyond the server URL, `<assistant_url>/mcp` (e.g. `https://loft.incremental.no/mcp`;
`assistant_url` defaults to `public_url`, and pairing links always carry `public_url`).

1. `POST /mcp` without a token answers `401` with
   `WWW-Authenticate: Bearer realm="loft", resource_metadata="<base>/.well-known/oauth-protected-resource/mcp"`.
2. Metadata: `GET /.well-known/oauth-protected-resource[/mcp]` (RFC 9728: `resource` = `<base>/mcp`,
   `authorization_servers` = `[<base>]`) and `GET /.well-known/oauth-authorization-server` (RFC 8414).
3. `POST /oauth/register` (RFC 7591). Public clients only (`token_endpoint_auth_method: none`); redirect URIs must be
   https (http only to loopback), are matched exactly, at most 5. Registrations that never sign anyone in are
   dropped after a day. One registration may serve all of a client's users. With `assistant_return_hosts` set
   (e.g. `["api.jeevo.ai"]`), only those hosts and loopback are accepted, here and on the sign-in page: anyone can
   register a client, so this keeps a look-alike client from using your Loft's sign-in page for phishing.
4. `GET /oauth/authorize` with `response_type=code`, `client_id`, `redirect_uri`, `code_challenge` (S256),
   `state`, optional `resource` (`<base>/mcp`). The page names the client and the host it returns to, and asks for an
   **approval code**. It never asks for the NAS password: it is served through the tunnel, where TLS ends at the
   edge (design.md §B.4). At most 20 unanswered pages per IP and 2000 in all; a page lasts 15 minutes.
5. The approval code comes from a Loft app where the user is signed in (Settings › Connect an assistant,
   `POST /v1/auth/assistant-codes`; from anywhere, or only on the home network with
   `assistant_codes_over_internet = false`), or
   from the gateway host: `sudo loft-gateway mcp code --user <nas user>` (asks for that account's NAS password). It
   looks like `XXXX-XXXX-XXXX`, works once, within 10 minutes. Wrong guesses are limited per code (5), per IP (20)
   and for everyone together (`assistant_code_max_fail_global`, 500) per 15 minutes.
6. Approve: back to `redirect_uri` with `code`, `state` and `iss`. Cancel: `error=access_denied`.
7. `POST /oauth/token` (form): `grant_type=authorization_code`, `code`, `code_verifier`, `client_id`,
   `redirect_uri` → `{"access_token", "token_type": "Bearer", "expires_in": 7200, "refresh_token"}`. The code works
   once, within 5 minutes; using it a second time ends the connection the first use made.
8. `grant_type=refresh_token`, `refresh_token`, `client_id` → a new access token and **the same refresh token**: it
   doesn't rotate. It lasts 90 days from its last use, or until the user signs the connection out. Access tokens
   that a refresh replaced keep working until their own 2 hours are up (at most 16 at once per connection), so every
   device that refreshed has a working one.

   **Why it doesn't rotate** (2026-10-31): Jeevo syncs one connection, refresh token included, end-to-end to all of
   a user's devices, and each device refreshes by itself. A desktop may get the phone's newer token only hours
   later. With rotation and reuse detection, its first refresh with the token the phone had already used would look
   stolen and end the connection on every device, within a day for anyone with a phone and a Mac. A sign-in per
   device would avoid that but costs the user. The trade-off: a copied refresh token works until it is idle for 90
   days or the connection is signed out, and the gateway can't tell a copy from another of the user's devices.
   What limits it: the refresh token is bound to its client id, every refresh is in the audit log with its IP, and
   signing the connection out in the device list ends every token at once. A copy can't open more than the user
   approved either: widening access needs a fresh approval code (*Widening needs the user*, below; decided 2026-11-02).
   (OAuth 2.1 §4.3.1 asks public clients for rotation or sender-constrained tokens; neither works with tokens
   synced between devices.)

The connection then appears in the user's device list in the Loft apps (kind `mcp`, named after the client), where
they can sign it out like any device. The NAS password is wrapped under each access token and, separately, under
the refresh token, exactly like an app's (design.md §B.2): neither the database nor a backup can decrypt it.

**By token instead** (clients without OAuth, or testing): on the gateway host,
`sudo loft-gateway mcp add --user U --name N [--working-folder PATH] [--read PATH ...] [--read-all] [--read-only]`
prints a bearer token that doesn't expire. `loft-gateway mcp list` and `loft-gateway mcp revoke ID` manage both kinds.
In the container: `docker exec -it loft loft-container loft-gateway mcp ...`.

## Which accounts

`assistant_users_re` (full match, case-insensitive) chooses the NAS accounts that may connect an assistant, apart
from `allowed_users_re`, which chooses who may sign in to the apps. An account needs both, and `denied_users_re`
refuses either way. The default, `.*`, is every account that may sign in: connecting needs an approval code from a Loft
app the user is signed in to, and the assistant opens only what the user approves, so it reaches nothing the account
can't already reach through the apps. Name accounts to narrow it, or set it empty for nobody. Taking an account out
ends its connections at their next call (`403 account_not_allowed`).

## What a connection may open

A connection is the user's NAS account, so the NAS's permissions apply, and blocked shares and paths stay blocked.
Within that, it opens **nothing until the user chooses**, in the assistant:

- **A working folder**: read and save. Saves, moves and deletes happen only inside it (not the folder itself).
- **Folders it may read**, or the whole drive (`""`). The working folder is readable too.
- Each choice is **general** or tied to a **context**: a label the assistant attaches to a call, such as a project or
  conversation id. A call gets the general choices plus those of its context; a context's working folder replaces
  the general one for that call. Labels mean nothing to Loft.

The context goes in `_meta` of `tools/call`:
`{"name": "write_file", "arguments": {...}, "_meta": {"no.incremental.loft/context": "conv-123"}}`. No context:
general choices only.

These choices bind the model, and anything else that uses the connection's tools. Whoever holds the token can also
call `loft/access/set`, but only within what the user approved (next section). Read-only connections (`--read-only`)
can't have a working folder.

### Widening needs the user

The gateway keeps, per connection, the folders the user has approved, each for **read** or for **read and save**
(write; it covers read). Choices made in the first 15 minutes after the connection was made (setting it up in the
assistant, right after the user approved the sign-in) are approved by the sign-in: a working folder for read and
save, read folders for read. So an assistant's setup should start from the folders the user picks, never from the
whole drive, which would stay approved.

After that, `loft/access/set` may narrow access, or choose with the token alone anything an approval covers: a read
folder inside an approved folder, a working folder inside one approved for read and save, for any context. Anything
else needs an **approval code** for the same account in `approval_code` (the same codes as for signing in: Loft app ›
Settings › Connect an assistant, or `loft-gateway mcp code` on the host). That includes making a folder that was
approved for reading the working folder. Without a code the call fails with `data.error` = `approval_needed` and
`data.paths` = `[{"path", "access": "read" | "write"}]`, and nothing changes; a folder to `create` is made only after
the approval. A code is used up only when it approves something, or when it belongs to another account.
Narrowing doesn't take an approval back, so a folder the user approved can be chosen again later without a code;
signing the connection out ends everything. Every approval and refusal is in the audit log, and
`loft-gateway mcp list` shows each connection's approvals.

Approvals compare paths per name after NFC, ignoring case (Samba does), but not `casefold()`: `Straße` and `Strasse`
are two folders on the NAS, and an approval of one doesn't cover the other.

Honest limit: a code approves whatever the request carries, and the user sees the paths only in the assistant, not
in Loft; it also works for any connection of the account. Giving out a code is the user's decision either way (it
could also sign in a new connection). Later: the Loft app approves a pending request that lists its paths.

### Settings methods (JSON-RPC on `/mcp`, same bearer token; not tools, so a model never sees them)

| Method | Params | Result |
|---|---|---|
| `loft/folders` | `{"path": ""}` | `{"path", "folders": [{"name", "path"}]}`: the account's shared folders (`""`), or the folders in one. For a folder picker; independent of what is granted. |
| `loft/access/get` | `{}` or `{"context": "c"}` | `{"read_only", "general": {"working_folder", "read": []}, "contexts": {"c": {...}}}`, plus `effective` (what a call with that context gets) when a context is given |
| `loft/access/set` | `{"context"?: "c", "working_folder"?: "home/ana/Jeevo" \| null, "create"?: true, "read"?: ["home/ana/Docs", ...], "approval_code"?: "XXXX-XXXX-XXXX"}` | The same as `loft/access/get` with that context. Only the keys given change; `null` / `[]` clear. `create` makes a missing working folder (its parent must exist). Up to 200 read folders per context. `approval_code`: the user's code, when this opens more than they approved. |

Errors are JSON-RPC errors (`-32602`, a readable `message`, `data.error` = the gateway's error code).

## Tools

Paths start at the **top of the user's drive**: shared folder, then folders, with `/`
(`home/ana/Jeevo/Meetings/2026-10-31 Weekly.md`). The same rule for every tool and every grant; a client that keeps
its own layout under the working folder joins the two (`<working_folder>/Music/song.mp3`). `list_folder` with no path
shows the working folder and the readable folders, and the path down to them.

| Tool | Does |
|---|---|
| `list_folder` | Files and folders in a folder, 200 per page (`cursor`) |
| `search_files` | Names containing a text, in a folder and below, across every readable folder (index; complete after each folder's first full scan) |
| `read_file` | Text of a file, 20 000 characters per call (`offset`). Plain text, and the text of PDF, .docx, .pptx (with speaker notes), .xlsx, .odt, .odp, .ods, extracted in the sandboxed decoder (`thumbworker.py`), never in the gateway process |
| `download_file` | Raw bytes as an embedded resource (base64), 2 MiB per call (`offset`, `length`) |
| `write_file` | Save `text` or `content_base64`, up to 8 MiB; `if_exists`: `rename` (default, "name (2).ext"), `replace`, `fail` |
| `upload_start` / `upload_chunk` / `upload_finish` | Bigger files: size + SHA-256 first, then chunks of up to 8 MiB in order, then the path |
| `create_folder` | A folder and any missing parents |
| `move` | Move or rename inside the working folder; never replaces |
| `delete` | To the trash |

Read tools carry `readOnlyHint`; `delete` carries `destructiveHint`. Results have a short text for the model and
`structuredContent` for programs. `serverInfo.name` is `loft`.

**The tool list is the same for every connection and every choice of folders**: a call outside what is granted, or a
write on a read-only connection, is refused at call time with a message saying what is open. Clients pin the list
(Jeevo hashes it), so names, descriptions and schemas change only with a release, and are announced.

## Writes

Each granted folder is its own index root, as when a Loft app picks a folder. Saves go through the working folder's
root, with the same upload and commit as the apps: saving over a file keeps the old one as a version, a delete goes to
Loft's trash, staging (`.loft/`) is inside the working folder, and every signed-in device sees the change.

## Transport

Streamable HTTP, stateless: `POST /mcp` with one JSON-RPC message, answered with one JSON body. No SSE stream, no
`Mcp-Session-Id`; `GET` and `DELETE` answer 405. A request with an `Origin` header is refused (a browser page has
no business here). Bodies are capped at 16 MiB. Protocol versions 2025-06-18, 2025-03-26 and 2024-11-05.

## Request size

Every body is capped as it arrives, chunked or not: `/mcp` 16 MiB, upload chunks `upload_chunk_max` (32 MiB), anything
else 64 KiB. Over the cap: `413`, before the body is held in memory. An upload chunk is checked for a token before it is read
into anything, and is spooled to disk (the unit sets `TMPDIR=/var/tmp`, as /tmp is often a tmpfs), not memory. The packaged systemd unit caps the gateway at
`MemoryMax=2G`.

## Routes the edge must pass to the gateway

`POST /mcp`, `GET /.well-known/oauth-protected-resource`, `GET /.well-known/oauth-protected-resource/mcp`,
`GET /.well-known/oauth-authorization-server`, `POST /oauth/register`, `GET` and `POST /oauth/authorize`,
`POST /oauth/token`. No bot challenge on any of them (API clients, and a sign-in page that must not be framed or
rewritten); no caching.
