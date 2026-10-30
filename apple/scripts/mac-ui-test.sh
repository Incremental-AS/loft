#!/usr/bin/env bash
# Mac UI tests (LoftMacUITests) against gateway/scripts/dev_server.py on this Mac's loopback.
# Runs on the Mac build host, in a copy of the repo, with a user logged in on the screen
# (XCUITest drives the real window server; over ssh that is the console user's session).
#
#   apple/scripts/mac-ui-test.sh [venv-python] [extra xcodebuild args, e.g. -only-testing:…]
#                                 (default python: .venv/bin/python, a venv made from
#                                  gateway/requirements.lock)
#
# The app under test is a Debug build under its own bundle id and URL scheme
# (no.incremental.loft.uitest, loft-uitest), run with -LoftUITest: an installed Loft on the
# same Mac keeps its extension, its loft:// links, its Keychain item and its Finder location.
# Nothing is installed or signed with an identity (the runner is signed ad hoc), and the
# test build is unregistered from Launch Services and deleted afterwards.
# LOFT_KEEP_BUILD=1 keeps it; the .xcresult (screenshots) stays in build/mac-ui-results/.
# LOFT_UI_PORTS="<direct> <public>" moves the dev server off its default ports.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=${1:-.venv/bin/python}
shift || true
read -r DP PP <<< "${LOFT_UI_PORTS:-18643 18280}"   # not core-it.sh's or ios-ui-test.sh's ports
DD=build/DerivedData-macui
RES=build/mac-ui-results/$(date +%Y%m%d-%H%M%S).xcresult
W=$(mktemp -d "${TMPDIR:-/var/tmp}/loft-macui.XXXXXX")
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
cleanup() {
  kill "$SRV" 2>/dev/null || true
  rm -rf "$W"
  for a in "$DD"/Build/Products/Debug/Loft.app "$DD"/Build/Products/Debug/LoftMacUITests-Runner.app; do
    [ -d "$a" ] && "$LSREG" -u "$a" 2>/dev/null || true
  done
  [ "${LOFT_KEEP_BUILD:-}" = 1 ] || rm -rf "$DD"
}
trap cleanup EXIT
mkdir -p "$W/nas/home/Documents" build/mac-ui-results
echo "Hello from the dev gateway." > "$W/nas/home/Documents/Welcome.txt"
"$PY" gateway/scripts/dev_server.py "$W/nas" "$W/work" $DP $PP > "$W/server.log" 2>&1 &
SRV=$!
for _ in $(seq 50); do curl -sf http://127.0.0.1:$PP/healthz >/dev/null && break; sleep 0.2; done
PIN=$(cat "$W/work/direct.pin")
LINK=$(PYTHONPATH=gateway "$PY" apple/scripts/pair-link.py "https://127.0.0.1:$DP" "$PIN" "Dev gateway")
( cd apple && xcodegen generate --quiet )
env TEST_RUNNER_LOFT_PAIR_LINK="$LINK" TEST_RUNNER_LOFT_GATEWAY_NAME="Dev gateway" \
    TEST_RUNNER_LOFT_ADDRESS="127.0.0.1:$DP" TEST_RUNNER_LOFT_PIN="$PIN" \
    TEST_RUNNER_LOFT_USER=loft-dev TEST_RUNNER_LOFT_PASSWORD=dev-password \
  xcodebuild test -project apple/Loft.xcodeproj -scheme Loft -configuration Debug \
    -destination "platform=macOS,arch=$(uname -m)" -derivedDataPath "$DD" -resultBundlePath "$RES" \
    ONLY_ACTIVE_ARCH=YES LOFT_MAC_BUNDLE_ID=no.incremental.loft.uitest LOFT_URL_SCHEME=loft-uitest "$@"
