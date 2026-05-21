"""psk-recorder CLI entry point.

Subcommands:
    inventory   — contract v0.3 JSON inventory
    validate    — contract v0.3 config validation
    version     — version + git block
    daemon      — long-running recorder (Phase 1)
    status      — health check (Phase 1)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path


def _resolve_log_level() -> int:
    """Resolve log level per contract v0.3 §11 precedence.

    1. --log-level CLI flag (handled by caller, not here)
    2. PSK_RECORDER_LOG_LEVEL env var
    3. CLIENT_LOG_LEVEL env var
    4. Default: INFO
    """
    for env_key in ("PSK_RECORDER_LOG_LEVEL", "CLIENT_LOG_LEVEL"):
        val = os.environ.get(env_key, "").upper().strip()
        if val and hasattr(logging, val):
            return getattr(logging, val)
    return logging.INFO


def _install_sighup_handler() -> None:
    """Re-read log level from env on SIGHUP (contract v0.3 §11)."""
    def _on_sighup(signum, frame):
        level = _resolve_log_level()
        logging.getLogger().setLevel(level)
        logging.getLogger(__name__).info(
            "SIGHUP: log level set to %s", logging.getLevelName(level)
        )
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_sighup)


def main():
    # "Quiet" surfaces emit clean stdout (JSON or shell-parseable) and
    # must not get the "psk-recorder starting" log line on top.
    # config show / config apply join inventory / validate / version
    # here because the whiptail wizard parses their stdout.
    _contract_quiet = any(
        arg in ("inventory", "validate", "version")
        for arg in sys.argv[1:3]
    ) or (
        len(sys.argv) >= 3 and sys.argv[1] == "config"
        and sys.argv[2] in ("show", "apply")
    )

    root = logging.getLogger()
    if _contract_quiet:
        root.setLevel(logging.WARNING)
    else:
        root.setLevel(_resolve_log_level())

    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        # Include ISO-8601 timestamp so off-line log scrapers (e.g.
        # sigmond's decode-health collector) can anchor events in
        # time.  systemd's StandardOutput=append:<file> writes raw
        # stdout/stderr to the file with no timestamp prefix; without
        # %(asctime)s every line is a timeless string.
        handler.setFormatter(
            logging.Formatter(
                fmt='%(asctime)s.%(msecs)03dZ %(levelname)s:%(name)s:%(message)s',
                datefmt='%Y-%m-%dT%H:%M:%S',
            )
        )
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            if _contract_quiet:
                handler.setLevel(logging.WARNING)

    if not _contract_quiet:
        logging.info("psk-recorder starting")

    parser = argparse.ArgumentParser(
        prog="psk-recorder",
        description="FT4/FT8 spot recorder and PSK Reporter uploader",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Shared arguments added to every subparser
    def _add_common(sub):
        sub.add_argument(
            "--config", type=Path, default=None,
            help="Path to psk-recorder-config.toml",
        )
        sub.add_argument(
            "--log-level", default=None,
            help="Override log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        )

    sub_inv = subparsers.add_parser("inventory", help="Contract v0.3 inventory")
    sub_inv.add_argument("--json", action="store_true", default=True)
    _add_common(sub_inv)

    sub_val = subparsers.add_parser("validate", help="Contract v0.3 validation")
    sub_val.add_argument("--json", action="store_true", default=True)
    _add_common(sub_val)

    sub_ver = subparsers.add_parser("version", help="Version info")
    sub_ver.add_argument("--json", action="store_true", default=True)
    _add_common(sub_ver)

    sub_daemon = subparsers.add_parser("daemon", help="Run recorder daemon")
    sub_daemon.add_argument(
        "--radiod-id", default=None,
        help="ID of the [[radiod]] block to use",
    )
    _add_common(sub_daemon)

    sub_status = subparsers.add_parser("status", help="Health check")
    _add_common(sub_status)

    # Configuration interview (CONTRACT-v0.5 §14).
    sub_cfg = subparsers.add_parser(
        "config",
        help="initialize or edit psk-recorder configuration",
    )
    cfg_sub = sub_cfg.add_subparsers(dest="config_command")

    sub_init = cfg_sub.add_parser(
        "init", help="write a fresh psk-recorder-config.toml from template")
    sub_init.add_argument("--reconfig", action="store_true",
                          help="overwrite existing config")
    sub_init.add_argument("--non-interactive", action="store_true",
                          help="use env-var defaults, do not prompt")
    _add_common(sub_init)

    sub_edit = cfg_sub.add_parser(
        "edit", help="review and update an existing config")
    sub_edit.add_argument("--non-interactive", action="store_true",
                          help="show current values, do not prompt")
    sub_edit.add_argument("--radiod-id", default=None,
                          help="focus edits on a specific [[radiod]] block")
    _add_common(sub_edit)

    # `config show` / `config apply` exist for the whiptail wizard
    # (scripts/config-wizard.sh) and any other tooling that wants to
    # round-trip the config as JSON through the same validator the
    # daemon uses.
    from psk_recorder import configurator as _cfg
    _cfg.add_show_apply_subparsers(cfg_sub, common=_add_common)

    args = parser.parse_args()

    if args.log_level and not _contract_quiet:
        level_name = args.log_level.upper()
        if hasattr(logging, level_name):
            root.setLevel(getattr(logging, level_name))

    if args.command == "inventory":
        _handle_inventory(args)
    elif args.command == "validate":
        _handle_validate(args)
    elif args.command == "version":
        _handle_version(args)
    elif args.command == "daemon":
        _handle_daemon(args)
    elif args.command == "status":
        _handle_status(args)
    elif args.command == "config":
        _handle_config(args)
    else:
        parser.print_help()
        sys.exit(1)


def _handle_config(args):
    from psk_recorder import configurator

    sub = getattr(args, "config_command", None)
    if sub == "init":
        sys.exit(configurator.cmd_config_init(args))
    if sub == "edit":
        sys.exit(configurator.cmd_config_edit(args))
    if sub == "show":
        sys.exit(configurator.cmd_config_show(args))
    if sub == "apply":
        sys.exit(configurator.cmd_config_apply(args))
    print("usage: psk-recorder config {init|edit|show|apply} [...]")
    sys.exit(2)


def _handle_inventory(args):
    from psk_recorder.config import DEFAULT_CONFIG_PATH, load_config
    from psk_recorder.contract import build_inventory

    config_path = args.config or Path(
        os.environ.get("PSK_RECORDER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        payload = {
            "client": "psk-recorder",
            "version": "0.1.0",
            "contract_version": "0.4",
            "config_path": str(config_path),
            "instances": [],
            "issues": [
                {
                    "severity": "fail",
                    "instance": "all",
                    "message": f"config not found: {config_path}",
                }
            ],
        }
        print(json.dumps(payload, indent=2))
        return

    payload = build_inventory(config, config_path)
    print(json.dumps(payload, indent=2))


def _handle_validate(args):
    from psk_recorder.config import DEFAULT_CONFIG_PATH, load_config
    from psk_recorder.contract import build_validate

    config_path = args.config or Path(
        os.environ.get("PSK_RECORDER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        payload = {
            "ok": False,
            "config_path": str(config_path),
            "issues": [
                {
                    "severity": "fail",
                    "instance": "all",
                    "message": f"config not found: {config_path}",
                }
            ],
        }
        print(json.dumps(payload, indent=2))
        sys.exit(1)
        return

    payload = build_validate(config, config_path)
    print(json.dumps(payload, indent=2))
    if not payload["ok"]:
        sys.exit(1)


def _handle_version(args):
    from psk_recorder import __version__
    from psk_recorder.version import GIT_INFO

    payload = {
        "client": "psk-recorder",
        "version": __version__,
    }
    if GIT_INFO:
        payload["git"] = GIT_INFO
    print(json.dumps(payload, indent=2))


def _handle_daemon(args):
    _install_sighup_handler()
    logger = logging.getLogger("psk_recorder.daemon")

    from psk_recorder.config import (
        DEFAULT_CONFIG_PATH,
        ensure_sources,
        load_config,
        resolve_radiod_block,
    )
    from psk_recorder.core.recorder import PskRecorder

    config_path = args.config or Path(
        os.environ.get("PSK_RECORDER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    config = load_config(config_path)

    if args.radiod_id is not None:
        # Legacy single-source mode — operator explicitly selected one
        # block; honor it exactly even if the config has more.  Used
        # by ``psk-recorder@<radiod-id>.service`` template units.
        radiod_block = resolve_radiod_block(config, args.radiod_id)
        blocks = [radiod_block]
        logger.info(
            "Starting psk-recorder daemon for radiod %s "
            "(config=%s, single-source mode)",
            radiod_block.get("id", "default"), config_path,
        )
    else:
        # Multi-source mode — drive every [[radiod]] block in the
        # config from a single process.  Mirrors wspr-recorder's
        # single-process / multi-source pattern.
        sources = ensure_sources(config)
        if not sources:
            raise SystemExit(
                f"No usable [[radiod]] blocks in {config_path}",
            )
        blocks = [s["radiod_block"] for s in sources]
        logger.info(
            "Starting psk-recorder daemon for %d radiod source(s): %s "
            "(config=%s)",
            len(blocks),
            ", ".join(s["radiod_id"] for s in sources),
            config_path,
        )

    recorder = PskRecorder(config, blocks)
    recorder.run()


def _handle_status(args):
    print("psk-recorder: not running (Phase 1 not yet implemented)")
    sys.exit(2)


if __name__ == "__main__":
    main()
