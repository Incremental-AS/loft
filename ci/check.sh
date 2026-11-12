#!/usr/bin/env bash
# The repository check: exit 0 green, 1 a failing step, 2 setup. One line per step,
# "<step> ok|FAIL", each step's output in $JB_CHECK_LOGS/<step>.log (a temporary
# directory when unset). The gateway's Python dependencies come from the hashed
# gateway/requirements.lock into a virtual environment cached outside the checkout.
#
#   ci/check.sh                      gateway tests, OpenAPI file, conformance (both modes)
#   SUITE_CONTRACTS=<dir> ci/check.sh   ... and the suite contract tests against that checkout
#                                    (also used when ../suite next to this checkout has them)
#
# Not here: the NAS agent's btrfs test (needs root and a loop mount; GitHub CI runs it) and
# the Apple, Android and Windows builds (their own workflows).
set -uo pipefail
cd "$(dirname "$0")/.."
LOGS="${JB_CHECK_LOGS:-$(mktemp -d)}"
mkdir -p "$LOGS"

HASH=$(cat gateway/requirements.lock conformance/requirements.txt | sha256sum | cut -c1-12)
VENV="${NAS_SYNC_CHECK_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/nas-sync-check/venv-$HASH}"
if [ ! -x "$VENV/bin/python" ] || [ ! -f "$VENV/.ready" ]; then
  {
    rm -rf "$VENV" && python3 -m venv "$VENV" &&
    "$VENV/bin/pip" install -q --require-hashes -r gateway/requirements.lock &&
    "$VENV/bin/pip" install -q -r conformance/requirements.txt openapi-spec-validator &&
    touch "$VENV/.ready"
  } > "$LOGS/setup.log" 2>&1 || { echo "setup FAIL (see setup.log)"; exit 2; }
fi
# This checkout's gateway, not one installed earlier.
"$VENV/bin/pip" install -q --no-deps -e gateway > "$LOGS/setup-gateway.log" 2>&1 \
  || { echo "setup FAIL (see setup-gateway.log)"; exit 2; }
PY="$VENV/bin/python"

rc=0
run_step() {   # name, command...
  local name="$1"; shift
  if "$@" > "$LOGS/$name.log" 2>&1; then echo "$name ok"; else echo "$name FAIL"; rc=1; fi
}

run_step gateway-tests sh -c "cd gateway && '$PY' -m pytest -q -p no:cacheprovider"
run_step openapi "$PY" api/gen_openapi.py --check
run_step conformance-tunnel env PYTHON="$PY" conformance/run_dev.sh -q -p no:cacheprovider
run_step conformance-direct env PYTHON="$PY" PUBLIC_MODE=direct conformance/run_dev.sh -q -p no:cacheprovider

# Site policy tests: only in the private branch (the public export leaves ops/ out).
if [ -d ops/tests ]; then
  run_step ops-policy sh -c "cd gateway && '$PY' -m pytest -q -p no:cacheprovider ../ops/tests"
fi

SUITE="${SUITE_CONTRACTS:-}"
if [ -z "$SUITE" ] && [ -f ../suite/tools/check_contract.py ]; then
  SUITE=$(cd ../suite && pwd)
fi
if [ -n "$SUITE" ]; then
  run_step suite-contracts sh -c "cd gateway && SUITE_CONTRACTS='$SUITE' '$PY' -m pytest -q -p no:cacheprovider tests/test_suite_contract.py"
else
  echo "suite-contracts skipped (no SUITE_CONTRACTS)"
fi
exit $rc
