#!/bin/bash
# After `connectedDebugAndroidTest`: checks that what the instrumented tests uploaded is on the dev
# gateway's disk, byte for byte, and that what they deleted or renamed is gone.
#
#   adb logcat -d -s LoftIT:I | android/scripts/check-disk.sh <nas-dir>/home
#
# The tests log `disk <sha256> <path>` and `gone <path>`, paths relative to the share.
set -u
share=${1:?usage: check-disk.sh <share dir on the dev server>}
ok=0 bad=0
sum() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
while IFS= read -r line; do
    case $line in
    *" LoftIT"*": disk "*)
        rest=${line#*: disk }
        want=${rest%% *}
        path=${rest#* }
        if [ -f "$share/$path" ] && [ "$(sum "$share/$path")" = "$want" ]; then
            ok=$((ok + 1)); echo "ok    $path"
        else
            bad=$((bad + 1)); echo "WRONG $path"
        fi ;;
    *" LoftIT"*": gone "*)
        path=${line#*: gone }
        if [ -e "$share/$path" ]; then
            bad=$((bad + 1)); echo "STILL $path"
        else
            ok=$((ok + 1)); echo "gone  $path"
        fi ;;
    esac
done
echo "$ok ok, $bad wrong"
[ "$bad" -eq 0 ] && [ "$ok" -gt 0 ]
