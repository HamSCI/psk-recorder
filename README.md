# psk-recorder

FT4/FT8 spot recorder and PSK Reporter uploader for [ka9q-radio][ka9q].
Replaces the native `ft8-record` / `ft8-decode` / `pskreporter@` shell
pipeline with a coordinated Python client that follows the HamSCI
sigmond [client contract][contract] (v0.6).

```
radiod (ka9q-radio)
  │   RTP multicast, one stream per (band, mode) channel
  ▼
psk-recorder daemon (one per radiod)
  ├─ per-channel: ring buffer → 15s/7.5s slot WAV → fork decode_ft8
  ├─ per-mode log file (decode_ft8 native format)
  ├─ per-mode: pskreporter-sender (UDP or TCP to pskreporter.info)
  └─ per-mode: ChTailer → sigmond.hamsci_sink.Writer → psk.spots
```

psk-recorder decodes with ka9q/ft8_lib's `decode_ft8`.  Rows tag
themselves via `decoder_kind` in `psk.spots`, and ChTailer parses the
decoder output into `psk.spots` rows.

One `psk-recorder@<radiod_id>.service` instance per radiod. Each
instance handles all configured FT8 and FT4 frequencies on that
radiod.

## Quickstart

External binaries must be present first:
- `decode_ft8` from [ka9q/ft8_lib][ft8_lib] → `/usr/local/bin/decode_ft8` —
  psk-recorder's FT4/FT8 decoder.
- `pskreporter-sender` from [pjsg/ftlib-pskreporter][ftlib] → `/usr/local/bin/pskreporter-sender`
- A working `radiod@<id>.service` from [ka9q/ka9q-radio][ka9q]

Then:

```bash
git clone https://github.com/mijahauan/psk-recorder /opt/git/sigmond/psk-recorder
sudo /opt/git/sigmond/psk-recorder/scripts/install.sh   # creates user, venv, config, units
sudo psk-recorder config edit                           # interactive wizard (whiptail) -- see below
sudo systemctl start psk-recorder@<radiod_id>
journalctl -fu psk-recorder@<radiod_id>
```

### Configuration

The daemon reads `/etc/psk-recorder/psk-recorder-config.toml`.  Three
ways to populate it:

1. **Interactive whiptail wizard (default)** — when stdout is a TTY
   and `whiptail` is installed, `psk-recorder config init` (first
   time) and `psk-recorder config edit` (subsequent) launch a
   menu-driven wizard with sections for Station, Paths, Processing,
   plus an "Edit raw TOML" item that drops to `$EDITOR` for the
   `[[radiod]]` arrays-of-tables and per-band `freqs_hz` lists that
   whiptail can't naturally express.  Inside a section, Cancel drops
   back to the menu — effective "back" navigation.  Per-key help
   lives in `config/help.toml`; pre-fills come from
   `/etc/sigmond/coordination.env` (`STATION_CALL`, `STATION_GRID`).

   Same UI pattern mag-recorder uses; see that repo's README for the
   shape.

2. **Headless / scripted**: `psk-recorder config init --non-interactive`
   renders the template with `STATION_CALL` / `SIGMOND_INSTANCE` /
   `SIGMOND_RADIOD_STATUS` env-bag substitutions, no prompts.

3. **Hand-edit**: `sudoedit /etc/psk-recorder/psk-recorder-config.toml`.
   Operator who values inline comments / formatting should pick this
   path; the wizard's `config apply` rewrites the TOML cleanly and
   doesn't preserve them.

The two JSON entry points the wizard uses are stable surfaces for
sigmond and other tooling:

```bash
psk-recorder config show --json [--defaults]   # → stdout JSON
psk-recorder config apply --json -             # ← stdin JSON, validated, atomic write
```

`config apply` writes only `[station]`, `[paths]`, `[processing]`.
`[[radiod]]` blocks pass through unchanged but cannot be set this way
(whiptail can't express array-of-tables, and `tomllib` can't preserve
comments across a round-trip).  Operators who need multi-radiod or
custom frequency lists use the "Edit raw TOML" menu item or
`sudoedit` directly.

For ongoing development on a checked-out repo:

```bash
sudo /opt/git/sigmond/psk-recorder/scripts/deploy.sh         # pip install -e + restart instances
sudo /opt/git/sigmond/psk-recorder/scripts/deploy.sh --pull  # git pull then deploy
```

For tests (no venv needed):

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — full install (deps, multi-radiod, paths, permissions)
- [docs/CONFIG.md](docs/CONFIG.md) — TOML schema reference (every section, every key)
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — running it: logs, monitoring, common failures
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — internals for contributors
- [docs/SIGMOND-CONTRACT.md](docs/SIGMOND-CONTRACT.md) — how psk-recorder satisfies the HamSCI client contract
- [CLAUDE.md](CLAUDE.md) — development briefing (workflow, conventions)

## What it does and does not

**Does:** receive RTP multicast from `radiod`, slot-align audio to FT8
(15s) or FT4 (7.5s) cadence, write a WAV per slot, fork `decode_ft8`,
append spots to per-mode log files in decode_ft8's native format,
supervise a long-running
`pskreporter-sender` per mode that tails those logs and uploads to
pskreporter.info, and stream parsed rows into `psk.spots` via
`sigmond.hamsci_sink.Writer` (sigmond's local SQLite sink by default).

**Does not:** reimplement the FT8/FT4 decoder, reimplement the
pskreporter protocol, or talk to `radiod` over anything but
[ka9q-python][ka9qpy]. Multicast destination addresses are *resolved
from* radiod, never specified by psk-recorder.

## License

MIT. See [LICENSE](LICENSE). Author: Michael Hauan, AC0G.

[ka9q]: https://github.com/ka9q/ka9q-radio
[ka9qpy]: https://github.com/mijahauan/ka9q-python
[ft8_lib]: https://github.com/ka9q/ft8_lib
[ftlib]: https://github.com/pjsg/ftlib-pskreporter
[contract]: https://github.com/mijahauan/sigmond/blob/main/docs/CLIENT-CONTRACT.md
