#!/bin/bash
# migrate-decoder-jt9.sh CONFIG_FILE — once per station, move an existing
# psk-recorder config from decode_ft8 (ka9q/ft8_lib) to jt9 (sigmond's UberSDR
# jt9 over shared memory, via jt9_decode).  rob, 2026-11-11: "switch everybody
# to jt9 off of decode_ft8".
#
# Every config written before 691f4f7 says decoder_kind = "decode_ft8" because
# the old template did, so the new default alone moves no station.  This runs
# from install.sh (so on `smd update`), rewrites only that one line, keeps a
# backup, and leaves a marker beside the config: it runs ONCE, and an operator
# who later sets decode_ft8 on purpose keeps it.  If jt9_decode is missing the
# recorder falls back to decode_ft8 by itself, so this can't stop decoding.
set -u
CFG=${1:?usage: migrate-decoder-jt9.sh CONFIG_FILE}
MARK="$(dirname "$CFG")/.decoder-jt9-migrated"
[ -f "$CFG" ] || exit 0
[ -e "$MARK" ] && exit 0
re='^([[:space:]]*decoder_kind[[:space:]]*=[[:space:]]*)"decode_ft8"'
if grep -qE "$re" "$CFG"; then
    cp -p "$CFG" "$CFG.pre-jt9" || exit 1
    tmp=$(mktemp) || exit 1
    sed -E "s/$re/\\1\"jt9\"/" "$CFG" > "$tmp" && cat "$tmp" > "$CFG"   # cat keeps owner and mode
    rc=$?; rm -f "$tmp"; [ $rc -eq 0 ] || exit 1
    echo "[INFO]  psk-recorder: decoder_kind decode_ft8 -> jt9 in $CFG (backup: $CFG.pre-jt9)"
fi
date -u +%Y-%m-%dT%H:%M:%SZ > "$MARK"
