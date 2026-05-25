# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**psk-recorder** is a Python client that receives FT4 and FT8 audio
streams from one or more ka9q-radio `radiod` instances via `ka9q-python`,
decodes spots using `decode_ft8`, and uploads them to pskreporter.info.
It is part of the HamSCI sigmond suite — see
`/opt/git/sigmond/sigmond/CLAUDE.md` (orchestrator) and
`/opt/git/sigmond/CLAUDE.md` (umbrella) for cross-repo context.

This client replaces the ka9q-radio native shell pipeline (`ft8-record`
+ `ft8-decode` + `pskreporter@`) with a coordinated daemon that
implements the HamSCI client contract.

## Authors

- Michael Hauan (AC0G, GitHub: mijahauan)
- Repo: https://github.com/mijahauan/psk-recorder

## Quick reference

```bash
# Development — uv is canonical; creates .venv/ and uses uv.lock
uv sync --extra dev
uv run pytest tests/ -v
uv run pytest tests/test_contract.py -v          # one file
uv run pytest -k authority -v                    # by keyword
uv run pytest tests/test_slot.py::SlotWorkerTests::test_X   # one test

# Run-from-source without install:
PYTHONPATH=src python3 -m pytest tests/ -v
PYTHONPATH=src python3 -m psk_recorder inventory --json \
    --config config/psk-recorder-config.toml.template

# Production install / upgrade (uses sigmond's shared _ensure_uv helper)
sudo ./scripts/install.sh           # first-run: user, venv (via uv), config, systemd
sudo ./scripts/deploy.sh            # ongoing: refresh install + restart instances
sudo ./scripts/deploy.sh --pull     # git pull then deploy

# CLI surface (current — verify against `psk-recorder --help`)
psk-recorder inventory --json       # per-instance resource view
psk-recorder validate --json        # config validation
psk-recorder version --json         # version + git sha
psk-recorder status                 # health check
psk-recorder daemon --config <path> --radiod-id <id>
psk-recorder config init|edit|show|apply
psk-recorder env                    # read/write /etc/psk-recorder/env/<instance>.env
```

The test suite is large (~222 tests collected). When iterating, target
the affected file with `pytest tests/test_<area>.py -v` rather than the
whole suite.

## Architecture

```
radiod (ka9q-radio)
  │  RadiodControl.ensure_channel() via ka9q-python
  │  preset=usb, samprate=12000, encoding=s16be
  ▼
RTP multicast ──► psk-recorder daemon (one per radiod)
                    │
                    ├─ per-channel: RingBuffer → SlotWorker
                    │    └─ 15s (FT8) or 7.5s (FT4) cadence
                    │    └─ write WAV → fork decode_ft8 → append spot log
                    │
                    └─ HsPskReporterUploader (one per daemon)
                         ├─ pulls from sigmond's SQLite sink
                         │  (/var/lib/sigmond/sink.db, filtered by radiod_id)
                         │  with a FileTreeSource fallback for sinkless hosts
                         ├─ ships via hs-uploader Pipeline + PskReporterTcp
                         │  transport (owns the TCP socket end-to-end —
                         │  no external pskreporter subprocess)
                         └─ watermark + retry state in
                            /var/lib/hs-uploader/watermarks.db
```

Pump cadence is 30 s (`hs_uploader_shim.PUMP_INTERVAL_SEC`), matching
the FT4/FT8 slot rate and the legacy `PSKREPORTER_INTERVAL`.

Two delivery modes selected by `PSK_DELIVERY_MODE`:

- **direct** — client POSTs spots directly to pskreporter.info; cross-rx
  dedup applies in the local pipeline.
- **server-forwarded** — spots are tagged `forward_to_pskreporter=True`
  in the local sink so a downstream `pskreporter_forwarder` service
  (Phase D, gw1-elected) does the upload.

## Project structure

```
src/psk_recorder/
  cli.py              # argparse entry point + stdout-cleanliness guard
  config.py           # TOML loader, radiod block resolution, defaults
  contract.py         # inventory/validate JSON builders (contract v0.7)
  configurator.py     # `config init`/`edit` — uses sigmond.wizard_dispatch
                      #   (CONTRACT v0.5 §14); whiptail wizard with stdin fallback
  version.py          # GIT_INFO dict for provenance
  core/
    recorder.py            # PskRecorder: orchestrates one radiod's channels
    receiver_manager.py    # per-channel receiver lifecycle
    stream.py              # ChannelStream: RadiodStream + ring + SlotWorker
    ring.py                # process-local deque ring buffer
    slot.py                # SlotWorker: cadence math, WAV write, decoder fork
    cycle_batcher.py       # FT cycle batching for the slot loop
    authority_reader.py    # §18 timing-authority snapshot subscriber
    wav.py                 # minimal WAV writer (s16be mono)
    hs_uploader_shim.py    # HsPskReporterUploader — sole upload path
    ch_tailer.py           # spot-tail path into sigmond's SQLite sink
tests/                # ~222 collected tests; fixtures in tests/fixtures/
config/               # psk-recorder-config.toml.template
docs/                 # ARCHITECTURE.md, CONFIG.md, INSTALL.md, OPERATIONS.md, SIGMOND-CONTRACT.md
scripts/
  install.sh          # first-run bootstrap (uv-based via sigmond's _ensure_uv)
  deploy.sh           # editable-install refresh + restart
  config-wizard.sh    # whiptail wizard backing configurator.py
systemd/              # psk-recorder@.service template unit
deploy.toml           # sigmond client manifest
```

When a file appears here but isn't covered above, read its module
docstring — the codebase is well-documented at module level.

## Key design decisions

- **Templated systemd unit** — `psk-recorder@<radiod_id>.service`, one
  instance per radiod. Multiple radiods = multiple instances, started
  and stopped independently.
- **ka9q-python owns multicast destination** — psk-recorder never
  passes `destination=` to `ensure_channel()`; reads the resolved
  address from `ChannelInfo` for the inventory payload (contract §7).
- **radiod identified by mDNS hostname** (`bee1-status.local`), never
  IP.
- **Process-local ring buffer** — `collections.deque` behind a
  `threading.Lock`, not SysV IPC. No cross-process consumers.
- **Subprocess only for decoding** — shells out to `decode_ft8`. The
  uploader is now in-process via hs-uploader (the legacy `pskreporter`
  subprocess was removed during the ClickHouse-removal sweep).
- **WAV spool deleted after decode** — `paths.keep_wav = false`
  default.
- **PSWS station/instrument IDs are optional** — psk-recorder doesn't
  require them; optional fields exist for operators who also run PSWS.

## Client contract (v0.7)

psk-recorder implements the HamSCI client contract at version 0.7
(authoritative source: `/opt/git/sigmond/sigmond/docs/CLIENT-CONTRACT.md`).
`contract.py` carries `CONTRACT_VERSION = "0.7"`; the `deploy.toml`
manifest currently declares `0.6` and may lag behind the code.

Sections psk-recorder implements:

- **§1 / §2 / §3 / §4 / §5** — native TOML config, radiod-id binding,
  self-describe CLI (`inventory`/`validate`/`version` `--json`),
  templated systemd, `deploy.toml` manifest.
- **§6 / §7** — uses ka9q-python; data destination read from
  `ChannelInfo`, never client-specified.
- **§8** — `RADIOD_<id>_CHAIN_DELAY_NS` read from env on startup.
- **§10 / §11** — `log_paths` in inventory; `PSK_RECORDER_LOG_LEVEL`
  / `CLIENT_LOG_LEVEL` honored on startup and SIGHUP.
- **§12** — validate hardening (SSRC uniqueness, paths, etc.).
- **§13** — control surface (status/config show/apply).
- **§14** — configuration interview via `configurator.py` (whiptail
  wizard + legacy stdin fallback); honors §14.3 env bag
  (`STATION_*`, `SIGMOND_INSTANCE`, `SIGMOND_RADIOD_STATUS`).
- **§17** — output sinks in inventory (SQLite sink + per-mode log
  files, both `kind = "file"`).
- **§18 (new in v0.7)** — timing-authority subscriber via
  `authority_reader.py`; inventory carries
  `timing_authority_applied` per instance (null = RTP-default mode,
  populated = authority-corrected with source/tier/σ/age).

## External dependencies (not pip-installable)

- **decode_ft8** — from https://github.com/ka9q/ft8_lib. Built and
  installed at `/usr/local/bin/decode_ft8`. Invoked as
  `decode_ft8 -f <freq_mhz> [-4] <wavfile>` (`-4` for FT4 mode).
- **ka9q-radio radiod** — the RTP source. psk-recorder talks to it
  exclusively via `ka9q-python`.

The legacy `pskreporter` binary (`ftlib-pskreporter`) is **no longer
on the runtime upload path** — `HsPskReporterUploader` owns the
PSKReporter TCP socket directly via `PskReporterTcp`. `contract.py`'s
validate step still checks for the binary at `/usr/local/bin/pskreporter`;
this check is legacy and may be retired.

## Python sibling dependencies

`pyproject.toml` `[tool.uv.sources]` resolves three libraries from
sibling editable checkouts under `/opt/git/sigmond/`:

- `ka9q-python` (also declared `>=3.14.0` for PyPI consumers)
- `callhash`
- `hs-uploader`

A `git pull` of any sibling propagates to this consumer's venv with no
reinstall — see "Fleet upgrade pattern" in
`/opt/git/sigmond/sigmond/CLAUDE.md` for staleness / restart rules.

## Config schema

```toml
[station]
callsign    = "AC0G"
grid_square = "EM38ww40pk"

[paths]
spool_dir   = "/var/lib/psk-recorder"
log_dir     = "/var/log/psk-recorder"
decoder     = "/usr/local/bin/decode_ft8"
pskreporter = "/usr/local/bin/pskreporter"   # legacy; see "External dependencies"
keep_wav    = false

[[radiod]]
id            = "bee1-rx888"
radiod_status = "bee1-status.local"          # mDNS, never IP

[radiod.ft8]
sample_rate = 12000
preset      = "usb"
encoding    = "s16be"
freqs_hz    = [14074000, 7074000, ...]

[radiod.ft4]
sample_rate = 12000
preset      = "usb"
encoding    = "s16be"
freqs_hz    = [14080000, 7047500, ...]
```

## Production paths

- Config: `/etc/psk-recorder/psk-recorder-config.toml`
- Per-instance env: `/etc/psk-recorder/env/<instance>.env`
- Spool: `/var/lib/psk-recorder/<radiod_id>/{ft8,ft4}/YYMMDD_HHMMSS.wav`
- Spot logs: `/var/log/psk-recorder/<radiod_id>-{ft8,ft4}.log`
- Process log: systemd journal (`journalctl -u psk-recorder@<radiod_id>`)
- Uploader state: `/var/lib/hs-uploader/watermarks.db`
- Sigmond local sink: `/var/lib/sigmond/sink.db`
- Venv: `/opt/psk-recorder/venv`
- Source: `/opt/git/sigmond/psk-recorder` (editable install)
- Service user: `pskrec:pskrec`

## Further reading

- `docs/ARCHITECTURE.md` — deeper internals than this file
- `docs/CONFIG.md` — config-schema reference
- `docs/INSTALL.md` — installation walkthrough
- `docs/OPERATIONS.md` — running / monitoring guidance
- `docs/SIGMOND-CONTRACT.md` — contract-mapping notes specific to this repo
- `/opt/git/sigmond/sigmond/docs/CLIENT-CONTRACT.md` — the authoritative
  v0.7 contract spec
