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
  ├─ per-channel: ring buffer → 15s/7.5s slot WAV → fork decoder
  │                                                  └─ jt9 (default) or
  │                                                     decode_ft8 (fallback)
  ├─ per-mode log file (WSJT-X-canonical or decode_ft8 native)
  ├─ per-mode: pskreporter-sender (UDP or TCP to pskreporter.info)
  └─ per-mode: ChTailer → sigmond.hamsci_sink.Writer → psk.spots
```

**v0.3 (current)** swaps the default decoder from `decode_ft8` to
WSJT-X's `jt9`.  jt9 reports calibrated dB SNR (vs. ft8_lib's opaque
"score") and surfaces the spectral_width metric the FT4/FT8 protocol
carries.  Both decoders coexist as parallel backends: rows tag
themselves via `decoder_kind` in `psk.spots`, ChTailer auto-detects
the line format, and operators can switch via `paths.decoder_kind` in
the config.

One `psk-recorder@<radiod_id>.service` instance per radiod. Each
instance handles all configured FT8 and FT4 frequencies on that
radiod.

## Quickstart

External binaries must be present first:
- **`jt9`** — bundled per-arch under `bin/decoders/jt9-{x86,arm64,arm32}-v27`
  in this repo.  `scripts/install.sh` copies the lot to
  `/opt/psk-recorder/bin/decoders/` and arch-symlinks the active host's
  binary to `jt9`.  Avoids pulling in the full `wsjtx` GUI package
  (~150 MB).  Runtime libs: `libqt5core5a`, `libfftw3-single3`,
  `libgfortran5` — listed in [`deploy.toml`][deploytoml] as apt deps.
- `decode_ft8` from [ka9q/ft8_lib][ft8_lib] → `/usr/local/bin/decode_ft8` —
  optional fallback when `decoder_kind = "decode_ft8"` in config.
- `pskreporter-sender` from [pjsg/ftlib-pskreporter][ftlib] → `/usr/local/bin/pskreporter-sender`
- A working `radiod@<id>.service` from [ka9q/ka9q-radio][ka9q]

Then:

```bash
git clone https://github.com/mijahauan/psk-recorder /opt/git/sigmond/psk-recorder
sudo /opt/git/sigmond/psk-recorder/scripts/install.sh   # creates user, venv, config, units
sudoedit /etc/psk-recorder/psk-recorder-config.toml   # set callsign, grid, freqs, [[radiod]]
sudo systemctl start psk-recorder@<radiod_id>
journalctl -fu psk-recorder@<radiod_id>
```

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
(15s) or FT4 (7.5s) cadence, write a WAV per slot, fork the decoder
(`jt9` by default; `decode_ft8` as opt-out fallback), append spots to
per-mode log files in the decoder's native format (WSJT-X-canonical
for jt9, decode_ft8-native for the fallback), supervise a long-running
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
[wsjtx]: https://wsjt.sourceforge.io/wsjtx.html
[deploytoml]: deploy.toml
[contract]: https://github.com/mijahauan/sigmond/blob/main/docs/CLIENT-CONTRACT.md
