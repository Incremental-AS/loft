# Third-party software in the gateway image

The gateway's Python dependencies come as wheels, and some wheels carry C libraries of their own
(`<package>.libs/`). Two of them carry GPL and LGPL code:

| Wheel | Bundled | Licence |
|---|---|---|
| `av` (PyAV) 19.0.2 | FFmpeg 9.0.2 | LGPL-3.0-or-later |
| | x264 (0.165, b35605a) | GPL-2.0-or-later |
| | x265 4.3 | GPL-2.0-or-later |
| | GnuTLS 3.8.13, alsa-lib 1.2.14 | LGPL-2.1-or-later |
| | LAME 3.101.0 (inside libavcodec) | LGPL-2.0-or-later |
| | Nettle 4.0, GMP 6.3.0, libunistring 1.4.2 | LGPL-3.0-or-later OR GPL-2.0-or-later |
| `pillow-heif` 1.8.1 | libheif 1.23.6, libde265 1.1.3 | LGPL-3.0-or-later |
| | x265 4.3 | GPL-2.0-or-later |

The Debian base of the image holds GPL and LGPL programs and libraries too. The full list, with
every other component and licence, is in each image's SBOM.

## What every image carries and what goes out with it

* **Inside the image**, `/usr/share/doc/loft-gateway/third-party/`: `sbom.spdx.json` (SPDX 2.3),
  `inventory.json`, `THIRD_PARTY_NOTICES.md` and `licenses/` (each Python package's licence files,
  each Debian package's copyright file, and the licence files of each bundled library from
  `notices/`).
* **Next to the image** (attached to the GitHub release of the same tag): the third-party report
  and the **source bundle**: the exact source of every GPL/LGPL/MPL part at the version shipped,
  with the scripts and patches that built it, and the source package of every Debian package in
  the image, with `SHA256SUMS` and `sources.json` (which component each file is the source of).
* The image's `org.opencontainers.image.licenses` label is the SPDX expression of what it holds.

## Files

* `components.toml`: for each wheel that bundles libraries, at the exact version in
  `requirements.lock`: which component every bundled file is, its licence, and its source (URL and
  SHA-256; where the SHA-256 comes from is noted).
* `thirdparty.py`: `collect` (SBOM and licence files from an image's file system), `sources` (fetch
  and verify the sources into a bundle), `check` (refuse an image whose GPL/LGPL parts have no
  source in the bundle, a bundled library nobody identified, or a wrong licenses label), `lock`
  (refuse a lock file whose wheels `components.toml` does not describe), `label`.
* `image-check.sh <image> <platform> <out-dir>`: all of it for one image and platform.
* `notices/<source>/`: the licence files of each bundled library's source (copied from a source
  bundle's `notices/` after review), so the image carries them.

## When a dependency changes

Bumping `av`, `pillow-heif` or `pillow` (or adding a wheel that bundles libraries) fails the
`lock` check and the image build until `components.toml` describes the new wheel: read its build
scripts at that tag for the bundled versions, URLs and checksums, list every file in its `.libs/`,
run `image-check.sh`, review the new `notices/`, and update the Dockerfile's licenses label to
what `thirdparty.py label` prints.

```sh
docker build -f gateway/packaging/docker/Dockerfile -t loft-gateway:dev gateway
gateway/packaging/third_party/image-check.sh loft-gateway:dev linux/amd64 /tmp/third-party/amd64
```
