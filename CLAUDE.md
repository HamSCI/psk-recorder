# CLAUDE.md — psk-recorder Development Briefing

## What this project is

**psk-recorder** is a Python client that receives FT4 and FT8 audio
streams from one or more ka9q-radio `radiod` instances via `ka9q-python`,
decodes spots using `decode_ft8`, and uploads them to pskreporter.info
via Philip Gladstone's `pskreporter` binary.

It replaces the ka9q-radio native shell pipeline (`ft8-record` +
`ft8-decode` + `pskreporter@`) with a coordinated Python client that
follows the HamSCI sigmond client contract.

## Authors

- Michael Hauan (AC0G, GitHub: mijahauan)
- Repo: https://github.com/mijahauan/psk-recorder

## Quick Reference

```bash
# Development (uv is the standard; creates .venv/ and locks via uv.lock)
uv sync --extra dev
uv run pytest tests/ -v
uv run psk-recorder inventory --json --config config/psk-recorder-config.toml.template
uv run psk-recorder validate --json --config tests/fixtures/test-config.toml

# pip fallback / run-from-source (no install):
PYTHONPATH=src python3 -m pytest tests/ -v
PYTHONPATH=src python3 -m psk_recorder inventory --json --config config/psk-recorder-config.toml.template

# Production install (Pattern A editable install)
sudo ./scripts/install.sh           # first-run: user, venv, config, systemd
sudo ./scripts/deploy.sh            # ongoing: pip install -e, restart instances
sudo ./scripts/deploy.sh --pull     # git pull then deploy

# CLI
psk-recorder inventory --json       # contract v0.3 resource view
psk-recorder validate --json        # config validation
psk-recorder version --json         # version + git sha
psk-recorder daemon --config /etc/psk-recorder/psk-recorder-config.toml --radiod-id bee1-rx888
```

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
                    └─ per-mode: PskReporterUploader
                         └─ supervises long-running pskreporter subprocess
```

## Project Structure

```
src/psk_recorder/
  cli.py              # CLI entry point, argparse, stdout-cleanliness guard
  config.py           # TOML loader, radiod block resolution, defaults
  contract.py         # inventory/validate JSON builders (contract v0.3)
  version.py          # GIT_INFO dict for provenance
  core/
    recorder.py       # PskRecorder: orchestrates one radiod's channels
    stream.py         # ChannelStream: RadiodStream + ring + SlotWorker
    ring.py           # Process-local deque ring buffer
    slot.py           # SlotWorker: cadence math, WAV write, decoder fork
    wav.py            # Minimal WAV writer (s16be mono)
    uploader.py       # PskReporterUploader: pskreporter subprocess mgmt
tests/
  test_contract.py    # 21 tests: stdout cleanliness, v0.3 fields, config
  fixtures/
    test-config.toml  # Test config with 2 FT8 + 2 FT4 frequencies
config/
  psk-recorder-config.toml.template
systemd/
  psk-recorder@.service   # Template unit; %i = radiod_id
scripts/
  install.sh          # First-run bootstrap (Pattern A)
  deploy.sh           # Editable-install refresh
deploy.toml           # Sigmond deploy manifest (contract v0.3)
```

## Key Design Decisions

- **Templated systemd unit** `psk-recorder@<radiod_id>.service` — one
  instance per radiod, not one per host.  Multiple radiods = multiple
  instances, started/stopped independently.
- **ka9q-python owns multicast destination** — psk-recorder never passes
  `destination=` to `ensure_channel()`.  Reads resolved address from
  `ChannelInfo` for inventory.
- **radiod identified by mDNS hostname** (`bee1-status.local`), never IP.
- **PSWS station/instrument IDs optional** — psk-recorder doesn't require
  them; optional fields exist for operators who also run PSWS.
- **Process-local ring buffer** — `collections.deque` behind a
  `threading.Lock`, not SysV IPC.  No cross-process consumers.
- **Subprocess-based decoder and uploader** — shells out to `decode_ft8`
  and `pskreporter`, does not reimplement them.
- **WAV spool deleted after decode** — `paths.keep_wav = false` default.

## Client Contract (v0.3)

psk-recorder implements the HamSCI client contract v0.3 as defined in
`sigmond/docs/CLIENT-CONTRACT.md`.  Key surfaces:

- `psk-recorder inventory --json` — per-instance resource view
- `psk-recorder validate --json` — config validation
- `deploy.toml` — build/install manifest
- `EnvironmentFile=-/etc/sigmond/coordination.env` in the systemd unit
- §7: data destination read from ka9q-python, not client-specified
- §8: `RADIOD_<id>_CHAIN_DELAY_NS` read from env on startup
- §10: `log_paths` in inventory output
- §11: `PSK_RECORDER_LOG_LEVEL` / `CLIENT_LOG_LEVEL` honored on
  startup and SIGHUP

## External Dependencies (not pip-installable)

- **decode_ft8** — from https://github.com/ka9q/ft8_lib.  Must be built
  and installed to `/usr/local/bin/decode_ft8`.  Invoked as:
  `decode_ft8 -f <freq_mhz> [-4] <wavfile>`  (`-4` for FT4 mode).
- **pskreporter** — from https://github.com/pjsg/ftlib-pskreporter.
  Must be built and installed to `/usr/local/bin/pskreporter`.
- **ka9q-radio radiod** — the RTP source.  psk-recorder talks to it
  exclusively via ka9q-python.

## Config Schema

```toml
[station]
callsign    = "AC0G"
grid_square = "EM38ww40pk"

[paths]
spool_dir   = "/var/lib/psk-recorder"
log_dir     = "/var/log/psk-recorder"
decoder     = "/usr/local/bin/decode_ft8"
pskreporter = "/usr/local/bin/pskreporter"
keep_wav    = false

[[radiod]]
id            = "bee1-rx888"
radiod_status = "bee1-status.local"    # mDNS, never IP

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

## Production Paths

- Config: `/etc/psk-recorder/psk-recorder-config.toml`
- Spool: `/var/lib/psk-recorder/<radiod_id>/{ft8,ft4}/YYMMDD_HHMMSS.wav`
- Spot logs: `/var/log/psk-recorder/<radiod_id>-{ft8,ft4}.log`
- Process log: systemd journal (`journalctl -u psk-recorder@<radiod_id>`)
- Venv: `/opt/psk-recorder/venv`
- Source: `/opt/git/sigmond/psk-recorder` (editable install)
- Service user: `pskrec:pskrec`

## Running Tests

```bash
uv sync --extra dev
uv run pytest tests/ -v
```

Tests use subprocess invocations against the fixture config and verify
JSON contract compliance end-to-end.

1. Don’t assume. Don’t hide confusion. Surface tradeoffs.

2. Minimum code that solves the problem. Nothing speculative.

3. Touch only what you must. Clean up only your own mess.

4. Define success criteria. Loop until verified.
