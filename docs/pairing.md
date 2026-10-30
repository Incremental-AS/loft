# Pairing a device with a Loft gateway

`loft-gateway pair` (same as `loft-admin pair`) on the gateway prints a QR code and a link:

    https://loft.incremental.no/pair#<payload>

The payload sits in the fragment (after `#`), so it never reaches loft.incremental.no or any other server.
The Loft apps scan the QR code or open the link (universal link / app link; desktops also accept `loft://pair#<payload>`
and a pasted link).

`<payload>` is base64url (no padding) of compact UTF-8 JSON:

| Key | | Meaning |
|---|---|---|
| `v` | required | Format version, `1` |
| `name` | required | Gateway name shown in the app (`gateway_name`) |
| `lan` | required | `https://host:port` of the direct listener on the home network (`lan_url`) |
| `pins` | required | base64 SHA-256 of the direct key's SubjectPublicKeyInfo: live first, then standby |
| `pub` | optional | `https://` outside address (`public_url`) |
| `mode` | with `pub` | `tunnel`: a TLS-ending proxy (e.g. Cloudflare Tunnel) in front of the public listener. Normal web PKI, device tokens only, sign-in refused. `direct`: a port forward to the direct listener, checked against `pins`, sign-in allowed |

Example: `{"v":1,"name":"Home","lan":"https://10.0.0.5:8443","pins":["…","…"]}`

The code holds no secret. Anyone who has it still needs an account on the SMB server, and the gateway's `allowed_users_re` /
`denied_users_re` still apply. The pins are what make the first connection safe: the app sends a password only to a
server that proves it holds a pinned key, so a look-alike gateway can't collect passwords. That's why apps don't offer a
plain "type the address" field. "Set up manually" takes the address **and** a pin.

Apps must refuse a payload with an unknown `v`, a non-`https` URL or no pins, and must not use `pub` for sign-in unless
`mode` is `direct`. After pairing, pins are refreshed from `GET /v1/identity` as before.

Made with ❤️ in 🇳🇴 by Incremental (www.incremental.no)
