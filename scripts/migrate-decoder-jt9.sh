#!/bin/bash
# migrate-decoder-jt9.sh CONFIG_DIR — once per station, move every psk-recorder
# config in CONFIG_DIR from decode_ft8 (ka9q/ft8_lib) to jt9 (sigmond's UberSDR
# jt9 over shared memory, via jt9_decode).  rob, 2026-11-11: "switch everybody
# to jt9 off of decode_ft8".
#
# Every config written before 691f4f7 says decoder_kind = "decode_ft8" because
# the old template did, so the new default alone moves no station.  A station
# keeps one config per instance (/etc/psk-recorder/<instance>.toml, e.g.
# AI6VN-1.toml), plus perhaps the old single psk-recorder-config.toml, so every
# *.toml here is covered.  (The first version, a29cfa9, took only
# psk-recorder-config.toml: on AI6VN-1 it switched that unused file and left the
# live AI6VN-1.toml on decode_ft8 -- hence the new marker name, which makes the
# stations it ran on run this once more.)
#
# Runs from install.sh (so on `smd update`), rewrites only the decoder_kind
# line, keeps <file>.pre-jt9, and leaves a marker: it runs ONCE, and an
# operator who later sets decode_ft8 on purpose keeps it.  If jt9_decode is
# missing the recorder falls back to decode_ft8 by itself, so this can't stop
# decoding.
set -u
DIR=${1:?usage: migrate-decoder-jt9.sh CONFIG_DIR}
[ -f "$DIR" ] && DIR=$(dirname "$DIR")      # an old caller passed the file
MARK="$DIR/.decoder-jt9-migrated-v2"
[ -d "$DIR" ] || exit 0
[ -e "$MARK" ] && exit 0
re='^([[:space:]]*decoder_kind[[:space:]]*=[[:space:]]*)"decode_ft8"'
rc=0
for CFG in "$DIR"/*.toml; do
    [ -f "$CFG" ] || continue
    grep -qE "$re" "$CFG" || continue
    [ -e "$CFG.pre-jt9" ] || cp -p "$CFG" "$CFG.pre-jt9" || { rc=1; continue; }
    tmp=$(mktemp) || { rc=1; continue; }
    if sed -E "s/$re/\\1\"jt9\"/" "$CFG" > "$tmp" && cat "$tmp" > "$CFG"; then   # cat keeps owner and mode
        echo "[INFO]  psk-recorder: decoder_kind decode_ft8 -> jt9 in $CFG (backup: $CFG.pre-jt9)"
    else
        rc=1
    fi
    rm -f "$tmp"
done
[ $rc -eq 0 ] && date -u +%Y-%m-%dT%H:%M:%SZ > "$MARK"
exit $rc
