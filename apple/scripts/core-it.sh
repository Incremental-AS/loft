#!/usr/bin/env bash
# LoftCore tests (Linux or macOS), including the client against the real gateway code.
#
#   apple/scripts/core-it.sh [venv-python] [swift test args…]
#                                     (default python: /var/tmp/$USER/loft-venv/bin/python)
#   LOFT_IT_PORTS="18443 18080"       the dev server's direct and public ports (another job
#                                     on the same machine may hold the defaults)
#
# Starts gateway/scripts/dev_server.py (LocalBackend, both listeners) on loopback,
# signs in over the DIRECT listener with curl and the dev pin (password login is
# direct-only, and swift-corelibs has no SecTrust to pin with), picks the root, then
# runs `swift test` with LOFT_IT_* so GatewayIntegrationTests talk to the public listener.
# It also makes a pairing link for the direct listener with the GATEWAY's own encoder
# (loft_gateway.admin), so PairedGatewayTests (macOS: SecTrust pinning) pair, probe,
# refresh pins and sign in exactly as the apps do.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=${1:-/var/tmp/$USER/loft-venv/bin/python}
read -r DP PP <<< "${LOFT_IT_PORTS:-18443 18080}"
W=$(mktemp -d "${TMPDIR:-/var/tmp}/loft-it.XXXXXX")
trap 'kill $SRV 2>/dev/null || true; rm -rf "$W"' EXIT
"$PY" gateway/scripts/dev_server.py "$W/nas" "$W/work" "$DP" "$PP" > "$W/server.log" 2>&1 &
SRV=$!
for _ in $(seq 50); do curl -sf http://127.0.0.1:$PP/healthz >/dev/null && break; sleep 0.2; done
PIN=$(cat "$W/work/direct.pin")
TOK=$(curl -sf -k --pinnedpubkey "sha256//$PIN" "https://127.0.0.1:$DP/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"user":"loft-dev","password":"dev-password","device_name":"core-it"}' \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -sf -X PUT "http://127.0.0.1:$PP/v1/me/root" -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{"share":"home","path":""}' >/dev/null
PAIR=$(PYTHONPATH=gateway "$PY" apple/scripts/pair-link.py "https://127.0.0.1:$DP" "$PIN" "Dev gateway")
cd apple/Packages/LoftCore
LOFT_IT_PUBLIC="http://127.0.0.1:$PP" LOFT_IT_TOKEN=$TOK LOFT_IT_NAS="$W/nas/home" \
LOFT_IT_PAIR=$PAIR LOFT_IT_USER=loft-dev LOFT_IT_PASSWORD=dev-password swift test "${@:2}"
