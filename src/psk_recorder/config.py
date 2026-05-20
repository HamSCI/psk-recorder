"""TOML config loader and defaults for psk-recorder."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_PATH = Path("/etc/psk-recorder/psk-recorder-config.toml")

DEFAULTS: dict[str, Any] = {
    "paths": {
        "spool_dir": "/var/lib/psk-recorder",
        "log_dir": "/var/log/psk-recorder",
        "decoder": "/usr/local/bin/decode_ft8",
        "pskreporter": "/usr/local/bin/pskreporter-sender",
        "keep_wav": False,
    },
    "processing": {
        # radiod LIFETIME tag (ka9q-python ≥3.13.0, ka9q-radio ≥0f8b622).
        # Channels self-destruct after this many radiod main-loop frames
        # (~50 Hz at the default 20 ms blocktime, so 6000 ≈ 2 min).  The
        # recorder refreshes lifetime every (frames / 4) seconds while
        # running, so a crashed/killed recorder leaves no residual
        # channels on radiod within ~2 min.
        # 0 = infinite (no LIFETIME tag, no keep-alive — radiod owns the
        # channel for its full template default).  Use only when you
        # truly want a channel to outlive the recorder.
        "radiod_lifetime_frames": 6000,
    },
}

FT8_CADENCE_SEC = 15.0
FT4_CADENCE_SEC = 7.5
DEFAULT_SAMPLE_RATE = 12000
DEFAULT_PRESET = "usb"
DEFAULT_ENCODING = "s16be"


def load_config(path: Path | None = None) -> dict:
    """Load and merge config with defaults."""
    config_path = path or Path(
        os.environ.get("PSK_RECORDER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    raw.setdefault("paths", {})
    for key, val in DEFAULTS["paths"].items():
        raw["paths"].setdefault(key, val)

    raw.setdefault("processing", {})
    for key, val in DEFAULTS["processing"].items():
        raw["processing"].setdefault(key, val)

    lifetime = raw["processing"]["radiod_lifetime_frames"]
    if not isinstance(lifetime, int) or lifetime < 0:
        raise ValueError(
            f"processing.radiod_lifetime_frames must be a non-negative int "
            f"(frames; ~50 Hz at default blocktime); got {lifetime!r}"
        )

    return raw


def resolve_radiod_block(config: dict, radiod_id: str | None) -> dict:
    """Find the [[radiod]] block matching radiod_id.

    If radiod_id is None, the config must contain exactly one [[radiod]].
    """
    radiod_blocks = config.get("radiod", [])
    if isinstance(radiod_blocks, dict):
        radiod_blocks = [radiod_blocks]

    if not radiod_blocks:
        raise ValueError("Config contains no [[radiod]] blocks")

    if radiod_id is None:
        if len(radiod_blocks) != 1:
            raise ValueError(
                f"--radiod-id required: config has {len(radiod_blocks)} "
                f"[[radiod]] blocks"
            )
        return radiod_blocks[0]

    for block in radiod_blocks:
        if block.get("id") == radiod_id:
            return block

    available = [b.get("id", "<unnamed>") for b in radiod_blocks]
    raise ValueError(
        f"No [[radiod]] block with id={radiod_id!r}. "
        f"Available: {', '.join(available)}"
    )


def get_freqs(radiod_block: dict, mode: str) -> list[int]:
    """Extract frequency list for a mode ('ft4' or 'ft8')."""
    mode_block = radiod_block.get(mode, {})
    return list(mode_block.get("freqs_hz", []))


def get_mode_params(radiod_block: dict, mode: str) -> dict:
    """Extract sample_rate, preset, encoding for a mode."""
    mode_block = radiod_block.get(mode, {})
    return {
        "sample_rate": int(mode_block.get("sample_rate", DEFAULT_SAMPLE_RATE)),
        "preset": mode_block.get("preset", DEFAULT_PRESET),
        "encoding": mode_block.get("encoding", DEFAULT_ENCODING),
    }


def resolve_radiod_status(radiod_block: dict) -> str:
    """Resolve the radiod mDNS hostname.

    Precedence:
      1. RADIOD_<ID>_STATUS from environment (sigmond-supplied)
      2. radiod_status field in the [[radiod]] block (standalone fallback)
    """
    radiod_id = radiod_block.get("id", "")
    env_key = f"RADIOD_{radiod_id.upper().replace('-', '_')}_STATUS"
    from_env = os.environ.get(env_key)
    if from_env:
        return from_env

    status = radiod_block.get("radiod_status")
    if not status:
        raise ValueError(
            f"[[radiod]] id={radiod_id!r} has no radiod_status and "
            f"{env_key} is not set in the environment"
        )
    return status


def derive_source_key(radiod_block: dict) -> str:
    """Canonical source identifier for a radiod block.

    Returns ``radiod:<resolved_status_address>`` — matches
    ``sigmond.sources.SourceKey`` string form and wspr-recorder's
    ``SourceConfig.key`` so a spot's ``rx_source`` tag is comparable
    across clients.

    Status resolution follows ``resolve_radiod_status`` precedence
    (env override → ``radiod_status`` field), so the key reflects what
    the recorder is actually talking to at runtime, not a static
    config field.
    """
    return f"radiod:{resolve_radiod_status(radiod_block)}"


def ensure_sources(config: dict) -> list[dict]:
    """Normalise ``[[radiod]]`` blocks into a list of source descriptors.

    Foundation for multi-source psk-recorder (Phase B).  Today each
    daemon process still serves one radiod via ``--radiod-id`` /
    ``resolve_radiod_block``; this helper just synthesises the
    Phase-B-shaped list so other code can be written against the
    final shape now.

    Each entry has:
      ``key``               — ``radiod:<status_address>`` (matches
                              ``derive_source_key``)
      ``radiod_id``         — short id from the ``[[radiod]]`` block
      ``status_address``    — resolved mDNS hostname
      ``radiod_block``      — the original block (freqs, lifetime, etc.)
    """
    blocks = config.get("radiod", [])
    if isinstance(blocks, dict):
        blocks = [blocks]

    sources: list[dict] = []
    for block in blocks:
        try:
            status = resolve_radiod_status(block)
        except ValueError:
            # Skip blocks that haven't been wired up yet — same shape
            # ``resolve_radiod_block`` would reject; callers should
            # not assume every entry resolves.
            continue
        sources.append({
            "key": f"radiod:{status}",
            "radiod_id": block.get("id", "default"),
            "status_address": status,
            "radiod_block": block,
        })
    return sources
