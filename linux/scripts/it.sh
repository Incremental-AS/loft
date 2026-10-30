#!/usr/bin/env bash
# Integration test: pair -> login -> mount -> ls/read/write/rename/delete/conflict/offline,
# checked on the dev server's disk.
#
#   linux/scripts/it.sh [venv-dir]      (default: linux/.it-venv, made from gateway/requirements.lock)
#
# Needs Python 3.11+, FUSE (/dev/fuse, fusermount3) and Go.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
repo=$(cd "$here/.." && pwd)
venv=${1:-$here/.it-venv}
if [ ! -x "$venv/bin/python" ]; then
  python3 -m venv "$venv"
  "$venv/bin/pip" install -q --require-hashes -r "$repo/gateway/requirements.lock"
fi
cd "$here"
LOFT_IT_PYTHON="$venv/bin/python" CGO_ENABLED=0 go test -count=1 -tags integration -v -timeout 15m ./itest "${@:2}"
