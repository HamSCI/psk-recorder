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

# --- Phase 1.4: ensure uv is on PATH (canonical sigmond-suite installer) ---
# Delegates to sigmond's shared helper if present; inline fallback for
# the bootstrap case.  Keep the fallback in sync with
# sigmond/scripts/install/ensure_uv.sh.
_ENSURE_UV_SH="/opt/git/sigmond/sigmond/scripts/install/ensure_uv.sh"
if [[ -r "$_ENSURE_UV_SH" ]]; then
    # shellcheck source=/dev/null
    source "$_ENSURE_UV_SH"
else
    _ensure_uv() {
        if command -v uv >/dev/null 2>&1; then
            printf '[INFO]  uv %s at %s\n' "$(uv --version 2>/dev/null | awk '{print $2}')" "$(command -v uv)"
            return 0
        fi
        printf '[INFO]  uv not found -- installing system-wide to /usr/local/bin\n'
        command -v curl >/dev/null || { printf '[ERROR] curl not found (apt install curl)\n' >&2; return 1; }
        if ! curl -LsSf https://astral.sh/uv/install.sh | env XDG_BIN_HOME=/usr/local/bin UV_NO_MODIFY_PATH=1 sh; then
            printf '[ERROR] uv installer failed\n' >&2
            return 1
        fi
        command -v uv >/dev/null || { printf '[ERROR] uv installer ran but uv is still not on PATH\n' >&2; return 1; }
        printf '[INFO]  uv %s installed\n' "$(uv --version 2>/dev/null | awk '{print $2}')"
    }
fi
_ensure_uv || { ui_error "_ensure_uv failed"; exit 1; }

# --- Phase 1.5: ensure sibling repos (callhash, hs-uploader, ka9q-python) ---
# pyproject.toml declares these via [tool.uv.sources] with `editable = true`
# and `path = "../<name>"`.  uv sync honors that natively (unlike plain
# pip), so we don't need an explicit pre-install pass anymore -- just
# verify the sibling repos exist on disk first.  If a sibling isn't at
# the canonical location, relocate from common alternates (~, ~/git,
# /opt/git) or clone upstream.
_ensure_sibling() {
    local name="$1" repo_url="$2"
    local target="/opt/git/sigmond/$name"

    if [[ -f "$target/pyproject.toml" ]]; then
        return 0
    fi

    ui_info "Sibling $name not at $target — searching common locations"
    local invoker="${SUDO_USER:-${USER:-$(id -un)}}"
    local src=""
    for candidate in \
        "/home/$invoker/$name" \
        "/home/$invoker/git/$name" \
        "/opt/git/$name"; do
        if [[ -f "$candidate/pyproject.toml" ]]; then
            src="$candidate"
            break
        fi
    done

    if [[ -n "$src" ]]; then
        ui_info "Found at $src — relocating to $target"
        if [[ -d "$target" && -n "$(ls -A "$target" 2>/dev/null)" ]]; then
            ui_error "$target exists and is non-empty — inspect and remove first"
            exit 1
        fi
        mkdir -p "$(dirname "$target")"
        [[ -d "$target" ]] && rmdir "$target"
        mv "$src" "$target"
        ui_info "Relocated $name to $target"
    else
        ui_info "Not found locally — cloning from $repo_url"
        git clone "$repo_url" "$target" || {
            ui_error "Failed to clone $repo_url"
            exit 1
        }
    fi
}

_ensure_sibling callhash    https://github.com/mijahauan/callhash
_ensure_sibling hs-uploader https://github.com/mijahauan/hs-uploader
_ensure_sibling ka9q-python https://github.com/mijahauan/ka9q-python

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

# Recreate the venv if it doesn't exist.  (An incomplete venv from a
# crashed previous install is also handled here -- uv venv --allow-existing
# would normally fail loudly; rm+recreate is safer for the bootstrap case.)
if [[ ! -d "$VENV_DIR" ]]; then
    ui_info "Creating venv at $VENV_DIR"
    mkdir -p "$(dirname "$VENV_DIR")"
    # --seed populates pip/setuptools/wheel for compatibility with tooling
    # that shells out to pip (e.g. the vendored pskreporter.py install
    # step below uses install(1), not pip, so --seed is not strictly
    # required here -- but it harmlessly keeps the venv layout
    # consistent with what pip-based tooling expects).
    uv venv "$VENV_DIR" --python 3.11 --seed --quiet
fi

# uv sync reads pyproject.toml + uv.lock, resolves [tool.uv.sources]
# (callhash, hs-uploader, ka9q-python all editable from sibling paths),
# installs psk-recorder itself editable into the venv, and pins exactly
# what's in uv.lock.  --no-dev skips dev extras (pytest etc.); --frozen
# requires uv.lock to be current (regenerate locally with `uv lock` if
# siblings or deps have shifted).
ui_info "Syncing psk-recorder + siblings (callhash, hs-uploader, ka9q-python) into $VENV_DIR"
UV_PROJECT_ENVIRONMENT="$VENV_DIR" \
    uv sync --project "$REPO_SOURCE" --frozen --no-dev --quiet

# sigmond is the host-wide orchestrator; psk-recorder lazy-imports
# sigmond.wizard_dispatch from configurator.py for the whiptail wizard
# plumbing (helpers shared with mag-recorder / wspr-recorder via
# sigmond's lib).  Falls back to a local implementation when absent
# so this install is recommended but not strictly required.  NOT
# declared in pyproject.toml so uv sync doesn't install it; explicit
# uv pip install when the sibling exists.
if [[ -d /opt/git/sigmond/sigmond ]]; then
    ui_info "Installing sigmond (editable) into venv"
    # uv pip install needs --python (not UV_PROJECT_ENVIRONMENT, which only
    # applies to project-level commands like uv sync).
    uv pip install --quiet --python "$VENV_DIR/bin/python3" -e /opt/git/sigmond/sigmond
else
    ui_info "sigmond repo not found at /opt/git/sigmond/sigmond -- wizard"
    ui_info "will use the local legacy-fallback dispatch."
fi

# Install our vendored pskreporter.py directly into the venv's
# site-packages.  We don't depend on the upstream `pjsg/ftlib-
# pskreporter` package because (1) the vendored copy is stdlib-only
# (no docopt/etc. needed — we only use the library, not the
# pskreporter-sender CLI), and (2) we carry two env-var knobs
# (PSKREPORTER_INTERVAL + PSKREPORTER_NO_DEDUP) that upstream hasn't
# merged.  See vendor/pskreporter.py for the full diff vs upstream.
# Idempotent.
VENDOR_PSKREPORTER="$REPO_SOURCE/vendor/pskreporter.py"
if [[ ! -f "$VENDOR_PSKREPORTER" ]]; then
    ui_error "$VENDOR_PSKREPORTER missing — repo is incomplete"
    exit 1
fi
PYVER=$("$VENV_DIR/bin/python3" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')
SITE_PKGS="$VENV_DIR/lib/$PYVER/site-packages"
install -m 644 "$VENDOR_PSKREPORTER" "$SITE_PKGS/pskreporter.py"
ui_info "Installed vendored pskreporter -> $SITE_PKGS/pskreporter.py"

# Verify the daemon can import it as the service user.
if ! sudo -u "$SERVICE_USER" "$VENV_DIR/bin/python3" -c 'import pskreporter' 2>/dev/null; then
    ui_error "Post-install: $SERVICE_USER cannot import pskreporter"
    exit 1
fi

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
# Both the singleton form (ft8-decode.service, written by ka9q-radio's
# package install) and the instance form (ft8-decode@*.service, used in
# some multi-band deployments) need to be disabled — the legacy decoders
# write /var/log/ft{8,4}.log which the legacy `pskreporter@ft8/ft4` path
# tails and uploads, duplicating every spot psk-recorder already ships
# from psk.spots.  Match list-unit-files (catches inactive-but-enabled
# units) as well as list-units (running ones).
for pattern in 'ft8-decode.service' 'ft4-decode.service' \
               'ft8-decode@*.service' 'ft4-decode@*.service' \
               'pskreporter@ft8.service' 'pskreporter@ft4.service'; do
    for unit in $(systemctl list-unit-files --no-legend "$pattern" 2>/dev/null | awk '{print $1}'); do
        if [[ -n "$unit" ]] && systemctl is-enabled "$unit" >/dev/null 2>&1; then
            ui_warn "Disabling legacy unit: $unit (psk-recorder replaces it)"
            systemctl disable --now "$unit" || true
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
