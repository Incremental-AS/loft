# Loft

Loft puts the files on your own NAS in Finder, Files, Explorer and your file manager, the way cloud drives do,
without a cloud. Files appear right away and download when you open them; what you save goes back to the NAS.

You run a small **gateway** at home, in front of your SMB shares (any NAS or server that speaks SMB3). The Loft apps
pair with it by scanning a QR code. The gateway signs in to the NAS as you, so the NAS's own permissions decide what
you see. Your password only ever travels over a connection pinned to your gateway's own key.

Website: https://loft.incremental.no

## Layout

| Path | What |
|---|---|
| `gateway/` | The gateway (Python, FastAPI). `.deb` and container image in `gateway/packaging/` |
| `api/openapi.yaml` | The REST API every app uses, generated from the gateway (`api/gen_openapi.py`) |
| `conformance/` | Black-box test suite for gateways and clients; `conformance/samba/` runs it against Samba in Docker |
| `apple/` | macOS and iOS app (Swift, File Provider) |
| `android/`, `windows/`, `linux/` | The other apps |
| `website/` | Source of loft.incremental.no |
| `docs/` | How it works (`design.md`: trust model, sign-in, routes, sync), the pairing format, `releases.json`, assistants (`mcp.md`), content search and its languages (`search.md`) |
| `ci/check.sh` | The whole check in one command: gateway tests, OpenAPI file, conformance in both modes, suite contracts when `SUITE_CONTRACTS` is set |

## Run a gateway

```sh
sudo apt install ./loft-gateway_0.2.0-1~debian13_amd64.deb   # or the container image: gateway/packaging/docker/
sudo loft-gateway init                              # asks for your NAS, makes keys, prints the pairing code
sudo loft-gateway doctor --user <your NAS account>  # checks SMB, encryption, shares, clock, keys
sudo systemctl enable --now loft-gateway
```

## Third-party source code

The container image and the `.deb` packages of each release include libraries under the GPL and LGPL (among them
FFmpeg, x264, x265, libheif and libde265; the container image also holds Debian's own packages). Their complete
source, with the scripts and patches that built them (pyav-ffmpeg's build scripts and patches at 9.0.2-1, included
because the GPL requires them), is attached to that release as `*-sources.tar`. The `*-third-party.tar` files next
to them hold each image's and package's SBOM (SPDX) and licence notices; `SHA256SUMS` lists them all.

| Version | Container image `ghcr.io/incremental-as/loft-gateway` | `.deb` packages |
|---|---|---|
| [0.2.1](https://github.com/Incremental-AS/loft/releases/tag/v0.2.1) | `loft-gateway-0.2.1-amd64-ghcr-sources.tar`, `loft-gateway-0.2.1-arm64-ghcr-sources.tar` | `loft-gateway-0.2.x-deb-sources.tar` |
| [0.2.0](https://github.com/Incremental-AS/loft/releases/tag/v0.2.0) | `loft-gateway-0.2.0-amd64-ghcr-sources.tar`, `loft-gateway-0.2.0-arm64-ghcr-sources.tar` | `loft-gateway-0.2.x-deb-sources.tar` |

Every later version carries the same files on its own [release](https://github.com/Incremental-AS/loft/releases).
Inside the image the SBOM and licence files are in `/usr/share/doc/loft-gateway/third-party/`. How they are made and
checked: [gateway/packaging/third_party/](gateway/packaging/third_party/README.md).

## Develop

```sh
python3 -m venv .venv && .venv/bin/pip install -e 'gateway[test]'
(cd gateway && ../.venv/bin/pytest)                       # gateway tests, no NAS needed
.venv/bin/pip install -r conformance/requirements.txt
PYTHON=.venv/bin/python conformance/run_dev.sh            # conformance against a local dev gateway
conformance/samba/run.sh                                  # ... and against Samba (Docker)
```

## Licence

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). The Loft name and icons are not part of the
licence: [TRADEMARKS.md](TRADEMARKS.md). Security reports: [SECURITY.md](SECURITY.md). Contributing:
[CONTRIBUTING.md](CONTRIBUTING.md).

Made with ❤️ in 🇳🇴 by Incremental (www.incremental.no)
