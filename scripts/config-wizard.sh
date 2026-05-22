#!/bin/bash
#
# psk-recorder config wizard (whiptail).
#
# Called by `psk-recorder config init` and `psk-recorder config edit`
# when stdout is a TTY and whiptail is installed.  Drives the operator
# through Station / Paths / Processing dialogs, validates inline, and
# writes the result through `psk-recorder config apply --json -` so
# the schema / type / range checks happen in Python.
#
# [[radiod]] arrays-of-tables and per-band freqs_hz lists are NOT
# editable through this wizard -- whiptail can't naturally express
# either.  The main menu has an "Edit raw TOML" item that opens the
# config in $EDITOR for the rare cases that need it.
#
# Usage:
#   config-wizard.sh init [--config <path>]
#   config-wizard.sh edit [--config <path>]
#
# Env (set by configurator.py before exec):
#   PSK_RECORDER_CLI         path to the psk-recorder binary to use
#   PSK_RECORDER_HELP_TOML   path to config/help.toml
#
# Reads (read-only) for pre-fills:
#   /etc/sigmond/coordination.env   STATION_CALL / STATION_GRID / etc. (§14.3)
#

set -euo pipefail

MODE="${1:-init}"; shift || true
CONFIG_PATH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_PATH="$2"; shift 2 ;;
        *) echo "config-wizard: unknown arg: $1" >&2; exit 2 ;;
    esac
done

PSK_RECORDER="${PSK_RECORDER_CLI:-psk-recorder}"
HELP_TOML="${PSK_RECORDER_HELP_TOML:-/opt/git/sigmond/psk-recorder/config/help.toml}"
COORD_ENV="/etc/sigmond/coordination.env"

HEIGHT=20
WIDTH=78
LIST_HEIGHT=10
BACKTITLE="psk-recorder configuration"

# -------- preflight ----------------------------------------------------

if ! command -v whiptail >/dev/null 2>&1; then
    cat <<EOF >&2
psk-recorder config: whiptail is not installed on this host.

The interactive wizard requires it.  Install with:

    sudo apt install whiptail

Or use the legacy stdin-prompt path with:

    psk-recorder config $MODE --non-interactive
EOF
    exit 1
fi

# -------- helpers ----------------------------------------------------

# Pre-fill from sigmond's coordination.env (read-only).
seed_from_coord_env() {
    local key="$1"
    [[ -r "$COORD_ENV" ]] || return 0
    sed -nE "s|^[[:space:]]*${key}=([\"']?)([^\"']*)\\1[[:space:]]*\$|\\2|p" \
        "$COORD_ENV" | tail -1
}

# Read one scalar out of the current effective config via JSON.
config_get() {
    local section="$1" key="$2"
    "$PSK_RECORDER" config show --json --defaults ${CONFIG_PATH:+--config "$CONFIG_PATH"} 2>/dev/null \
        | python3 -c "
import json, sys
d = json.load(sys.stdin)
v = d.get('$section', {}).get('$key', '')
if isinstance(v, bool):
    print('true' if v else 'false')
else:
    print(v)
"
}

# In-session lookup: SCRATCH_JSON (pending edits) first, then disk.
current_value() {
    local section="$1" key="$2"
    local scratch_val
    scratch_val=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
v = d.get('$section', {}).get('$key', None)
if v is None:
    pass
elif isinstance(v, bool):
    print('true' if v else 'false')
else:
    print(v)
")
    if [[ -n "$scratch_val" ]]; then
        echo "$scratch_val"
    else
        config_get "$section" "$key"
    fi
}

# Pull help.toml's title/help/example/validator_hint/required for a key.
help_get() {
    local dotted="$1" attr="$2"
    [[ -r "$HELP_TOML" ]] || return 0
    python3 -c "
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open('$HELP_TOML', 'rb') as f:
    d = tomllib.load(f)
node = d
for part in '$dotted'.split('.'):
    if not isinstance(node, dict):
        node = {}
        break
    node = node.get(part, {})
if isinstance(node, dict):
    v = node.get('$attr', '')
    if isinstance(v, bool):
        print('true' if v else 'false')
    else:
        print(v)
" 2>/dev/null
}

# -------- validators --------------------------------------------------

valid_callsign()       { [[ "$1" =~ ^[A-Za-z0-9/]{1,9}$ ]]; }
# Maidenhead 6-10 chars: field (A-R) + square (0-9) + alpha-num
# subsquare etc.  Accept case-insensitive; canonicalize before write.
valid_grid()           { [[ "$1" =~ ^[A-Ra-r]{2}[0-9]{2}[A-Za-z0-9]{2,8}$ ]]; }
valid_path_absolute()  { [[ "$1" == /* ]]; }
valid_path_executable(){ [[ "$1" == /* && -x "$1" ]]; }
valid_bool()           { [[ "$1" =~ ^(true|false)$ ]]; }
valid_nonneg_int()     { [[ "$1" =~ ^[0-9]+$ ]]; }
valid_always()         { return 0; }   # for free-form text / no-validate fields

# -------- ask() ------------------------------------------------------

# Returns the entered value on stdout.
#
# Cancel/Esc behaviour depends on help.toml's `required` flag:
#   required = true  -> Cancel returns 1; caller aborts the section.
#   required = false -> Cancel echoes the pre-fill and returns 0
#                       (operator-perceived skip-and-keep).
#
# Whiptail mis-parses defaults starting with '-' as flags.  Workaround:
# pass an empty default and surface the current value in the body when
# the pre-fill starts with '-'.  (psk-recorder's fields don't normally
# carry leading dashes, but reusing the mag-recorder fix keeps the
# wizard scaffolding consistent in case someone sets a negative
# radiod_lifetime_frames or similar future field.)
#
# Args: dotted_key  current_value  validator_fn  validator_args...
ask() {
    local dotted="$1" current="$2" validator="$3"; shift 3
    local extra_args=("$@")

    local title; title=$(help_get "$dotted" "title")
    local help;  help=$(help_get  "$dotted" "help")
    local example;  example=$(help_get "$dotted" "example")
    local hint;  hint=$(help_get  "$dotted" "validator_hint")
    local required; required=$(help_get "$dotted" "required")
    [[ -z "$required" ]] && required="true"

    [[ -z "$title" ]] && title="$dotted"
    local body="$help"
    [[ -n "$hint"    ]] && body+=$'\n\nFormat: '"$hint"
    [[ -n "$example" ]] && body+=$'\n''Example: '"$example"
    if [[ "$required" != "true" ]]; then
        body+=$'\n\n''(Optional -- Cancel to keep current value.)'
    fi

    local effective_default="$current"
    if [[ "$current" == -* ]]; then
        body+=$'\n\nCurrent value: '"$current"$'\n''(Leave the box empty to keep it.)'
        effective_default=""
    fi

    local entered
    while :; do
        if ! entered=$(whiptail \
                --title "$title" \
                --backtitle "$BACKTITLE" \
                --inputbox "$body" \
                "$HEIGHT" "$WIDTH" \
                "$effective_default" 3>&1 1>&2 2>&3); then
            if [[ "$required" == "true" ]]; then
                return 1
            fi
            echo "$current"
            return 0
        fi
        entered="${entered## }"; entered="${entered%% }"  # trim
        if [[ -z "$entered" && "$current" == -* ]]; then
            echo "$current"
            return 0
        fi
        if "$validator" "$entered" "${extra_args[@]}"; then
            echo "$entered"
            return 0
        fi
        whiptail --title "Invalid value" \
                 --backtitle "$BACKTITLE" \
                 --msgbox $'That value didn'\''t match the expected format.\n\n'"Hint: ${hint:-(see help text)}" \
                 12 "$WIDTH"
        current="$entered"
        effective_default="$entered"
        [[ "$current" == -* ]] && effective_default=""
    done
}

# Yes/no helper -- shared shape with --yesno-button labels.
ask_yesno() {
    local title="$1" body="$2"
    whiptail --title "$title" --backtitle "$BACKTITLE" --yesno "$body" 12 "$WIDTH"
}

# -------- screens ----------------------------------------------------

welcome_screen() {
    local body
    if [[ "$MODE" == "init" ]]; then
        body="Welcome to the psk-recorder configuration wizard.

You'll see a menu of sections (Station, Paths, Processing); pick
any section to fill in, then return to the menu.  Pick 'Apply'
when you're done to write everything in one go, or 'Cancel' to
discard pending changes and exit.

Pre-fills come from /etc/sigmond/coordination.env (if present) and
your current /etc/psk-recorder/psk-recorder-config.toml.  Inside a
section, pressing Cancel drops back to the menu (not all the way
out) -- effectively a 'back' button.

[[radiod]] blocks and per-band freqs_hz lists aren't editable
here -- pick 'Edit raw TOML' from the menu to open the file in
\$EDITOR (or hand-edit it later)."
    else
        body="Edit the current psk-recorder configuration.

You'll see a menu of sections (Station, Paths, Processing) with
current values shown inline.  Pick any section to edit, then
return to the menu.  Pick 'Apply' to write changes or 'Cancel'
to discard them.  Inside a section, pressing Cancel drops back
to the menu (not out).

For [[radiod]] blocks and per-band freqs_hz lists, pick 'Edit
raw TOML' from the menu to open the file in \$EDITOR."
    fi
    whiptail --title "psk-recorder configuration wizard" \
             --backtitle "$BACKTITLE" \
             --yesno "$body"$'\n\n'"Continue?" \
             "$HEIGHT" "$WIDTH"
}

collect_station() {
    local callsign grid antenna
    callsign=$(current_value station callsign)
    [[ -z "$callsign" ]] && callsign="$(seed_from_coord_env STATION_CALL)"
    [[ -z "$callsign" ]] && callsign="$(seed_from_coord_env STATION_CALLSIGN)"
    grid=$(current_value station grid_square)
    [[ -z "$grid" ]] && grid="$(seed_from_coord_env STATION_GRID)"
    antenna=$(current_value station antenna)

    callsign=$(ask station.callsign    "$callsign" valid_callsign) || return 1
    callsign="${callsign^^}"
    grid=$(ask     station.grid_square "$grid"     valid_grid)     || return 1
    # Canonical Maidenhead: field uppercase, square unchanged, subsquare lower.
    {
        local _f="${grid:0:2}" _s="${grid:2:2}" _rest="${grid:4}"
        grid="${_f^^}${_s}${_rest,,}"
    }
    # antenna is free-form; no validator.  Optional -- skip-on-Cancel via help.toml.
    antenna=$(ask station.antenna "$antenna" valid_always) || return 1

    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
d.setdefault('station', {}).update({
    'callsign':    '$callsign',
    'grid_square': '$grid',
    'antenna':     '''$antenna''',
})
print(json.dumps(d))
")
}

collect_paths() {
    local spool log decoder_kind decoder_path pskreporter pskreporter_tcp keep_wav
    spool=$(current_value         paths spool_dir)
    log=$(current_value           paths log_dir)
    decoder_kind=$(current_value  paths decoder_kind)
    [[ -z "$decoder_kind" ]] && decoder_kind="decode_ft8"
    decoder_path=$(current_value  paths decoder_decode_ft8)
    [[ -z "$decoder_path" ]] && decoder_path="/usr/local/bin/decode_ft8"
    pskreporter=$(current_value     paths pskreporter)
    pskreporter_tcp=$(current_value paths pskreporter_tcp)
    [[ -z "$pskreporter_tcp" ]] && pskreporter_tcp="true"
    keep_wav=$(current_value      paths keep_wav)

    spool=$(ask         paths.spool_dir         "$spool"        valid_path_absolute) || return 1
    log=$(ask           paths.log_dir           "$log"          valid_path_absolute) || return 1
    decoder_kind=$(ask  paths.decoder_kind      "$decoder_kind" valid_always)        || return 1
    decoder_path=$(ask  paths.decoder_decode_ft8 "$decoder_path" valid_path_absolute) || return 1
    pskreporter=$(ask   paths.pskreporter       "$pskreporter"  valid_path_absolute) || return 1

    # Bool fields presented as yesno screens.
    local pskreporter_tcp_new keep_wav_new
    if ask_yesno "$(help_get paths.pskreporter_tcp title)" \
                 "$(help_get paths.pskreporter_tcp help)"$'\n\n'"Current value: $pskreporter_tcp"$'\n\n'"Use TCP transport?"; then
        pskreporter_tcp_new=true
    else
        pskreporter_tcp_new=false
    fi
    pskreporter_tcp="$pskreporter_tcp_new"

    if ask_yesno "$(help_get paths.keep_wav title)" \
                 "$(help_get paths.keep_wav help)"$'\n\n'"Current value: $keep_wav"$'\n\n'"Keep WAV slices after decode?"; then
        keep_wav_new=true
    else
        keep_wav_new=false
    fi
    keep_wav="$keep_wav_new"

    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
d.setdefault('paths', {}).update({
    'spool_dir':         '$spool',
    'log_dir':           '$log',
    'decoder_kind':      '$decoder_kind',
    'decoder_decode_ft8': '$decoder_path',
    'pskreporter':       '$pskreporter',
    # bools arrived as bash strings 'true'/'false'; coerce.
    'pskreporter_tcp':   ('$pskreporter_tcp' == 'true'),
    'keep_wav':          ('$keep_wav' == 'true'),
})
print(json.dumps(d))
")
}

collect_processing() {
    local lifetime
    lifetime=$(current_value processing radiod_lifetime_frames)
    lifetime=$(ask processing.radiod_lifetime_frames "$lifetime" valid_nonneg_int) || return 1

    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
d.setdefault('processing', {}).update({
    'radiod_lifetime_frames': int('$lifetime'),
})
print(json.dumps(d))
")
}

# Return a comma-separated summary of [[radiod]] ids (or "(none)").
# Reads from SCRATCH_JSON first (in-session edits), then disk.
radiod_ids_summary() {
    python3 -c "
import json
try:
    scratch = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    scratch = {}
if 'radiod' in scratch:
    blocks = scratch['radiod']
else:
    import subprocess
    try:
        out = subprocess.check_output(
            ['$PSK_RECORDER', 'config', 'show', '--json', '--defaults'] +
            ($CONFIG_PATH and ['--config', '$CONFIG_PATH'] or []),
            stderr=subprocess.DEVNULL,
        ).decode()
        blocks = json.loads(out).get('radiod', [])
    except Exception:
        blocks = []
ids = [b.get('id', '?') for b in blocks if isinstance(b, dict)]
print(','.join(ids) if ids else '(none)')
" 2>/dev/null
}

# Pull the current full [[radiod]] list (from SCRATCH_JSON or disk)
# as a JSON array on stdout.
radiod_list_json() {
    python3 -c "
import json
try:
    scratch = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    scratch = {}
if 'radiod' in scratch:
    print(json.dumps(scratch['radiod']))
else:
    import subprocess
    try:
        out = subprocess.check_output(
            ['$PSK_RECORDER', 'config', 'show', '--json', '--defaults'] +
            ($CONFIG_PATH and ['--config', '$CONFIG_PATH'] or []),
            stderr=subprocess.DEVNULL,
        ).decode()
        print(json.dumps(json.loads(out).get('radiod', [])))
    except Exception:
        print('[]')
"
}

# Update the SCRATCH_JSON [[radiod]] list at index ``idx`` (or append
# when idx == -1 / >= len) with id + radiod_status; preserves any
# existing nested [radiod.ft8] / [radiod.ft4] sub-tables and freqs_hz.
radiod_block_update() {
    local idx="$1" new_id="$2" new_status="$3"
    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
blocks = d.get('radiod')
if blocks is None:
    blocks = json.loads(r'''$(radiod_list_json)''')
idx = $idx
if idx == -1 or idx >= len(blocks):
    blocks.append({'id': '$new_id', 'radiod_status': '$new_status'})
else:
    blocks[idx]['id']            = '$new_id'
    blocks[idx]['radiod_status'] = '$new_status'
d['radiod'] = blocks
print(json.dumps(d))
")
}

# Delete the [[radiod]] block at index idx (operator confirmed first).
radiod_block_delete() {
    local idx="$1"
    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
blocks = d.get('radiod')
if blocks is None:
    blocks = json.loads(r'''$(radiod_list_json)''')
del blocks[$idx]
d['radiod'] = blocks
print(json.dumps(d))
")
}

# psk-recorder's radiod id has the same shape as a callsign in spirit
# -- letters, digits, hyphens.  Let's not block legitimate values.
valid_radiod_id()   { [[ "$1" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; }
valid_mdns_host()   { [[ "$1" =~ ^[A-Za-z0-9._-]{1,253}$ ]]; }

collect_radiod() {
    # Build menu of existing blocks + "Add new" + "Done".
    while :; do
        local blocks_json
        blocks_json=$(radiod_list_json)
        # Whiptail menu items: tag = index (or "new" / "done"),
        # description = "id (radiod_status, ft8=N freqs_hz, ft4=M freqs_hz)".
        local menu_args
        menu_args=$(python3 -c "
import json
blocks = json.loads(r'''$blocks_json''')
items = []
for i, b in enumerate(blocks):
    rid = b.get('id', '?')
    status = b.get('radiod_status', '?')
    ft8 = len(b.get('ft8', {}).get('freqs_hz', []) or [])
    ft4 = len(b.get('ft4', {}).get('freqs_hz', []) or [])
    items += [str(i), f'{rid:<12s} status={status}  ft8={ft8} ft4={ft4}']
items += ['new',  '(add a new [[radiod]] block)']
items += ['done', '(back to main menu)']
import shlex
print(' '.join(shlex.quote(s) for s in items))
")
        # shellcheck disable=SC2086
        local pick
        pick=$(eval whiptail --title "'Radiod blocks'" \
                             --backtitle "'$BACKTITLE'" \
                             --menu '"Pick a [[radiod]] block to edit, or add a new one.\n\nfreqs_hz lists stay in the raw TOML -- use Edit-TOML from the main menu."' \
                             "$HEIGHT" "$WIDTH" "$LIST_HEIGHT" \
                             $menu_args 3>\&1 1>\&2 2>\&3) || return 1

        case "$pick" in
            done) return 0 ;;
            new)
                local new_id new_status
                new_id=$(ask     radiod.id            "" valid_radiod_id) || continue
                new_status=$(ask radiod.radiod_status "" valid_mdns_host) || continue
                radiod_block_update -1 "$new_id" "$new_status"
                ;;
            *)
                local cur_id cur_status
                cur_id=$(python3 -c "
import json
blocks = json.loads(r'''$blocks_json''')
print(blocks[$pick].get('id', ''))
")
                cur_status=$(python3 -c "
import json
blocks = json.loads(r'''$blocks_json''')
print(blocks[$pick].get('radiod_status', ''))
")
                # Sub-menu: edit / delete / back
                local subpick
                subpick=$(whiptail --title "Block '$cur_id'" \
                                   --backtitle "$BACKTITLE" \
                                   --menu "Edit or delete this [[radiod]] block?" \
                                   "$HEIGHT" "$WIDTH" 3 \
                                   "edit"   "change id / radiod_status" \
                                   "delete" "remove this block entirely" \
                                   "back"   "(back to block list)" \
                                   3>&1 1>&2 2>&3) || continue
                case "$subpick" in
                    edit)
                        local new_id new_status
                        new_id=$(ask     radiod.id            "$cur_id"     valid_radiod_id) || continue
                        new_status=$(ask radiod.radiod_status "$cur_status" valid_mdns_host) || continue
                        radiod_block_update "$pick" "$new_id" "$new_status"
                        ;;
                    delete)
                        if ask_yesno "Confirm delete" \
                                     "Remove [[radiod]] block id='$cur_id' status='$cur_status'?

This also drops the [radiod.ft8] and [radiod.ft4] sub-tables (freqs_hz lists) for this block.

The change isn't written until you choose Apply from the main menu."; then
                            radiod_block_delete "$pick"
                        fi
                        ;;
                    back) : ;;
                esac
                ;;
        esac
    done
}

collect_timing() {
    local chain_delay
    chain_delay=$(current_value timing chain_delay_ns)
    [[ -z "$chain_delay" ]] && chain_delay="0"
    chain_delay=$(ask timing.chain_delay_ns "$chain_delay" valid_nonneg_int) || return 1

    SCRATCH_JSON=$(python3 -c "
import json
try:
    d = json.loads(r'''$SCRATCH_JSON''')
except Exception:
    d = {}
d.setdefault('timing', {}).update({
    'chain_delay_ns': int('$chain_delay'),
})
print(json.dumps(d))
")
}

# Pick which radiod_id's env file to operate on.
# Returns the chosen id on stdout, or returns 1 on cancel.
pick_radiod_id_for_env() {
    local blocks_json
    blocks_json=$(radiod_list_json)
    local count
    count=$(python3 -c "
import json
print(len(json.loads(r'''$blocks_json''')))
")
    if [[ "$count" == "0" ]]; then
        whiptail --title "No radiod blocks" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "There are no [[radiod]] blocks defined yet.  Add one via the 'Radiod' menu item first, then come back here." \
                 12 "$WIDTH"
        return 1
    fi
    if [[ "$count" == "1" ]]; then
        # Single radiod -- no need to prompt.
        python3 -c "
import json
print(json.loads(r'''$blocks_json''')[0].get('id', ''))
"
        return 0
    fi
    # Multi-radiod: present a picker.
    local menu_args
    menu_args=$(python3 -c "
import json, shlex
blocks = json.loads(r'''$blocks_json''')
items = []
for b in blocks:
    rid    = b.get('id', '?')
    status = b.get('radiod_status', '?')
    items += [rid, f'status={status}']
print(' '.join(shlex.quote(s) for s in items))
")
    eval whiptail --title "'Pick instance for env'" \
                  --backtitle "'$BACKTITLE'" \
                  --menu '"Which [[radiod]] instance do you want to edit /etc/psk-recorder/env/<id>.env for?"' \
                  "$HEIGHT" "$WIDTH" "$LIST_HEIGHT" \
                  $menu_args 3>\&1 1>\&2 2>\&3 || return 1
}

# Summary of pipelines for the first radiod_id's env file, or "(unset)".
delivery_summary() {
    local blocks_json first_id env_file pipelines
    blocks_json=$(radiod_list_json)
    first_id=$(python3 -c "
import json
blocks = json.loads(r'''$blocks_json''')
print(blocks[0]['id'] if blocks else '')
" 2>/dev/null)
    [[ -z "$first_id" ]] && { echo "(no radiod blocks)"; return; }
    env_file=$("$PSK_RECORDER" env show --json --instance "$first_id" 2>/dev/null)
    pipelines=$(python3 -c "
import json
d = json.loads(r'''${env_file:-{}}''' or '{}')
v = d.get('PSK_DELIVERY_PIPELINES') or d.get('PSK_DELIVERY_MODE') or '(unset)'
print(v)
" 2>/dev/null)
    echo "${pipelines:-(unset)}"
}

# Read one key from /etc/sigmond/coordination.env (read-only display).
sigmond_coord_get() {
    local key="$1"
    [[ -r /etc/sigmond/coordination.env ]] || return 0
    sed -nE "s|^[[:space:]]*${key}=([\"']?)([^\"']*)\\1[[:space:]]*\$|\\2|p" \
        /etc/sigmond/coordination.env | tail -1
}

collect_delivery() {
    # Make sure there's a radiod block (env file is per-instance).
    local instance
    instance=$(pick_radiod_id_for_env) || return 1
    [[ -z "$instance" ]] && return 1

    # Show sigmond-managed context so the operator knows what's
    # coming from coordination.env vs the per-instance env file.
    local sqlite_path
    sqlite_path=$(sigmond_coord_get SIGMOND_SQLITE_PATH)
    [[ -z "$sqlite_path" ]] && sqlite_path="(unset -- sigmond.hamsci_sink will fall back to /var/lib/sigmond/sink.db)"
    whiptail --title "Sigmond-managed context (read-only)" \
             --backtitle "$BACKTITLE" \
             --msgbox "These come from /etc/sigmond/coordination.env (sigmond owns that file; the wizard never writes there):

  Local SQLite sink:  $sqlite_path
  STATION_CALL:       $(sigmond_coord_get STATION_CALL)
  STATION_GRID:       $(sigmond_coord_get STATION_GRID)

The next screens edit /etc/psk-recorder/env/$instance.env -- the per-instance upload-destination knobs psk-recorder reads at start (via systemd EnvironmentFile=)." \
             16 "$WIDTH"

    # Read current env file via `psk-recorder env show`.
    local current_env
    current_env=$("$PSK_RECORDER" env show --json --instance "$instance" 2>/dev/null)
    local cur_pipelines cur_hs_upload cur_dedup
    cur_pipelines=$(python3 -c "
import json
d = json.loads(r'''${current_env:-{}}''' or '{}')
print(d.get('PSK_DELIVERY_PIPELINES', d.get('PSK_DELIVERY_MODE', 'direct')))
")
    cur_hs_upload=$(python3 -c "
import json
d = json.loads(r'''${current_env:-{}}''' or '{}')
print(d.get('PSK_USE_HS_UPLOADER', '1'))
")
    cur_dedup=$(python3 -c "
import json
d = json.loads(r'''${current_env:-{}}''' or '{}')
print(d.get('PSK_DIRECT_DEDUP', '0'))
")

    # PSK_DELIVERY_PIPELINES: checklist of three options.
    # Build the per-option ON/OFF status from current value.
    local p_direct p_merge p_raw
    p_direct=$([[ ",$cur_pipelines," == *",direct,"*  ]] && echo "on" || echo "off")
    p_merge=$([[  ",$cur_pipelines," == *",server-merge,"* ]] && echo "on" || echo "off")
    p_raw=$([[    ",$cur_pipelines," == *",server-raw,"*   ]] && echo "on" || echo "off")
    local pick
    pick=$(whiptail --title "$(help_get env.PSK_DELIVERY_PIPELINES title)" \
                    --backtitle "$BACKTITLE" \
                    --separate-output \
                    --checklist "$(help_get env.PSK_DELIVERY_PIPELINES help)" \
                    "$HEIGHT" "$WIDTH" 3 \
                    "direct"       "POST direct to pskreporter.info"             "$p_direct" \
                    "server-merge" "ship to wsprdaemon, server forwards one"     "$p_merge" \
                    "server-raw"   "ship to wsprdaemon, suppress server forward" "$p_raw" \
                    3>&1 1>&2 2>&3) || return 1
    # Auto-downgrade: if both 'direct' and 'server-merge' selected,
    # server-merge -> server-raw (per recorder.py).
    local new_pipelines
    new_pipelines=$(python3 -c "
selected = set('$pick'.split())
if 'direct' in selected and 'server-merge' in selected:
    selected.discard('server-merge')
    selected.add('server-raw')
if not selected:
    selected = {'direct'}    # default fallback
print(','.join(sorted(selected)))
")

    # Booleans as yesno.
    local new_hs new_dedup
    if ask_yesno "$(help_get env.PSK_USE_HS_UPLOADER title)" \
                 "$(help_get env.PSK_USE_HS_UPLOADER help)"$'\n\n'"Current: $cur_hs_upload"; then
        new_hs=1
    else
        new_hs=0
    fi
    if ask_yesno "$(help_get env.PSK_DIRECT_DEDUP title)" \
                 "$(help_get env.PSK_DIRECT_DEDUP help)"$'\n\n'"Current: $cur_dedup"; then
        new_dedup=1
    else
        new_dedup=0
    fi

    # Write immediately via `psk-recorder env apply` -- env files are
    # cheap to round-trip; no need to defer until the main Apply step.
    local payload
    payload=$(python3 -c "
import json
print(json.dumps({
    'PSK_DELIVERY_PIPELINES': '$new_pipelines',
    'PSK_USE_HS_UPLOADER':    '$new_hs',
    'PSK_DIRECT_DEDUP':       '$new_dedup',
}))
")
    if ! printf '%s' "$payload" | \
            "$PSK_RECORDER" env apply --json - --instance "$instance"; then
        whiptail --title "env apply failed" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "Couldn't write /etc/psk-recorder/env/$instance.env.  See stderr for details.  Existing env file unchanged." \
                 12 "$WIDTH"
        return 1
    fi
    whiptail --title "Delivery written" \
             --backtitle "$BACKTITLE" \
             --msgbox "Wrote /etc/psk-recorder/env/$instance.env:

  PSK_DELIVERY_PIPELINES = $new_pipelines
  PSK_USE_HS_UPLOADER    = $new_hs
  PSK_DIRECT_DEDUP       = $new_dedup

Restart 'psk-recorder@$instance.service' for the change to take effect." \
             14 "$WIDTH"
}

edit_raw_toml() {
    # Resolve target file path (same logic configurator.py uses).
    local target="${CONFIG_PATH:-/etc/psk-recorder/psk-recorder-config.toml}"
    if [[ ! -f "$target" ]]; then
        whiptail --title "Config file not found" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "No file at $target.  Apply any pending changes first, then re-enter this option." \
                 12 "$WIDTH"
        return 0
    fi

    # Apply any pending wizard changes first so $EDITOR sees a consistent file.
    if [[ "$SCRATCH_JSON" != "{}" ]]; then
        if ! ask_yesno "Apply pending changes first?" \
                       "You have pending wizard edits (Station / Paths / Processing).

Apply them to $target before opening it in \$EDITOR?

Yes  -- write pending edits, then open the file
No   -- discard pending edits, open the file as-is"; then
            SCRATCH_JSON='{}'
        else
            if ! printf '%s' "$SCRATCH_JSON" | \
                    "$PSK_RECORDER" config apply --json - ${CONFIG_PATH:+--config "$CONFIG_PATH"}; then
                whiptail --title "Apply failed" \
                         --backtitle "$BACKTITLE" \
                         --msgbox "Couldn't write pending edits.  Aborting open." \
                         12 "$WIDTH"
                return 1
            fi
            SCRATCH_JSON='{}'
        fi
    fi

    local editor="${EDITOR:-${VISUAL:-nano}}"
    if ! command -v "$editor" >/dev/null 2>&1; then
        editor=$(command -v nano 2>/dev/null || command -v vi)
        [[ -z "$editor" ]] && {
            whiptail --title "No editor found" \
                     --backtitle "$BACKTITLE" \
                     --msgbox "No \$EDITOR / \$VISUAL / nano / vi found on PATH.  Hand-edit $target with whatever you have." \
                     12 "$WIDTH"
            return 0
        }
    fi
    # Drop out of the whiptail UI for the editor session.
    clear
    "$editor" "$target"
    # Re-validate via psk-recorder validate after the edit.
    local validate_rc=0
    "$PSK_RECORDER" validate --json ${CONFIG_PATH:+--config "$CONFIG_PATH"} >/dev/null 2>&1 \
        || validate_rc=$?
    if [[ $validate_rc -ne 0 ]]; then
        whiptail --title "Validation warnings" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "psk-recorder validate reported issues after your edit.  Run

    psk-recorder validate --json | jq

to see the details.  The file was written as you saved it -- this is just a heads-up." \
                 14 "$WIDTH"
    fi
}

main_menu_loop() {
    # Display normalization: leftover template placeholders rendered as "(unset)".
    display() {
        local v="$1"
        if [[ -z "$v" || "$v" =~ ^\<.*\>$ ]]; then
            echo "(unset)"
        else
            echo "$v"
        fi
    }

    while :; do
        local cur_call cur_grid cur_spool cur_decoder cur_lifetime
        cur_call=$(display     "$(current_value station    callsign)")
        cur_grid=$(display     "$(current_value station    grid_square)")
        cur_spool=$(display    "$(current_value paths      spool_dir)")
        cur_decoder=$(display  "$(current_value paths      decoder)")
        cur_lifetime=$(current_value processing radiod_lifetime_frames)

        local cur_timing cur_radiods cur_pipelines
        cur_timing=$(current_value timing chain_delay_ns)
        # Radiod summary: comma-separated list of ids.
        cur_radiods=$(radiod_ids_summary)
        # Delivery summary: PSK_DELIVERY_PIPELINES for the first radiod_id.
        cur_pipelines=$(delivery_summary)

        local choice
        choice=$(whiptail --title "psk-recorder configuration" \
                          --backtitle "$BACKTITLE" \
                          --cancel-button "Exit wizard" \
                          --menu "Pick a section to edit, or Apply when you're done.

Each section's questions walk linearly; Cancel inside a section
drops back here instead of aborting." \
                          "$HEIGHT" "$WIDTH" 9 \
                          "Station"    "Call=$cur_call  Grid=$cur_grid" \
                          "Paths"      "spool=$cur_spool  decoder=$cur_decoder" \
                          "Processing" "lifetime=${cur_lifetime:-(unset)} frames" \
                          "Timing"     "chain_delay=${cur_timing:-0} ns (sigmond usually overrides)" \
                          "Radiod"     "blocks: $cur_radiods" \
                          "Delivery"   "pipelines: $cur_pipelines (per-instance env)" \
                          "Edit-TOML"  "Open raw config in \$EDITOR (for freqs_hz lists)" \
                          "Apply"      "Review and write changes" \
                          "Cancel"     "Discard pending changes and exit" \
                          3>&1 1>&2 2>&3)
        if [[ $? -ne 0 ]]; then
            if ask_yesno "Discard changes?" "Discard any pending changes and exit the wizard?"; then
                return 1
            fi
            continue
        fi

        case "$choice" in
            Station)    collect_station    || true ;;
            Paths)      collect_paths      || true ;;
            Processing) collect_processing || true ;;
            Timing)     collect_timing     || true ;;
            Radiod)     collect_radiod     || true ;;
            Delivery)   collect_delivery   || true ;;
            Edit-TOML)  edit_raw_toml      || true ;;
            Apply)
                if confirm_and_write; then
                    return 0
                fi
                ;;
            Cancel)
                if ask_yesno "Discard changes?" "Discard any pending changes and exit the wizard?"; then
                    return 1
                fi
                ;;
        esac
    done
}

confirm_and_write() {
    if [[ "$SCRATCH_JSON" == "{}" ]]; then
        whiptail --title "Nothing to apply" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "You haven't changed any of the wizard-managed sections (Station / Paths / Processing).

If you only used 'Edit raw TOML', your changes were written directly when you saved the file.  Exit the wizard to confirm." \
                 14 "$WIDTH"
        return 1
    fi

    local summary
    summary=$(python3 -c "
import json
d = json.loads(r'''$SCRATCH_JSON''')
lines = []
def walk(prefix, obj):
    for k, v in obj.items():
        if isinstance(v, dict):
            walk(prefix + k + '.', v)
        else:
            lines.append(f'{prefix}{k} = {v!r}')
walk('', d)
print('\n'.join(lines))
")
    if ! whiptail --title "Review and write" \
                  --backtitle "$BACKTITLE" \
                  --yesno "About to apply the following to ${CONFIG_PATH:-/etc/psk-recorder/psk-recorder-config.toml}:

$summary

Continue?" "$HEIGHT" "$WIDTH"; then
        return 1
    fi
    if ! printf '%s' "$SCRATCH_JSON" | \
            "$PSK_RECORDER" config apply --json - ${CONFIG_PATH:+--config "$CONFIG_PATH"}; then
        whiptail --title "Apply failed" \
                 --backtitle "$BACKTITLE" \
                 --msgbox "psk-recorder config apply rejected the input.  See stderr for details.  Existing config was not modified." \
                 12 "$WIDTH"
        return 1
    fi
    whiptail --title "Config written" \
             --backtitle "$BACKTITLE" \
             --msgbox "Configuration written.

Next steps:
  - Verify:   psk-recorder validate --json | jq
  - Restart:  sudo systemctl restart psk-recorder@<radiod_id>.service

Note: [[radiod]] blocks and freqs_hz weren't touched by this wizard -- if you need to change those, re-enter the wizard and pick 'Edit raw TOML'." \
             "$HEIGHT" "$WIDTH"
}

# -------- main flow --------------------------------------------------

SCRATCH_JSON='{}'

welcome_screen || { echo "wizard: cancelled at welcome" >&2; exit 1; }
if main_menu_loop; then
    exit 0
else
    echo "wizard: exited without writing" >&2
    exit 1
fi
