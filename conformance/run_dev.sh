#!/usr/bin/env bash
# Run the conformance suite against gateway/scripts/dev_server.py (local folders, no SMB).
#
#   conformance/run_dev.sh [pytest args...]
#
# PUBLIC_MODE=direct tests the "direct" public mode (public URL = the direct listener).
# Uses $PYTHON (default python3); it needs the gateway's deps and conformance/requirements.txt.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
PY=${PYTHON:-python3}
WORK=$(mktemp -d "${TMPDIR:-/var/tmp}/loft-conf.XXXXXX")
PID=
cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null && wait "$PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT
free_port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
DP=$(free_port); PP=$(free_port)
"$PY" "$REPO/gateway/scripts/dev_server.py" "$WORK/nas" "$WORK/gw" "$DP" "$PP" >"$WORK/server.log" 2>&1 &
PID=$!
for _ in $(seq 1 150); do
  curl -sf "http://127.0.0.1:$PP/healthz" >/dev/null 2>&1 && break
  kill -0 "$PID" 2>/dev/null || { cat "$WORK/server.log"; exit 1; }
  sleep 0.2
done
export LOFT_DIRECT_URL="https://127.0.0.1:$DP" LOFT_PINS="$(cat "$WORK/gw/direct.pin")"
if [ "${PUBLIC_MODE:-tunnel}" = direct ]; then
  # "direct" public mode: the outside address is a port forward to the direct listener.
  export LOFT_PUBLIC_URL="$LOFT_DIRECT_URL" LOFT_PUBLIC_MODE=direct
else
  export LOFT_PUBLIC_URL="http://127.0.0.1:$PP" LOFT_PUBLIC_MODE=tunnel
fi
export LOFT_USER=loft-dev LOFT_PASSWORD=dev-password LOFT_SHARE=home
cd "$HERE"
"$PY" -m pytest "$@"
