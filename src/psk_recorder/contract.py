"""Client-contract v0.6 inventory and validate JSON builders."""

from __future__ import annotations

import logging
import os
import shutil
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from psk_recorder.config import (
    get_freqs,
    get_mode_params,
    load_config,
    resolve_radiod_status,
)
from psk_recorder.version import GIT_INFO

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "0.7"


def build_inventory(config: dict, config_path: Path) -> dict:
    """Build the inventory --json payload per contract v0.3."""
    station = config.get("station", {})
    paths = config.get("paths", {})
    log_dir = paths.get("log_dir", "/var/log/psk-recorder")

    try:
        version = pkg_version("psk-recorder")
    except Exception:
        version = "0.1.0"

    radiod_blocks = config.get("radiod", [])
    if isinstance(radiod_blocks, dict):
        radiod_blocks = [radiod_blocks]

    instances = []
    all_log_paths: dict[str, Any] = {}

    for block in radiod_blocks:
        radiod_id = block.get("id", "default")  # local label; used for
                                                # env-var keys, log paths
        ft8_freqs = get_freqs(block, "ft8")
        ft4_freqs = get_freqs(block, "ft4")
        all_freqs = sorted(set(ft8_freqs + ft4_freqs))

        try:
            status_dns = resolve_radiod_status(block)
        except ValueError:
            status_dns = block.get("radiod_status", "")

        # RADIOD-IDENTIFICATION.md §3.2 — inventory radiod_id is the
        # mDNS control/status multicast name (the only functional
        # identifier).  Fall back to the local label when the operator
        # hasn't declared radiod_status yet, so legacy configs still
        # produce parseable inventory.  The local `radiod_id` variable
        # remains the per-block label for env-var lookups and file
        # naming — that internal use isn't changing in Phase 2.
        inventory_radiod_id = status_dns or radiod_id

        chain_delay_env = f"RADIOD_{radiod_id.upper().replace('-', '_')}_CHAIN_DELAY_NS"
        chain_delay_raw = os.environ.get(chain_delay_env)
        chain_delay = int(chain_delay_raw) if chain_delay_raw else None

        modes = []
        if ft8_freqs:
            modes.append("ft8")
        if ft4_freqs:
            modes.append("ft4")

        spool_path = f"{paths.get('spool_dir', '/var/lib/psk-recorder')}/{radiod_id}"

        # CONTRACT v0.6 §17 — output sinks per instance.  psk-recorder
        # writes spots into sigmond's local SQLite sink (via the
        # in-process tailer) and to per-mode log files; both are file
        # sinks from the contract's point of view.
        data_sinks: list[dict[str, Any]] = [
            {
                "kind":           "file",
                "target":         spool_path,
                "schema_ref":     None,
                "retention_days": 0,
                "mb_per_day":     0,
            },
            {
                "kind":           "file",
                "target":         log_dir,
                "schema_ref":     None,
                "retention_days": 365,
                "mb_per_day":     5,
            },
        ]

        instance = {
            "instance": radiod_id,
            "radiod_id": inventory_radiod_id,
            "host": "localhost",
            "radiod_status_dns": status_dns,
            "data_destination": None,
            "ka9q_channels": len(ft8_freqs) + len(ft4_freqs),
            "frequencies_hz": all_freqs,
            "modes": modes,
            "data_sinks": data_sinks,
            "uses_timing_calibration": False,
            "provides_timing_calibration": False,
            "chain_delay_ns_applied": chain_delay,
            # CONTRACT v0.7 §18 — runtime-state field for the §18
            # subscription. psk-recorder runs in RTP-default mode (PSK
            # decoding is ms-tolerant; no hard-deadline scheduling
            # against UTC, so subscribing to a peer authority would
            # not improve spot quality). Reported as null to satisfy
            # the v0.7 inventory shape and signal "contract-aware,
            # currently default mode."
            "timing_authority_applied": None,
        }
        instances.append(instance)

        # The process log goes to the systemd journal
        # (StandardOutput=journal) — see it via `smd log psk-recorder`.
        # log_paths lists only file-based logs (for `smd log --files`).
        instance_logs: dict[str, Any] = {}
        spot_logs: dict[str, str] = {}
        if ft8_freqs:
            spot_logs["ft8"] = f"{log_dir}/{radiod_id}-ft8.log"
        if ft4_freqs:
            spot_logs["ft4"] = f"{log_dir}/{radiod_id}-ft4.log"
        if spot_logs:
            instance_logs["spots"] = spot_logs
        all_log_paths[radiod_id] = instance_logs

    effective_level = logging.getLogger().getEffectiveLevel()
    log_level_name = logging.getLevelName(effective_level)

    payload: dict[str, Any] = {
        "client": "psk-recorder",
        "version": version,
        "contract_version": CONTRACT_VERSION,
        "config_path": str(config_path),
    }

    if GIT_INFO:
        payload["git"] = GIT_INFO

    if all_log_paths:
        payload["log_paths"] = all_log_paths

    payload["log_level"] = log_level_name
    payload["instances"] = instances
    payload["deps"] = {
        "git": [
            {"name": "ka9q-radio", "note": "decode_ft8 binary"},
        ],
        "pypi": [
            {"name": "ka9q-python", "version": ">=3.6.0"},
        ],
    }
    payload["issues"] = _collect_issues(config, paths)

    return payload


def build_validate(config: dict, config_path: Path | None = None) -> dict:
    """Build the validate --json payload per contract v0.4.

    §12.3: report the absolute path of the loaded config.
    """
    paths = config.get("paths", {})
    issues = _collect_issues(config, paths)
    payload: dict[str, Any] = {
        "ok": not any(i["severity"] == "fail" for i in issues),
    }
    if config_path is not None:
        payload["config_path"] = str(config_path)
    payload["issues"] = issues
    return payload


def _collect_issues(config: dict, paths: dict) -> list[dict]:
    """Run validation checks and return issues list."""
    issues: list[dict] = []

    station = config.get("station", {})
    if not station.get("callsign"):
        issues.append({
            "severity": "warn",
            "instance": "all",
            "message": "station.callsign is empty",
        })
    if not station.get("grid_square"):
        issues.append({
            "severity": "warn",
            "instance": "all",
            "message": "station.grid_square is empty",
        })

    decoder = paths.get("decoder", "/usr/local/bin/decode_ft8")
    if not shutil.which(decoder) and not Path(decoder).is_file():
        issues.append({
            "severity": "warn",
            "instance": "all",
            "message": f"decoder not found: {decoder}",
        })

    radiod_blocks = config.get("radiod", [])
    if isinstance(radiod_blocks, dict):
        radiod_blocks = [radiod_blocks]
    if not radiod_blocks:
        issues.append({
            "severity": "fail",
            "instance": "all",
            "message": "no [[radiod]] blocks configured",
        })

    for block in radiod_blocks:
        rid = block.get("id", "<unnamed>")
        if not block.get("radiod_status"):
            env_key = f"RADIOD_{rid.upper().replace('-', '_')}_STATUS"
            if not os.environ.get(env_key):
                issues.append({
                    "severity": "fail",
                    "instance": rid,
                    "message": f"radiod_status not set and {env_key} not in environment",
                })

        ft8 = get_freqs(block, "ft8")
        ft4 = get_freqs(block, "ft4")
        if not ft8 and not ft4:
            issues.append({
                "severity": "warn",
                "instance": rid,
                "message": "no FT4 or FT8 frequencies configured",
            })

        # §12.2 (v0.4): SSRC uniqueness. Duplicate
        # (freq, preset, sample_rate, encoding) tuples collide on
        # SSRC; MultiStream's slot dict silently overwrites.
        seen: dict[tuple, str] = {}
        for mode in ("ft8", "ft4"):
            params = get_mode_params(block, mode)
            for hz in get_freqs(block, mode):
                key = (int(hz), params["preset"], params["sample_rate"], params["encoding"])
                if key in seen:
                    issues.append({
                        "severity": "fail",
                        "instance": rid,
                        "message": (
                            f"SSRC collision: {mode.upper()} {hz} Hz "
                            f"duplicates {seen[key]} "
                            f"(preset={params['preset']}, "
                            f"rate={params['sample_rate']}, "
                            f"enc={params['encoding']}) — "
                            f"ka9q-python will silently drop one"
                        ),
                    })
                else:
                    seen[key] = f"{mode.upper()} {hz} Hz"

    return issues
