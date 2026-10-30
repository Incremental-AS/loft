#!/usr/bin/env bash
# Conformance against Samba, in Docker. Needs Docker with compose v2; nothing is published
# on the host and the containers' network has no way out.
#
#   conformance/samba/run.sh                      # the CI matrix (below)
#   conformance/samba/run.sh required desired tunnel   # one combination:
#        <gateway smb_encryption> <Samba server smb encrypt> <public mode>
#
# Every combination: build, start Samba + gateway, `loft-gateway doctor --user alice`
# inside the gateway container, the pairing code from `loft-gateway pair`, the suite,
# then tear down (volumes too). Plus one negative case: the gateway requires encryption
# and Samba has it off, so doctor must fail and sign-in must be refused.
set -uo pipefail
cd "$(dirname "$0")"
LOG=${LOG_DIR:-.}
mkdir -p "$LOG"
DC=(docker compose -f docker-compose.yml)

down() { "${DC[@]}" --profile run down -v --remove-orphans >/dev/null 2>&1 || true; }

one() {   # one <gw enc> <samba enc> <mode> -> 0 ok
  local gw=$1 samba=$2 mode=$3 tag="$1-$2-$3" rc=0
  echo "=== gateway smb_encryption=$gw, Samba server smb encrypt=$samba, public mode=$mode"
  export GW_SMB_ENCRYPTION=$gw SAMBA_ENCRYPT=$samba PUBLIC_MODE=$mode
  if [ "$mode" = direct ]; then
    export GW_PUBLIC_URL=https://gateway:8443 RUNNER_PUBLIC_URL=https://gateway:8443
  else
    export GW_PUBLIC_URL=https://loft.example.invalid RUNNER_PUBLIC_URL=http://gateway:8080
  fi
  down
  "${DC[@]}" up -d --wait samba gateway || { "${DC[@]}" logs gateway samba | tail -50; down; return 1; }
  "${DC[@]}" exec -T -e LOFT_DOCTOR_PASSWORD=alice-conformance-pw gateway \
      loft-container loft-gateway doctor --user alice | tee "$LOG/doctor-$tag.txt"
  local drc=${PIPESTATUS[0]}
  if [ "$drc" = 1 ]; then echo "doctor FAILED ($tag)"; rc=1; fi
  "${DC[@]}" --profile run run --rm pair || rc=1
  "${DC[@]}" --profile run run --rm runner -q -ra 2>&1 | tee "$LOG/pytest-$tag.txt" | tail -20
  [ "${PIPESTATUS[0]}" = 0 ] || rc=1
  [ $rc = 0 ] || "${DC[@]}" logs gateway | tail -80 >"$LOG/gateway-$tag.log"
  down
  return $rc
}

negative() {   # required on the gateway, encryption off on Samba: refuse, loudly
  echo "=== negative: gateway requires encryption, Samba has it off"
  export GW_SMB_ENCRYPTION=required SAMBA_ENCRYPT=off PUBLIC_MODE=tunnel
  export GW_PUBLIC_URL=https://loft.example.invalid RUNNER_PUBLIC_URL=http://gateway:8080
  down
  "${DC[@]}" up -d --wait samba gateway || { down; return 1; }
  "${DC[@]}" exec -T -e LOFT_DOCTOR_PASSWORD=alice-conformance-pw gateway \
      loft-container loft-gateway doctor --user alice | tee "$LOG/doctor-negative.txt"
  local drc=${PIPESTATUS[0]} rc=0
  [ "$drc" = 1 ] || { echo "doctor should have failed"; rc=1; }
  grep -q 'FAIL.*smb encryption' "$LOG/doctor-negative.txt" || { echo "no encryption FAIL"; rc=1; }
  # Sign-in over the pinned direct listener must be refused (the NAS can't encrypt).
  "${DC[@]}" --profile run run --rm pair >/dev/null || rc=1
  "${DC[@]}" --profile run run --rm --entrypoint sh runner -c '
    export LOFT_PAIRING=$(cat /pair/link); python - <<EOF
import os, sys
sys.path.insert(0, "/suite/conformance")
from loftclient import Loft, Spec, decode_pairing
p = decode_pairing(os.environ["LOFT_PAIRING"])
c = Loft(p["lan"], Spec(), p["pins"])
r = c.call("login", json_body={"user": "alice", "password": "alice-conformance-pw"}, token=False)
print("login without SMB encryption ->", r.status_code, r.text)
sys.exit(0 if r.status_code in (401, 503) else 1)
EOF' || { echo "sign-in was not refused"; rc=1; }
  down
  return $rc
}

if [ $# = 3 ]; then
  one "$@"; exit $?
fi
"${DC[@]}" --profile run build || exit 1
fail=0
one required desired tunnel || fail=1
one required required direct || fail=1
one server   off     tunnel || fail=1
one server   desired tunnel || fail=1
negative || fail=1
[ $fail = 0 ] && echo "ALL GREEN" || echo "SOMETHING FAILED"
exit $fail
