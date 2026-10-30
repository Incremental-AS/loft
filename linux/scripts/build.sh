#!/usr/bin/env bash
# Static binaries for amd64 and arm64 (CGO_ENABLED=0, no C toolchain needed).
#   linux/scripts/build.sh            -> linux/dist/loft-linux-{amd64,arm64}/loft
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${VERSION:-0.2.0}
ARCHES=${ARCHES:-amd64 arm64}
for arch in $ARCHES; do
  out=dist/loft-linux-$arch/loft
  CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -trimpath \
    -ldflags "-s -w -X main.version=$VERSION" -o "$out" ./cmd/loft
  echo "built $out ($(du -h "$out" | cut -f1))"
done
