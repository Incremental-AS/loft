#!/usr/bin/env bash
# Build the gateway packages in Docker (run from anywhere; needs Docker + buildx).
#
#   gateway/packaging/build-in-docker.sh deb   [release...]   # default: debian:trixie ubuntu:noble
#   gateway/packaging/build-in-docker.sh image [tag]          # container image
#
# PLATFORMS (default: the host's own, e.g. linux/amd64). linux/arm64 on an amd64 host
# needs qemu binfmt registered on that host; without it the arm64 build fails fast.
# Output: dist/ next to the repo root.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
DIST=${DIST:-$REPO/dist}
HOSTP=linux/$(docker version --format '{{.Server.Arch}}')
PLATFORMS=${PLATFORMS:-$HOSTP}
mkdir -p "$DIST"

case "${1:-}" in
  deb)
    shift
    RELEASES=${*:-debian:trixie ubuntu:noble}
    for p in ${PLATFORMS//,/ }; do
      for img in $RELEASES; do
        echo "=== $img $p"
        docker run --rm --platform "$p" -v "$REPO:/src:ro" -v "$DIST:/out" "$img" \
          bash /src/gateway/packaging/deb/build.sh
      done
    done
    ;;
  image)
    TAG=${2:-loft-gateway:$(python3 -c "import tomllib;print(tomllib.load(open('$REPO/gateway/pyproject.toml','rb'))['project']['version'])")}
    if [ "$PLATFORMS" = "$HOSTP" ]; then
      docker build -f "$REPO/gateway/packaging/docker/Dockerfile" -t "$TAG" "$REPO/gateway"
    else
      # Multi-arch: build into an OCI archive (nothing is pushed anywhere).
      docker buildx build --platform "$PLATFORMS" -f "$REPO/gateway/packaging/docker/Dockerfile" \
        -t "$TAG" --output "type=oci,dest=$DIST/loft-gateway-image.oci.tar" "$REPO/gateway"
    fi
    ;;
  *)
    sed -n 2,9p "$0"; exit 2 ;;
esac
