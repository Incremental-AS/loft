#!/usr/bin/env bash
# Packages for amd64 and arm64: .deb + .rpm (nfpm), AppImage (appimagetool), tarball.
#   linux/scripts/package.sh           -> linux/dist/*.{deb,rpm,AppImage,tar.gz} + SHA256SUMS
#
# Tools: nfpm on PATH (or $HOME/tools/bin/nfpm; `go install github.com/goreleaser/nfpm/v2/cmd/nfpm@v2.48.0`).
# appimagetool and the AppImage runtimes are fetched once into $LOFT_TOOLS (default
# ~/tools/loft-packaging) from the AppImage project's GitHub releases; their SHA-256 is
# written to SOURCES.txt there. Building is not releasing: nothing here uploads anything.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${VERSION:-0.2.0}
ARCHES=${ARCHES:-amd64 arm64}
TOOLS=${LOFT_TOOLS:-$HOME/tools/loft-packaging}
NFPM=${NFPM:-$(command -v nfpm || echo "$HOME/tools/bin/nfpm")}
dist=$PWD/dist
mkdir -p "$TOOLS" "$dist"

fetch() { # name url
  if [ ! -s "$TOOLS/$1" ]; then
    curl -fsSL --retry 3 -o "$TOOLS/$1.part" "$2"
    mv "$TOOLS/$1.part" "$TOOLS/$1"
    chmod +x "$TOOLS/$1"
    echo "$(sha256sum "$TOOLS/$1" | cut -d' ' -f1)  $1  $2  $(date -u +%F)" >> "$TOOLS/SOURCES.txt"
  fi
}
AI=https://github.com/AppImage
fetch appimagetool-x86_64.AppImage "$AI/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
fetch runtime-x86_64 "$AI/type2-runtime/releases/download/continuous/runtime-x86_64"
fetch runtime-aarch64 "$AI/type2-runtime/releases/download/continuous/runtime-aarch64"

ARCHES="$ARCHES" VERSION="$VERSION" scripts/build.sh
rm -rf "$dist/stage" "$dist/nfpm"

for arch in $ARCHES; do
  case $arch in amd64) ai=x86_64 ;; arm64) ai=aarch64 ;; *) echo "unknown arch $arch"; exit 1 ;; esac
  bin=$dist/loft-linux-$arch/loft

  # .deb and .rpm
  for fmt in deb rpm; do
    install -Dm755 "$bin" "$dist/nfpm/loft"
    ARCH=$arch VERSION=$VERSION "$NFPM" package -f packaging/nfpm.yaml -p $fmt -t "$dist/" >/dev/null
  done

  # tarball: bin/ + share/ + install.sh (installs to ~/.local without root)
  t=$dist/stage/loft-$VERSION-linux-$arch
  install -Dm755 "$bin" "$t/bin/loft"
  install -Dm644 packaging/no.incremental.loft.desktop "$t/share/applications/no.incremental.loft.desktop"
  install -Dm644 packaging/no.incremental.loft-pair.desktop "$t/share/applications/no.incremental.loft-pair.desktop"
  install -Dm644 packaging/no.incremental.loft.metainfo.xml "$t/share/metainfo/no.incremental.loft.metainfo.xml"
  install -Dm644 packaging/loft.service "$t/share/systemd/user/loft.service"
  mkdir -p "$t/share/icons" && cp -r packaging/icons/hicolor "$t/share/icons/"
  install -Dm755 packaging/install.sh "$t/install.sh"
  install -Dm644 README.md "$t/README.md"
  tar -C "$dist/stage" --owner=0 --group=0 -czf "$dist/loft-$VERSION-linux-$arch.tar.gz" "loft-$VERSION-linux-$arch"

  # AppImage
  a=$dist/stage/Loft-$arch.AppDir
  install -Dm755 packaging/AppRun "$a/AppRun"
  install -Dm755 "$bin" "$a/usr/bin/loft"
  install -Dm644 packaging/no.incremental.loft.desktop "$a/no.incremental.loft.desktop"
  install -Dm644 packaging/no.incremental.loft.desktop "$a/usr/share/applications/no.incremental.loft.desktop"
  install -Dm644 packaging/no.incremental.loft.metainfo.xml "$a/usr/share/metainfo/no.incremental.loft.appdata.xml"
  mkdir -p "$a/usr/share/icons" && cp -r packaging/icons/hicolor "$a/usr/share/icons/"
  cp packaging/icons/hicolor/256x256/apps/no.incremental.loft.png "$a/no.incremental.loft.png"
  ln -sf no.incremental.loft.png "$a/.DirIcon"
  ARCH=$ai APPIMAGE_EXTRACT_AND_RUN=1 "$TOOLS/appimagetool-x86_64.AppImage" --no-appstream \
    --runtime-file "$TOOLS/runtime-$ai" "$a" "$dist/Loft-$VERSION-$ai.AppImage" >"$dist/stage/appimagetool-$ai.log" 2>&1 \
    || { cat "$dist/stage/appimagetool-$ai.log"; exit 1; }
done

rm -rf "$dist/stage" "$dist/nfpm"
(cd "$dist" && sha256sum ./*.deb ./*.rpm ./*.AppImage ./*.tar.gz | sed 's| \./| |' > SHA256SUMS)
echo "packages in $dist:"
cat "$dist/SHA256SUMS"
