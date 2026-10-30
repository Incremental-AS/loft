# Loft gateway container image

```sh
docker run -d --name loft --restart unless-stopped \
  -p 8443:8443 \
  -e LOFT_SMB_SERVER=nas.example.org \
  -e LOFT_LAN_URL=https://10.0.0.5:8443 \
  -v loft-etc:/etc/loft -v loft-state:/var/lib/loft \
  ghcr.io/incremental-as/loft-gateway:0.2.0
docker logs loft                                          # the pairing code from the first start
docker exec -it loft loft-container loft-gateway doctor --user alice
docker exec loft loft-container loft-gateway pair         # the pairing code again
```

First start (empty `/etc/loft`) runs `loft-gateway init` with these variables:

| Variable | |
|---|---|
| `LOFT_SMB_SERVER` | required: the SMB server |
| `LOFT_LAN_URL` | required: `https://<host address>:<published port>`, how devices at home reach the container |
| `LOFT_SMB_ENCRYPTION` | `required` (default) or `server` |
| `LOFT_NAME` | name shown in the apps (default `Loft`) |
| `LOFT_PUBLIC_MODE`, `LOFT_PUBLIC_URL` | outside access: `tunnel` (a proxy to port 8080) or `direct` (a port forward to 8443) |

`/etc/loft` holds the settings, the TLS keys (move `standby/` somewhere safe and offline) and the master key as a
0600 file: a container has no systemd credentials. Back up both volumes.

Ports: 8443 is the direct listener (TLS with the pinned key; sign-in). 8080 is the public listener for a tunnel
(device tokens only, trusts the tunnel's `CF-Connecting-IP`): publish it only to the tunnel, never to the internet.

## Thumbnails: what is different from the .deb

Thumbnails decode images, PDFs and videos that anyone with write access to a share controls. Under systemd
(`.deb`) every file gets a fresh process with no network, no file system, 512 MB and 40 s. A container has no systemd
sandbox, so the image does the part it can:

* the decoder runs as its own user (`loft-thumb`), not as the gateway (`loft`), so it can't read the master key,
  the TLS key or the index (`/etc/loft` and `/var/lib/loft` are 0700 `loft`);
* one fresh process per file, with limits on memory (address space), CPU time, open files and processes, killed after
  40 s, at most 4 at once;
* it **does** share the container's network and can read whatever the image makes world-readable.

For the full sandbox, use the `.deb` on a systemd host.
