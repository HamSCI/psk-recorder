#!/bin/bash
# install.sh — first-run bootstrap for psk-recorder (Pattern A editable install)
#
# Usage: sudo ./scripts/install.sh [--pull] [--yes]
#
# What it does:
#   1. Creates service user pskrec:pskrec
#   2. Clones/links repo to /opt/git/sigmond/psk-recorder
#   3. Creates venv at /opt/psk-recorder/venv with editable install
#   4. Renders config template (non-destructive — never overwrites)
#   5. Installs systemd unit template
#   6. Disables native ka9q-radio FT services if running
#   7. Enables psk-recorder@<radiod_id> instances from config
#
# Idempotent: safe to re-run.

set -euo pipefail

SERVICE_USER="pskrec"
SERVICE_GROUP="pskrec"
REPO_SOURCE="/opt/git/sigmond/psk-recorder"
VENV_DIR="/opt/psk-recorder/venv"
CONFIG_DIR="/etc/psk-recorder"
CONFIG_FILE="${CONFIG_DIR}/psk-recorder-config.toml"
SPOOL_DIR="/var/lib/psk-recorder"
LOG_DIR="/var/log/psk-recorder"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ui_info()  { echo "[INFO]  $*"; }
ui_warn()  { echo "[WARN]  $*" >&2; }
ui_error() { echo "[ERROR] $*" >&2; }

# --- Phase 0: arg parsing ---
DO_PULL=false
AUTO_YES=false
for arg in "$@"; do
    case "$arg" in
        --pull) DO_PULL=true ;;
        --yes)  AUTO_YES=true ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    ui_error "Must run as root (sudo)"
    exit 1
fi

# --- Phase 1: service user ---
if ! id -u "$SERVICE_USER" &>/dev/null; then
    ui_info "Creating service user $SERVICE_USER"
    useradd --system --shell /usr/sbin/nologin \
            --home-dir /nonexistent --no-create-home \
            "$SERVICE_USER"
fi

# --- Phase 2: repo + venv ---
if [[ ! -d "$REPO_SOURCE" ]]; then
    ui_info "Linking $REPO_ROOT -> $REPO_SOURCE"
    mkdir -p "$(dirname "$REPO_SOURCE")"
    ln -sfn "$REPO_ROOT" "$REPO_SOURCE"
fi

# Traversability check (Pattern A defense)
if ! sudo -u "$SERVICE_USER" test -r "$REPO_SOURCE/src/psk_recorder/__init__.py"; then
    ui_error "Service user $SERVICE_USER cannot read $REPO_SOURCE/src/psk_recorder/__init__.py"
    ui_error "Fix: ensure the repo is at /opt/git/sigmond/psk-recorder (not under a mode-700 home)"
    ui_error "  or: chmod g+rx the path and add $SERVICE_USER to the owner's group"
    exit 1
fi

if $DO_PULL; then
    ui_info "Pulling latest from origin"
    git -C "$REPO_SOURCE" pull --ff-only
fi

# Recreate the venv if it doesn't exist, OR if it's incomplete (a
# previous install that crashed before bootstrapping pip leaves a
# partial venv with python but no bin/pip — re-running install would
# then trip on `$VENV_DIR/bin/pip` not existing).
if [[ ! -d "$VENV_DIR" ]] || [[ ! -x "$VENV_DIR/bin/pip" ]]; then
    if [[ -d "$VENV_DIR" ]]; then
        ui_warn "Venv at $VENV_DIR is incomplete — recreating"
        rm -rf "$VENV_DIR"
    fi
    ui_info "Creating venv at $VENV_DIR"
    mkdir -p "$(dirname "$VENV_DIR")"
    python3 -m venv "$VENV_DIR"
fi

ui_info "Installing psk-recorder (editable) into venv"
"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel >/dev/null
"$VENV_DIR/bin/pip" install -e "$REPO_SOURCE" >/dev/null

# Post-install verify
if ! sudo -u "$SERVICE_USER" "$VENV_DIR/bin/python3" -c 'import psk_recorder' 2>/dev/null; then
    ui_error "Post-install verify failed: $SERVICE_USER cannot import psk_recorder"
    exit 1
fi
ui_info "Post-install verify OK"

# --- Phase 3: config ---
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_FILE" ]]; then
    ui_info "Rendering config template -> $CONFIG_FILE"
    cp "$REPO_SOURCE/config/psk-recorder-config.toml.template" "$CONFIG_FILE"
    ui_warn "Edit $CONFIG_FILE with your callsign, grid, and radiod settings"
else
    ui_info "Config exists at $CONFIG_FILE — not overwriting"
fi

# --- Phase 4: directories ---
for dir in "$SPOOL_DIR" "$LOG_DIR"; do
    mkdir -p "$dir"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$dir"
done

# --- Phase 4.5: bundled jt9 decoder binaries ---
# Ship per-arch binaries under /opt/psk-recorder/bin/decoders/ (project-
# scoped, never in /usr/local/sbin).  Avoids pulling in the full WSJT-X
# GUI package — only the few runtime shared libs it needs.  After
# installing all arches, symlink the active host's arch to a stable
# `jt9` filename so the config can reference one path on every box.
INSTALL_DEC_DIR="/opt/psk-recorder/bin/decoders"
ui_info "Installing bundled jt9 binaries to $INSTALL_DEC_DIR"
mkdir -p "$INSTALL_DEC_DIR"
install -o root -g root -m 755 "$REPO_ROOT/bin/decoders/jt9-x86-v27"   "$INSTALL_DEC_DIR/"
install -o root -g root -m 755 "$REPO_ROOT/bin/decoders/jt9-arm64-v27" "$INSTALL_DEC_DIR/"
install -o root -g root -m 755 "$REPO_ROOT/bin/decoders/jt9-arm32-v26" "$INSTALL_DEC_DIR/"

case "$(uname -m)" in
    x86_64)        arch_jt9="jt9-x86-v27" ;;
    aarch64|arm64) arch_jt9="jt9-arm64-v27" ;;
    armv7l|armhf)  arch_jt9="jt9-arm32-v26" ;;
    *)             arch_jt9="" ;;
esac
if [[ -n "$arch_jt9" ]]; then
    ln -sfn "$INSTALL_DEC_DIR/$arch_jt9" "$INSTALL_DEC_DIR/jt9"
    ui_info "  symlinked $INSTALL_DEC_DIR/jt9 → $arch_jt9"
else
    ui_warn "unknown architecture $(uname -m); $INSTALL_DEC_DIR/jt9 not symlinked"
    ui_warn "set paths.decoder_jt9 explicitly in /etc/psk-recorder/psk-recorder-config.toml"
fi

# Verify jt9's runtime deps are present.  We don't pull `wsjtx` (which
# would drag in Qt5Gui + sample sounds + ~150 MB of GUI parts); only
# the minimum shared libs jt9 dlopens are required.
missing_libs=()
for lib in libQt5Core.so.5 libfftw3f.so.3 libgfortran.so.5; do
    if ! ldconfig -p | grep -q "$lib"; then
        missing_libs+=("$lib")
    fi
done
if (( ${#missing_libs[@]} > 0 )); then
    ui_warn "jt9 runtime libs missing: ${missing_libs[*]}"
    ui_warn "Install with: sudo apt install libqt5core5a libfftw3-single3 libgfortran5"
fi

# --- Phase 5: systemd ---
ui_info "Installing systemd unit template"
install -o root -g root -m 644 \
    "$REPO_SOURCE/systemd/psk-recorder@.service" \
    /etc/systemd/system/psk-recorder@.service
systemctl daemon-reload

# --- Phase 6: disable native ka9q-radio FT services ---
for unit in ft8-record.service ft4-record.service; do
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        ui_warn "Disabling native ka9q-radio unit: $unit (psk-recorder replaces it)"
        systemctl disable --now "$unit"
    fi
done
for pattern in 'ft8-decode@*.service' 'ft4-decode@*.service' \
               'pskreporter@ft8.service' 'pskreporter@ft4.service'; do
    for unit in $(systemctl list-units --plain --no-legend "$pattern" 2>/dev/null | awk '{print $1}'); do
        if [[ -n "$unit" ]]; then
            ui_warn "Disabling native ka9q-radio unit: $unit (psk-recorder replaces it)"
            systemctl disable --now "$unit"
        fi
    done
done

# --- Phase 7: enable instances ---
ui_info "Parsing radiod IDs from $CONFIG_FILE"
RADIOD_IDS=$("$VENV_DIR/bin/python3" -c "
import tomllib
with open('$CONFIG_FILE', 'rb') as f:
    cfg = tomllib.load(f)
blocks = cfg.get('radiod', [])
if isinstance(blocks, dict):
    blocks = [blocks]
for b in blocks:
    print(b.get('id', 'default'))
" 2>/dev/null)

if [[ -z "$RADIOD_IDS" ]]; then
    ui_warn "No radiod IDs found in config — no instances enabled"
else
    for rid in $RADIOD_IDS; do
        ui_info "Enabling psk-recorder@${rid}.service"
        systemctl enable "psk-recorder@${rid}.service"
        # Don't start yet — daemon is Phase 1 stub
        ui_info "  (not starting — daemon not yet implemented)"
    done
fi

ui_info "Install complete. Edit $CONFIG_FILE then start instances with:"
ui_info "  sudo systemctl start psk-recorder@<radiod-id>"
