#!/bin/sh
# Install Loft from the tarball for the current user (no root needed):
#   ./install.sh            -> ~/.local/bin/loft, menu entry, icons, systemd --user unit
#   PREFIX=/usr/local sudo ./install.sh   for everyone (unit goes to /etc/systemd/user)
# Needs FUSE 3 (the fuse3 package: fusermount3).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
prefix=${PREFIX:-$HOME/.local}
if [ "$prefix" = "$HOME/.local" ]; then
  unitdir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
else
  unitdir=/etc/systemd/user
fi
install -Dm755 "$here/bin/loft" "$prefix/bin/loft"
for f in "$here"/share/applications/*.desktop; do
  sed "s|^Exec=loft |Exec=$prefix/bin/loft |" "$f" > "$f.tmp"
  install -Dm644 "$f.tmp" "$prefix/share/applications/$(basename "$f")"
  rm -f "$f.tmp"
done
install -Dm644 "$here/share/metainfo/no.incremental.loft.metainfo.xml" "$prefix/share/metainfo/no.incremental.loft.metainfo.xml"
(cd "$here/share/icons" && find hicolor -type f) | while read -r f; do
  install -Dm644 "$here/share/icons/$f" "$prefix/share/icons/$f"
done
sed "s|^ExecStart=/usr/bin/loft |ExecStart=$prefix/bin/loft |" "$here/share/systemd/user/loft.service" > "$here/loft.service.tmp"
install -Dm644 "$here/loft.service.tmp" "$unitdir/loft.service"
rm -f "$here/loft.service.tmp"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$prefix/share/applications" || true
command -v fusermount3 >/dev/null 2>&1 || echo "Note: fusermount3 not found. Install FUSE 3 (package fuse3) before mounting."
echo "Installed $prefix/bin/loft. Next: loft pair <link>, loft login, then systemctl --user enable --now loft"
