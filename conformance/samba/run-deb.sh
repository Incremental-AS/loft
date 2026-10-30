#!/usr/bin/env bash
# Install a loft-gateway .deb in a clean Debian/Ubuntu container and run doctor + the
# conformance suite against Samba with it.
#
#   conformance/samba/run-deb.sh dist/loft-gateway_0.2.0-1~debian13_amd64.deb [debian:trixie]
set -uo pipefail
DEB=$(readlink -f "${1:?path to the .deb}")
export DEB_BASE=${2:-debian:trixie}
cd "$(dirname "$0")"
LOG=${LOG_DIR:-.}
mkdir -p "$LOG"
rm -rf .deb && mkdir .deb && cp "$DEB" .deb/
DC=(docker compose -f docker-compose.yml -f docker-compose.deb.yml)
export GW_SMB_ENCRYPTION=${GW_SMB_ENCRYPTION:-required} SAMBA_ENCRYPT=${SAMBA_ENCRYPT:-desired}
export PUBLIC_MODE=tunnel RUNNER_PUBLIC_URL=http://gateway:8080
down() { "${DC[@]}" --profile run down -v --remove-orphans >/dev/null 2>&1 || true; }
rc=0
down
"${DC[@]}" build gateway || exit 1
"${DC[@]}" --profile run build runner || exit 1
docker image inspect "${GATEWAY_IMAGE:-loft-gateway:ci}" >/dev/null 2>&1 \
  || docker compose -f docker-compose.yml build gateway || exit 1     # for the pair step
echo "=== .deb $(basename "$DEB") on $DEB_BASE"
"${DC[@]}" up -d --wait samba gateway || { "${DC[@]}" logs gateway | tail -40; down; exit 1; }
"${DC[@]}" exec -T gateway dpkg -s loft-gateway | grep -E '^(Package|Version|Status|Depends):'
"${DC[@]}" exec -T gateway sh -c 'ls -l /usr/bin/loft-gateway /lib/systemd/system/loft-* && id loft && stat -c "%a %U:%G %n" /etc/loft /var/lib/loft /etc/loft/direct.key /etc/loft/master.key'
"${DC[@]}" exec -T -e LOFT_DOCTOR_PASSWORD=alice-conformance-pw gateway \
    loft-gateway doctor --user alice | tee "$LOG/doctor-deb.txt"
[ "${PIPESTATUS[0]}" = 1 ] && { echo "doctor FAILED"; rc=1; }
"${DC[@]}" --profile run run --rm pair || rc=1
"${DC[@]}" --profile run run --rm runner -q -ra 2>&1 | tee "$LOG/pytest-deb.txt" | tail -15
[ "${PIPESTATUS[0]}" = 0 ] || rc=1
[ $rc = 0 ] || "${DC[@]}" logs gateway | tail -60 > "$LOG/gateway-deb.log"
down
rm -rf .deb
[ $rc = 0 ] && echo "DEB GREEN" || echo "DEB FAILED"
exit $rc
