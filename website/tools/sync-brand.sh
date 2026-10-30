#!/usr/bin/env bash
# Copy the generated Loft web kit into static/. The kit is the source of truth:
# never edit the copies here, change the kit and run this again.
#   BRAND=/path/to/brand/loft/web tools/sync-brand.sh
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
src="${BRAND:?set BRAND to the brand kit's web/ directory}"
dst="$here/static"
[ -f "$src/tokens.css" ] || { echo "no brand kit at $src" >&2; exit 1; }

mkdir -p "$dst"/{css,fonts/licences,img/hero,img/logo,img/diagram,img/og,img/illustration}
# tokens.css sits in /css/, the fonts in /fonts/
sed 's#url("fonts/#url("/fonts/#g' "$src/tokens.css" > "$dst/css/tokens.css"
cp "$src"/fonts/*.woff2 "$dst/fonts/"
cp "$src"/fonts/licences/*.txt "$dst/fonts/licences/"
cp "$src"/hero/hero-*.{webp,jpg} "$dst/img/hero/"
cp "$src"/logo/*.svg "$dst/img/logo/"
cp "$src"/diagram/how-it-works-*-notext.svg "$dst/img/diagram/"
cp "$src"/diagram/how-it-works-*-{en,no}.svg "$dst/img/diagram/"
cp "$src"/og/og-{en,no}.jpg "$dst/img/og/"
cp "$src"/illustration/loft-traditional-*.{webp,jpg} "$dst/img/illustration/"
# favicons live at the site root, where site.webmanifest expects them
cp "$src"/favicon/{favicon.svg,favicon.ico,apple-touch-icon.png,icon-192.png,icon-512.png,icon-maskable-512.png,site.webmanifest} "$dst/"
echo "synced from $src"
