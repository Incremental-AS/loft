#!/usr/bin/env bash
# Third-party check of a gateway container image, for one platform:
#
#   gateway/packaging/third_party/image-check.sh <image> <platform> <out-dir>
#   e.g. image-check.sh loft-gateway:0.3.0 linux/arm64 /tmp/tp/arm64
#
# 1. Exports the image's file system (docker create + export; nothing in the image is run).
# 2. collect: <out>/report/{inventory.json,sbom.spdx.json,THIRD_PARTY_NOTICES.md,licenses/}.
# 3. sources: <out>/sources/ with the exact source of every GPL/LGPL/MPL part and every Debian
#    package, SHA256SUMS and sources.json (downloads are cached in $XDG_CACHE_HOME/loft-third-party).
# 4. check: refuses (exit 1) when a part that needs its source has none in <out>/sources, when a
#    bundled library is not explained by components.toml, or when the image's
#    org.opencontainers.image.licenses label is not the SPDX expression of what it holds.
# Then tars report/ and sources/ next to them with their SHA-256: these are what a release publishes.
set -euo pipefail
[ $# -eq 3 ] || { sed -n 2,4p "$0"; exit 2; }
IMAGE=$1 PLATFORM=$2 OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
TP="python3 -I $HERE/thirdparty.py"
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
ROOT=$OUT/rootfs
rm -rf "$ROOT" "$OUT/report" && mkdir -p "$ROOT"
trap 'rm -rf "$ROOT"' EXIT

cid=$(docker create --platform "$PLATFORM" "$IMAGE")
docker export "$cid" | tar -x -C "$ROOT" --no-same-owner --exclude='dev/*' 2>/dev/null || true
docker rm "$cid" >/dev/null
VERSION=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$IMAGE")
LABEL=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.licenses"}}' "$IMAGE")
ARCH=${PLATFORM#linux/}; ARCH=${ARCH//\//-}

$TP collect "$ROOT" "$OUT/report" --image-version "${VERSION:-unknown}" --arch "$ARCH"
$TP sources "$OUT/report/inventory.json" "$OUT/sources"
$TP check "$OUT/report/inventory.json" "$OUT/sources" --label "$LABEL"

base=$(basename "${IMAGE//[:\/]/-}")-$ARCH
tar -C "$OUT" -cf "$OUT/$base-third-party.tar" report
tar -C "$OUT" -cf "$OUT/$base-sources.tar" sources
(cd "$OUT" && sha256sum "$base-third-party.tar" "$base-sources.tar" | tee "$base.sha256")
echo "IMAGE_THIRD_PARTY_OK $IMAGE $PLATFORM"
