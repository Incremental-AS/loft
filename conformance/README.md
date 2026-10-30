# Loft conformance suite

Black-box tests of a running Loft gateway, against the API in `../api/openapi.yaml`. Every JSON answer is
validated against the schema of its operation, and an error status or code the spec doesn't list fails the test.
Use it to check a gateway (CI runs it against Samba, see `samba/`), and as the reference for what a Loft client
can rely on.

```sh
pip install -r conformance/requirements.txt
export LOFT_PAIRING='https://loft.incremental.no/pair#…'   # from `loft-gateway pair`
export LOFT_USER=alice LOFT_PASSWORD=…                     # an SMB account the gateway may sign in
export LOFT_PUBLIC_URL=http://127.0.0.1:8080               # optional: the public listener
pytest conformance/
```

| Variable | |
|---|---|
| `LOFT_PAIRING` | Pairing link or payload. Gives the LAN URL, the pins and (if set) the public URL + mode |
| `LOFT_DIRECT_URL`, `LOFT_PINS` | Instead of `LOFT_PAIRING`: `https://host:port` of the direct listener and its SPKI pins, comma-separated |
| `LOFT_PUBLIC_URL` | The public route. Without it, the public-route tests are skipped |
| `LOFT_PUBLIC_MODE` | `tunnel` (default: sign-in must be refused there) or `direct` (pinned, sign-in allowed) |
| `LOFT_USER`, `LOFT_PASSWORD` | The account |
| `LOFT_SHARE` | Share to work in (default: the first one the account sees) |
| `LOFT_ROOT_PATH` | Folder in the share used as the root (default: the share itself) |

The suite only works inside a folder it makes (`loft-conformance-<random>`) and moves that folder to the root's
trash at the end. It signs in several devices and signs them out again. It makes one wrong-password attempt,
so running it more than a few times within the gateway's `login_window_s` turns that test's answer into
`429 too_many_attempts` (also accepted).

The direct listener is trusted only by its pin (`loftclient.py`, `pinned_transport`): the key is checked right after
the TLS handshake, before any request byte, on every connection. Clients must do the same.

`run_dev.sh` runs everything against `gateway/scripts/dev_server.py` (local folders instead of SMB);
`PUBLIC_MODE=direct run_dev.sh` checks the `direct` public mode.
