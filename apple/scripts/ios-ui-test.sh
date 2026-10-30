#!/usr/bin/env bash
# iPhone UI tests in the simulator against gateway/scripts/dev_server.py on this Mac's
# loopback. Runs on the Mac build host, in a copy of the repo.
#
#   apple/scripts/ios-ui-test.sh [venv-python] [extra xcodebuild args, e.g. -only-testing:…]
#                                 (default python: .venv/bin/python, a venv made from
#                                  gateway/requirements.lock)
#
# Seeds the dev NAS (share "home" with a folder and a file), makes a pairing link for the
# direct listener with the gateway's own encoder, and hands the tests the link, the
# address + pin (manual setup) and the dev account through TEST_RUNNER_* variables.
# LOFT_SIM picks the simulator (default "iPhone 17"); LOFT_SHOTS=<dir> keeps screenshots;
# LOFT_UI_PORTS="<direct> <public>" moves the dev server off its default ports.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=${1:-.venv/bin/python}
shift || true
SIM=${LOFT_SIM:-iPhone 17}
read -r DP PP <<< "${LOFT_UI_PORTS:-18543 18180}"   # not core-it.sh's ports, so both can run at once
W=$(mktemp -d "${TMPDIR:-/var/tmp}/loft-ui.XXXXXX")
trap 'kill $SRV 2>/dev/null || true; rm -rf "$W"' EXIT
mkdir -p "$W/nas/home/Documents"
echo "Hello from the dev gateway." > "$W/nas/home/Documents/Welcome.txt"
"$PY" gateway/scripts/dev_server.py "$W/nas" "$W/work" $DP $PP > "$W/server.log" 2>&1 &
SRV=$!
for _ in $(seq 50); do curl -sf http://127.0.0.1:$PP/healthz >/dev/null && break; sleep 0.2; done
PIN=$(cat "$W/work/direct.pin")
LINK=$(PYTHONPATH=gateway "$PY" apple/scripts/pair-link.py "https://127.0.0.1:$DP" "$PIN" "Dev gateway")
( cd apple && xcodegen generate --quiet )
SHOTS=()
[ -n "${LOFT_SHOTS:-}" ] && mkdir -p "$LOFT_SHOTS" && SHOTS=(TEST_RUNNER_LOFT_SHOTS="$LOFT_SHOTS")
env TEST_RUNNER_LOFT_PAIR_LINK="$LINK" TEST_RUNNER_LOFT_GATEWAY_NAME="Dev gateway" \
    TEST_RUNNER_LOFT_ADDRESS="127.0.0.1:$DP" TEST_RUNNER_LOFT_PIN="$PIN" \
    TEST_RUNNER_LOFT_USER=loft-dev TEST_RUNNER_LOFT_PASSWORD=dev-password \
    TEST_RUNNER_LOFT_SHARE=home TEST_RUNNER_LOFT_OPEN=Documents/Welcome.txt ${SHOTS[@]+"${SHOTS[@]}"} \
  xcodebuild test -project apple/Loft.xcodeproj -scheme LoftPhone \
    -destination "platform=iOS Simulator,name=$SIM" -derivedDataPath build/DerivedData-iossim "$@"
