#!/usr/bin/env bash
# Build the loft-gateway .deb INSIDE a clean container of the target release, so the
# bundled venv matches that release's python3:
#
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/out:/out" debian:trixie \
#       bash /src/gateway/packaging/deb/build.sh
#
# (packaging/deb/build-in-docker.sh does exactly that per release and architecture.)
# Result: /out/loft-gateway_<version>-1~<release>_<arch>.deb
set -euo pipefail
SRC=${SRC:-/src}
OUT=${OUT:-/out}
export DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1

apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv ca-certificates >/dev/null
. /etc/os-release
PYV=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
NEXT=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]+1}")')
ARCH=$(dpkg --print-architecture)
VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('$SRC/gateway/pyproject.toml','rb'))['project']['version'])")
REL=${VERSION_CODENAME:-$ID}
PKG=loft-gateway_${VERSION}-1~${ID}${VERSION_ID%%.*}_${ARCH}

ROOT=$(mktemp -d)/pkg
mkdir -p "$ROOT/opt/loft" "$ROOT/DEBIAN"
# The venv lives at its final path, so its scripts' #! lines are right.
python3 -m venv /opt/loft/venv
/opt/loft/venv/bin/pip install -q --require-hashes -r "$SRC/gateway/requirements.lock"
cp -r "$SRC/gateway" /tmp/gw && rm -rf /tmp/gw/*.egg-info /tmp/gw/build
/opt/loft/venv/bin/pip install -q --no-deps /tmp/gw
/opt/loft/venv/bin/python -c "import loft_gateway.thumbworker, av, pillow_heif, pypdfium2"
/opt/loft/venv/bin/pip uninstall -q -y pip setuptools wheel 2>/dev/null || true
find /opt/loft/venv -name '__pycache__' -type d -prune -exec rm -rf {} +
cp -a /opt/loft/venv "$ROOT/opt/loft/venv"

install -d "$ROOT/usr/bin" "$ROOT/lib/systemd/system" "$ROOT/usr/share/doc/loft-gateway" \
           "$ROOT/usr/share/loft-gateway"
ln -s /opt/loft/venv/bin/loft-gateway "$ROOT/usr/bin/loft-gateway"
ln -s /opt/loft/venv/bin/loft-admin "$ROOT/usr/bin/loft-admin"
install -m 644 "$SRC"/gateway/packaging/systemd/* "$ROOT/lib/systemd/system/"
install -m 644 "$SRC/LICENSE" "$ROOT/usr/share/doc/loft-gateway/copyright"
[ -f "$SRC/NOTICE" ] && install -m 644 "$SRC/NOTICE" "$ROOT/usr/share/doc/loft-gateway/NOTICE"
install -m 644 "$SRC/gateway/packaging/deb/README.Debian" "$ROOT/usr/share/doc/loft-gateway/"
install -m 644 "$SRC/gateway/packaging/gateway.toml.example" "$ROOT/usr/share/loft-gateway/"

SIZE=$(du -sk "$ROOT" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<EOF
Package: loft-gateway
Version: ${VERSION}-1~${ID}${VERSION_ID%%.*}
Architecture: ${ARCH}
Maintainer: Incremental AS <hello@incremental.no>
Installed-Size: ${SIZE}
Depends: python3 (>= ${PYV}), python3 (<< ${NEXT}), adduser, systemd, ca-certificates
Recommends: qrencode
Section: net
Priority: optional
Homepage: https://loft.incremental.no
Description: Loft gateway: your SMB shares in the Loft apps
 Runs in front of an SMB server (a NAS or a file server) and serves its shares to
 the Loft apps for macOS, iOS, Android, Windows and Linux. Signs in to the SMB
 server as each user, so the server's own permissions apply.
 .
 After installing, run "sudo loft-gateway init", then "sudo loft-gateway doctor".
EOF
install -m 755 "$SRC/gateway/packaging/deb/postinst" "$ROOT/DEBIAN/postinst"
install -m 755 "$SRC/gateway/packaging/deb/prerm" "$ROOT/DEBIAN/prerm"
install -m 755 "$SRC/gateway/packaging/deb/postrm" "$ROOT/DEBIAN/postrm"

mkdir -p "$OUT"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$OUT/$PKG.deb" >/dev/null
( cd "$OUT" && sha256sum "$PKG.deb" > "$PKG.deb.sha256" )
echo "built $OUT/$PKG.deb ($(du -h "$OUT/$PKG.deb" | cut -f1), python ${PYV}, ${REL})"
