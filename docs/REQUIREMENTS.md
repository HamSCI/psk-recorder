# psk-recorder — Requirements Specification

**Status:** v0.1 baseline (retroactive). **Owner:** Michael Hauan (AC0G).
**Last reconciled against code:** psk-recorder `0.4.0` / deploy `0.4.0`,
contract `0.8`, git `8179d4d` (2026-06-25).
**Prefix:** `PSK`.

> Application of [sigmond/docs/REQUIREMENTS-TEMPLATE.md](https://github.com/HamSCI/sigmond/blob/main/docs/REQUIREMENTS-TEMPLATE.md)
> to a **Mature** component (cf. the mature hf-timestd and Early
> superdarn-sounder pilots). The expected picture is mostly `[CODE]✅` /
> `[DOC]✅` — a working recorder whose requirements were never written down —
> with a small tail of `[NEW]` gaps surfaced by the reconciliation. The
> sigmond↔component **interface** requirements are specified once in the
> [client contract](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md)
> (v0.8) and referenced — not restated — here (§8.3). Provenance tags:
> `[DOC]` documented · `[CODE]` implicit-in-code · `[NEW]` surfaced by this review.
> Status: ✅ implemented · 🟡 partial/unverified · ⬜ planned.

## 1. Context & problem statement

FT8 and FT4 are the dominant weak-signal digital modes on the amateur HF
bands; the global crowd-sourced reception network **PSKReporter** turns every
receiving station into a propagation sensor by aggregating who-heard-whom spots
into a near-real-time ionospheric-coverage map. A DASI2 / HamSCI station running
`radiod` (ka9q-radio) already has a wideband, GPSDO-disciplined receiver hearing
the whole HF spectrum at once; psk-recorder is the client that **turns that
receiver into an FT4/FT8 spot recorder and PSKReporter feeder** — decoding every
configured FT8 (15 s) and FT4 (7.5 s) channel on a radiod, emitting per-mode spot
logs, and shipping spots to pskreporter.info.

It exists to **replace the ka9q-radio native shell pipeline**
(`ft8-record` + `ft8-decode` + `pskreporter@`) with a single coordinated Python
daemon that conforms to the HamSCI sigmond client contract: self-describing
(`inventory`/`validate` JSON), templated per radiod, multi-instance-aware, and
writing its spots into the suite's shared SQLite sink (`psk.spots`) in addition
to feeding PSKReporter. That sink makes the same spot stream available to the
suite's uploaders and downstream science consumers without re-decoding.

Its defining design principle is **decode locally, deliver flexibly, never
re-implement the hard parts.** psk-recorder does not contain an FT8/FT4 decoder
(it forks ka9q/ft8_lib's `decode_ft8`) and does not contain the PSKReporter
protocol (it uses the `hs-uploader` library's `PskReporterTcp` transport). What
it owns is the orchestration: RTP capture, slot-cadence alignment, the spool, the
spot-log tailer into the shared sink, and a three-way delivery policy (direct /
server-merge / server-raw) that lets a multi-receiver merge fleet decide where
de-duplication and the actual upload happen.

## 2. Goals & objectives

- **Decode every configured FT8/FT4 channel on a radiod** at full slot cadence
  (15 s / 7.5 s) without inducing RX888 USB packet drops on radiod's cores.
- **Feed PSKReporter** reliably, with watermark/retry state that survives
  restarts and does not re-post history.
- **Populate the shared `psk.spots` sink** additively so the spot stream is
  reusable by suite uploaders and downstream consumers without re-decoding.
- **Support a multi-receiver merge fleet** — let an operator choose whether
  cross-receiver de-duplication and the pskreporter.info POST happen on the
  client (`direct`) or on the wsprdaemon server (`server-merge` / `server-raw`).
- **Recover compound-callsign hashes** to plaintext using the shared `callhash`
  table, so hashed `<...>` calls become usable spots.
- **Run usefully standalone** (radiod + this client) and as a fully
  contract-conformant suite client under sigmond.

## 3. Non-goals / out of scope

- **Implementing the FT8/FT4 decoder.** It forks `decode_ft8` (ka9q/ft8_lib);
  the decoder is an external binary, not bundled. (Owner: ka9q/ft8_lib.)
- **Implementing the PSKReporter protocol.** The `hs-uploader` library owns the
  `PskReporterTcp` transport and socket. (Owner: hs-uploader.)
- **Tuning / addressing radiod.** It consumes RTP from a pre-provisioned radiod
  via ka9q-python; multicast destination is *resolved from* radiod, never
  chosen by psk-recorder. (Owner: ka9q-radio / ka9q-python.)
- **Being a timing authority or applying chain-delay.** Spot timestamps are
  ~1 s slot-quantized, well outside the chain-delay regime; it is a §18
  *subscriber-aware but RTP-default* client, not a producer. (Owner: hf-timestd.)
- **The server-side merge endpoint.** `server-merge` requires a wsprdaemon-server
  forwarder that does cross-rx dedup + the PSKReporter POST; that endpoint is a
  plan, not built. (Owner: wsprdaemon-server — see
  [PHASE-D-SERVER-MERGE-ENDPOINT.md](https://github.com/HamSCI/sigmond/blob/main/docs/PHASE-D-SERVER-MERGE-ENDPOINT.md).)

## 4. Stakeholders & actors

Station operator · `radiod` (ka9q-radio, RTP IQ/audio source, required) ·
`ka9q-python` (the only path to radiod) · `decode_ft8` (ka9q/ft8_lib fork,
required decoder binary) · the shared `callhash` library (compound-call hash
resolution) · the `hs-uploader` library (`PskReporterTcp` transport + watermark
store) · **pskreporter.info** (upload target) · the shared sigmond SQLite sink
(`/var/lib/sigmond/sink.db`, `psk.spots`) and its downstream consumers ·
`hf-timestd` (§18 timing-authority producer, optional) · the wsprdaemon-server
PSK merge endpoint (optional, server-* pipelines) · sigmond (multi-instance
lifecycle, CPU affinity, identity, status).

## 5. Assumptions & constraints

- `PSK-C-001` `[DOC]` ✅ `radiod` (ka9q-radio) SHALL be present and reachable by
  its mDNS **status** multicast name; psk-recorder talks to it only via
  ka9q-python (no direct control protocol, no IP addressing).
- `PSK-C-002` `[DOC]` ✅ The external binary `decode_ft8` (ka9q/ft8_lib **fork**,
  branch `emit-numeric-callsign-hash`, pinned `37484ad`) SHALL be present at the
  configured `decoder` path; it is built per-host and is not pip-installable.
- `PSK-C-003` `[CODE]` ✅ The compound-call hash table SHALL be recoverable only
  with the **patched** decoder that emits `<NNNNNNN>` (numeric hash); the
  upstream decoder's opaque `<...>` is unresolvable (no regression, just no
  recovery).
- `PSK-C-004` `[DOC]` ✅ Exactly **one systemd instance per radiod**
  (`psk-recorder@<instance>.service`); each instance handles all configured FT8
  and FT4 frequencies on that radiod.
- `PSK-C-005` `[CODE]` ✅ Decoder children SHALL run off radiod's CPU cores (via
  sigmond `AFFINITY_UNITS`) — 19+ concurrent `decode_ft8` children otherwise
  pollute radiod's L3 and steal its cores, the classic USB-drop symptom.
- `PSK-C-006` `[CODE]` ✅ Python ≥3.10 (3.11 canonical); `ka9q-python`,
  `callhash`, `hs-uploader` resolved as **editable siblings** under
  `/opt/git/sigmond/` so a `git pull` propagates without reinstall.
- `PSK-C-007` `[CODE]` ✅ `sigmond` is **lazy-imported** with a no-op fallback;
  the same binary SHALL run unchanged with or without sigmond present.

## 6. Functional requirements

### 6.1 Acquisition & channel provisioning
- `PSK-F-001` `[DOC]` ✅ SHALL provision one ka9q-python channel per configured
  FT8/FT4 frequency on the bound radiod via `RadiodControl.ensure_channel`
  (`preset=usb`, `sample_rate=12000`, `encoding=s16be`), **never** passing
  `destination=`.
- `PSK-F-002` `[CODE]` ✅ SHALL maintain a process-local ring buffer
  (`collections.deque` behind a lock) per channel feeding a per-channel
  `SlotWorker`; no cross-process IPC ring.
- `PSK-F-003` `[CODE]` ✅ SHALL detect a per-channel **timing fault** (the
  sample-count projection diverging from radiod's GPS reference), re-anchor to
  keep decoding, and surface the fault loudly (rate-limited `#TIMINGFAULT` line +
  journal ERROR) — recovery is never silent.

### 6.2 Slotting & decode
- `PSK-F-010` `[DOC]` ✅ SHALL slot-align each channel's audio to the FT8 (15 s)
  or FT4 (7.5 s) cadence, write one mono `s16be` WAV per slot, and fork
  `decode_ft8` (`-f <freq_mhz>`, `-4` for FT4).
- `PSK-F-011` `[CODE]` ✅ SHALL bound each decode with a watchdog timeout and
  reap the child, so a hung decoder on a corrupt WAV cannot accumulate.
- `PSK-F-012` `[DOC]` ✅ SHALL delete the slot WAV after decode by default
  (`paths.keep_wav=false`).
- `PSK-F-013` `[DOC]` ✅ SHALL append each decoder's spots to a per-mode log file
  `<log_dir>/<radiod_id>-{ft8,ft4}.log` in `decode_ft8`'s native stdout format.
- `PSK-F-014` `[CODE]` 🟡 The decoder backend is selectable via `decoder_kind`,
  but `decode_ft8` is the **only** supported/installed backend; a `jt9` path is
  referenced historically but not wired. *(doc/code drift — `PSK-F-090`.)*

### 6.3 Spot parsing, hash resolution & the sink tailer
- `PSK-F-020` `[DOC]` ✅ A `ChTailer` per (radiod, mode) SHALL tail the spot log,
  parse each `decode_ft8` line into a `psk.spots` row, and insert via
  `sigmond.hamsci_sink.Writer.from_env()` (`target_db=psk`, `table=spots`,
  `schema_version=2`).
- `PSK-F-021` `[CODE]` ✅ SHALL resolve compound-callsign hashes to plaintext via
  the shared `callhash` table (cross-mode: a call seen on WSPR/MSK144 resolves an
  FT8/FT4 hash), persisting the table at most every 5 min and on stop.
- `PSK-F-022` `[CODE]` ✅ SHALL apply a per-mode decoder dt calibration
  (`PSK_FT8_DT_CAL_SEC` default 0.65, `PSK_FT4_DT_CAL_SEC` default 0.6) so the
  sink/upload dt matches the WSJT-X convention.
- `PSK-F-023` `[CODE]` ✅ Each row SHALL carry `decoder_kind`, `rx_source`
  (`radiod:<status>`), `frequency_bucket_hz` (100 Hz floor), `reporter_id`,
  legacy `instance` (=radiod_id), `forward_to_pskreporter`, `processing_version`,
  and a §18 `timing_authority` provenance block.
- `PSK-F-024` `[CODE]` ✅ `snr_db` SHALL be `null` for `decode_ft8` (its internal
  `score` is not a calibrated dB); the integer `score` is carried instead.
- `PSK-F-025` `[CODE]` ✅ The sink write SHALL degrade to a clean **no-op** when
  the sink path is unwritable (standalone host); spot logs and uploads are
  unaffected.

### 6.4 PSKReporter upload
- `PSK-F-030` `[DOC]` ✅ SHALL run one `HsPskReporterUploader` per daemon that
  pumps spots (30 s cadence) to pskreporter.info via the `hs-uploader`
  `Pipeline` + `PskReporterTcp` transport (owns the TCP socket end-to-end; no
  external `pskreporter` subprocess).
- `PSK-F-031` `[CODE]` ✅ The upload source SHALL be the SQLite sink
  (`SqliteSource`, filtered/projected for PSKReporter) when present, falling back
  to a per-slot `FileTreeSource` (`*.spots.txt`, delete-on-ack) on sinkless hosts.
- `PSK-F-032` `[CODE]` ✅ Upload watermark + retry state SHALL persist to
  `/var/lib/hs-uploader/watermarks.db`, anchored to **now** on a fresh
  watermark so a first deploy / lost state does not re-ship history.
- `PSK-F-033` `[CODE]` ✅ Cross-receiver de-dup on the `direct` path SHALL be
  **opt-in** (`PSK_DIRECT_DEDUP=1`): a SQL window function keeps the best-`score`
  row per `(time, tx_call, frequency_bucket_hz)`; default OFF (the dedup CTE can
  trip `disk I/O error` on a sink shared with wspr-recorder).

### 6.5 Delivery pipelines (the merge-fleet policy)
- `PSK-F-040` `[DOC]` ✅ SHALL select delivery via `PSK_DELIVERY_PIPELINES`
  (comma list of `direct` / `server-merge` / `server-raw`), with legacy
  `PSK_DELIVERY_MODE` (`direct`/`server`/`both`) translated, default
  `server-merge`.
- `PSK-F-041` `[DOC]` ✅ `server-merge` and `server-raw` SHALL set the per-row
  `forward_to_pskreporter` flag (True/False); `direct` does the client-side
  upload.
- `PSK-F-042` `[DOC]` ✅ When BOTH `direct` and `server-merge` are enabled, the
  client SHALL downgrade `server-merge`→`server-raw` (forward=False) so the
  server does not double-post.
- `PSK-F-043` `[CODE]` ✅ Unknown pipeline tokens SHALL be dropped with a WARNING
  while honoring the rest; an all-invalid list falls through to legacy/default.
- `PSK-F-044` `[NEW]` 🟡 `server-merge` SHALL produce dedup+forwarded uploads
  **only when the server endpoint exists**; today the endpoint is unbuilt, so
  `server-merge` behaves like `server-raw` server-side (stores per-rx, no POST).
  *(cross-repo gap — `PSK-F-091`.)*

### 6.6 Self-description & configuration (contract surface)
- `PSK-F-050` `[CODE]` ✅ SHALL implement `inventory --json` / `validate --json`
  / `version --json` per contract v0.8 with **pure-JSON stdout** (a root-logger
  guard redirects logging to stderr before arg parse).
- `PSK-F-051` `[CODE]` ✅ `validate` SHALL **fail** on no `[[radiod]]` block, a
  block with no/placeholder `status`, or an SSRC collision
  `(freq, preset, sample_rate, encoding)` within a block; and **warn** on empty
  callsign/grid, a missing decoder binary, or a block with no FT4/FT8 freqs.
- `PSK-F-052` `[DOC]` ✅ SHALL provide a whiptail-driven config wizard
  (`config init|edit`, §14) with a stdin fallback, plus JSON entry points
  (`config show|apply --json`, `env show|apply --json`) for atomic
  sigmond/tooling edits.
- `PSK-F-053` `[CODE]` ✅ `env apply` SHALL accept **only** the delivery-knob keys
  (`PSK_DELIVERY_PIPELINES`, `PSK_USE_HS_UPLOADER`, `PSK_DIRECT_DEDUP`, legacy
  `PSK_DELIVERY_MODE`); any other key is rejected, and a JSON `null` deletes a key.
- `PSK-F-054` `[CODE]` ✅ Per-instance config (`/etc/psk-recorder/<instance>.toml`)
  SHALL be preferred over the legacy shared `psk-recorder-config.toml`; the
  legacy fallback emits a one-line `DeprecationWarning`.

### 6.7 Lifecycle & control
- `PSK-F-060` `[CODE]` ✅ SHALL honor `PSK_RECORDER_LOG_LEVEL` / `CLIENT_LOG_LEVEL`
  / `--log-level` at startup and re-apply on **SIGHUP** without restarting RTP.
- `PSK-F-061` `[CODE]` ✅ SHALL read `RADIOD_<ID>_CHAIN_DELAY_NS` (env, §8) and
  surface it as `chain_delay_ns_applied`, with `[timing].chain_delay_ns` as the
  standalone fallback; the correction is **recorded, not applied** (out of regime).
- `PSK-F-062` `[CODE]` ✅ SHALL exit `EX_CONFIG (78)` on the unconfigured-radiod
  placeholder and SHALL NOT crash-loop that exit (`RestartPreventExitStatus=78`).

## 7. Quality / non-functional requirements

- `PSK-Q-001` `[CODE]` ✅ Decode children SHALL be CPU-confined off radiod's cores
  (sigmond affinity drop-in) so burst decode cannot induce RX888 USB drops.
- `PSK-Q-002` `[CODE]` ✅ The unit SHALL be `Type=notify` with `WatchdogSec=120`,
  `Restart=always` (burst-capped 10/300 s), `MemoryMax=2G`/`MemorySwapMax=0`, and
  `TimeoutStartSec=180` to cover a 19-channel provisioning startup.
- `PSK-Q-003` `[CODE]` ✅ The sink Writer, the SqliteSource, and the callhash
  library SHALL each degrade to a graceful no-op when their backing resource is
  absent; none may hard-fail the daemon.
- `PSK-Q-004` `[CODE]` ✅ The upload path SHALL be **idempotent across restarts**
  via the persistent watermark store and `start_at=now` first-pump anchor.
- `PSK-Q-005` `[CODE]` ✅ Multi-instance isolation SHALL hold by construction: the
  SqliteSource scope and per-instance spool/log/env paths keep instances from
  cross-contaminating one shared sink.
- `PSK-Q-006` `[CODE]` ✅ The unit SHALL NOT re-stomp the group/ownership of the
  **shared** `/var/lib/hs-uploader` (owned by tmpfiles.d as `root:sigmond 02775`)
  — doing so locked wsprdaemon out of the shared watermarks.db (bee1 2026-05-14).
- `PSK-Q-007` `[CODE]` ✅ The hardened unit SHALL run with `ProtectSystem=strict`,
  `NoNewPrivileges`, a minimal `CAP_NET_RAW/CAP_NET_BIND_SERVICE` capability set,
  and an explicit `ReadWritePaths` including `/var/lib/sigmond` (omitting it
  silently no-ops every spot write).
- `PSK-Q-008` `[CODE]` ✅ Sink retention SHALL be externally janitored by
  `smd admin storage trim` (`PSK_RETENTION_MIN`, default 60 min, 30 min floor);
  the producer does **not** delete-on-commit, so multiple consumers can read the
  queue.
- `PSK-Q-009` `[NEW]` ✅ Decoder timestamp accuracy SHALL be `~1 s`
  (slot-quantized); finer accuracy is explicitly not claimed. The §18 timing
  authority is read and stamped for provenance only and intentionally NOT applied
  to gate timing (`uses_timing_calibration=false`) — psk-recorder's products are
  FT8 15 s / FT4 7.5 s slot-quantized, so RTP-default timing is sufficient. This
  is a deliberate design decision (sigmond #36), not an open gap.

## 8. External interfaces

### 8.1 Inputs (derived from deploy.toml + config + inventory --json)
- **RF/data:** radiod RTP via ka9q-python; one channel per freq,
  `preset=usb`, `sample_rate=12000`, `encoding=s16be`. Bound radiod = the
  `[[radiod]].status` mDNS name (live: `sigma-rx888mk2-status.local`).
- **External binary:** `decode_ft8` (ka9q/ft8_lib fork `37484ad`) at
  `decoder_decode_ft8` (default `/usr/local/bin/decode_ft8`).
- **Config — `/etc/psk-recorder/<instance>.toml`** (legacy shared
  `psk-recorder-config.toml`). Operator MUST set: `[[radiod]].status`;
  `[radiod.ft8].freqs_hz` and/or `[radiod.ft4].freqs_hz`. Operator SHOULD set:
  `[station].callsign`, `[station].grid_square`. Optional: `[paths]`
  (`spool_dir`, `log_dir`, `decoder_kind`, `decoder_decode_ft8`, `keep_wav`),
  `[timing].chain_delay_ns`, `[instance].reporter_id`, PSWS ids.
- **Per-instance env — `/etc/psk-recorder/env/<instance>.env`** (delivery knobs):
  `PSK_USE_HS_UPLOADER`, `PSK_DELIVERY_PIPELINES`, `PSK_DIRECT_DEDUP`, legacy
  `PSK_DELIVERY_MODE`. (Live: `PSK_USE_HS_UPLOADER=1`, `PSK_DELIVERY_PIPELINES=direct`.)
- **Coordination env — `/etc/sigmond/coordination.env`** (sigmond-owned,
  read-only here): `STATION_CALL`, `STATION_GRID`, `SIGMOND_SQLITE_PATH`,
  `RADIOD_<ID>_CHAIN_DELAY_NS`, `PSK_RECORDER_LOG_LEVEL` / `CLIENT_LOG_LEVEL`,
  `SIGMOND_INSTANCE`, `SIGMOND_RADIOD_STATUS`.
- **Optional:** `hf-timestd` authority at `/run/hf-timestd/authority.json` (§18).

### 8.2 Outputs (derived)
- **Shared sink:** `sigmond.hamsci_sink` `target_db=psk`, `table=spots`,
  `schema_version=2`, at `SIGMOND_SQLITE_PATH` or `/var/lib/sigmond/sink.db`.
  Row fields: `time`, `mode`, `decoder_kind`, `score`, `snr_db`(null),
  `spectral_width_hz`(null), `dt`, `frequency`, `frequency_mhz`,
  `frequency_bucket_hz`, `message`, `tx_call`, `rx_call`, `grid`, `report`,
  `host_call`, `host_grid`, `radiod_id`, `instance`(legacy), `reporter_id`,
  `rx_source`, `processing_version`, `forward_to_pskreporter`, `timing_authority`.
- **Upload:** pskreporter.info via `PskReporterTcp` (`direct`), and/or the
  wsprdaemon-server raw/merge path (`server-*`).
- **Spool:** `/var/lib/psk-recorder/<radiod_id>/{ft8,ft4}/YYMMDD_HHMMSS.wav`
  (deleted after decode by default). `inventory` `data_sinks`: spool (`file`,
  retention 0) + log_dir (`file`, retention 365 d, ~5 mb/day).
- **Spot logs:** `/var/log/psk-recorder/<radiod_id>-{ft8,ft4}.log`;
  timing log `<radiod_id>-timing.log`. Surfaced in inventory `log_paths`.
- **Process log:** systemd journal (`SyslogIdentifier=psk-recorder@%I`).
- **Uploader state:** `/var/lib/hs-uploader/watermarks.db`.
- **Self-description:** `inventory`/`validate`/`version --json` (live:
  `client=psk-recorder`, `version=0.4.0`, `contract_version=0.8`, 19 channels,
  modes `[ft8,ft4]`, `uses/provides_timing_calibration=false`,
  `timing_authority_applied=null`, `issues=[]`).

### 8.3 Contracts / APIs (reference, not restated)
- `PSK-I-001` `[CODE]` ✅ Conforms to **client contract v0.8** (`contract.py`
  `CONTRACT_VERSION="0.8"`; `deploy.toml` `contract_version=0.8`). `deploy.toml`
  declares templated unit `psk-recorder@.service`, build/install/render steps,
  `config init|edit` interview hooks (§14), git deps (`ft8_lib`,
  `ftlib-pskreporter`), pypi dep `ka9q-python`, and `client_features`
  (`watch`/`verifier`/`receiver_channels`, verb `psk`). Field semantics:
  contract §1–§7, §10–§14, §17. Full spec:
  [CLIENT-CONTRACT.md](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md).
- `PSK-I-002` `[CODE]` 🟡 **Timing-authority consumer (partial, §18):** reads
  hf-timestd's authority via `authority_reader.py` and stamps a `timing_authority`
  block on every row, falling back to `standalone_timing_authority` when absent —
  **but does not consume it to gate/correct timing** (`uses_timing_calibration=false`,
  `timing_authority_applied=null`). Subscriber obligations are defined by the
  contract, not here.
- `PSK-I-003` `[DOC]` ✅ The `server-merge` delivery path's wire/endpoint contract
  is governed by
  [PHASE-D-SERVER-MERGE-ENDPOINT.md](https://github.com/HamSCI/sigmond/blob/main/docs/PHASE-D-SERVER-MERGE-ENDPOINT.md)
  (server endpoint unbuilt — see `PSK-F-091`).

## 9. Data requirements

The canonical artefact is the `psk.spots` sink row (schema_version 2, fields in
§8.2); the per-mode spot log is the durable on-disk source the tailer derives it
from. Provenance/timing: every row carries a §18 `timing_authority` block
(`source = hf-timestd | rtp-default | unavailable`), `decoder_kind`, and
`processing_version`; timestamps are UTC, slot-quantized (~1 s) with a per-mode dt
calibration. Retention: sink rows externally trimmed (`PSK_RETENTION_MIN`,
default 60 min, 30 min floor, **not** delete-on-commit so multiple consumers can
read); spot logs ~5 MB/day, operator-retained ~365 d; WAV spool ephemeral
(deleted post-decode). The compound-call hash table persists to a per-radiod
JSON store and accumulates across restarts.

## 10. Dependencies & development sequence

**Runtime deps:** `radiod` (required), `decode_ft8` (ka9q/ft8_lib fork,
required, per-host build), `ka9q-python ≥3.14` / `callhash ≥1.0` /
`hs-uploader ≥0.1` (editable siblings), `numpy`, `tomli` (py<3.11), `whiptail`
(optional wizard UI), `sigmond` (optional, lazy-imported). The legacy
`pskreporter`/`pskreporter-sender` binary is **no longer on the runtime upload
path** (PskReporterTcp owns the socket); `validate`'s check for it is vestigial.

**Development sequence (intended, recovered as requirement):** it began as the
contract's **greenfield v0.3 reference implementation** and surfaced the six v0.4
hardening items (§12) during its first deploy. Subsequent phases, recovered from
the code: **Phase A** rx_source plumbing (multi-source identity) → **Phase B**
multi-source single-process capture → **Phase C** cycle-batcher (cycle-aligned
commit) → **Phase D** the delivery-pipeline policy + cross-rx dedup (Cut 1
per-rx tally, Cut 2 `frequency_bucket_hz` + SQL dedup, Cut 3 `PSK_DELIVERY_PIPELINES`,
Cut 4 server-raw tar). The legacy `pskreporter` subprocess and ClickHouse sink
were removed in the same sweep that moved upload in-process onto hs-uploader and
SQLite. Multi-instance per-instance config cutover tracks sigmond's
MULTI-INSTANCE-ARCHITECTURE phases (per-instance `<instance>.toml` shipped;
`smd instance migrate` Phase 8 pending; legacy `instance` row field removed Phase 9).

## 11. Acceptance criteria & verification

- **Contract conformance** → `psk-recorder validate --json` (exit 0, no `fail`;
  live: `ok=true`, `issues=[]`) surfaced via `smd status`.
- **SSRC safety** → `validate` fails on duplicate `(freq,preset,rate,enc)` within
  a block (the 1.840 MHz FT8/FT4 collision regression test).
- **Decode/spot integrity** → spots land in the per-mode log and the `psk.spots`
  sink with hash-resolved calls; standalone host → sink Writer is a clean no-op.
- **Upload delivery** → `smd admin verifier report --target psk` audits
  delivered / lost / in-flight on the FT cycles; watermark idempotency across
  restart (no re-shipped history).
- **Delivery policy** → `PSK_DELIVERY_PIPELINES` resolution: `direct`+`server-merge`
  downgrades to `server-raw`; unknown tokens dropped with warning.
- **Instance isolation / liveness** → one `psk-recorder@<id>` per radiod, off
  radiod cores, `Type=notify` watchdog healthy under the 19-channel layout.

## 12. Risks & open questions

- `PSK-F-090` `[NEW]` 🟡 **Decoder-backend drift:** `decoder_kind` implies a
  pluggable decoder and historical comments/README reference `jt9`, but
  `decode_ft8` is the only wired/installed backend. Either wire `jt9` or document
  the field as decode_ft8-only. *(candidate #18 Clients issue.)*
- `PSK-F-091` `[NEW]` ⬜ **`server-merge` half-built:** the client emits the
  Phase-D fields and routing intent, but the wsprdaemon-server dedup+forward
  endpoint is a plan (PHASE-D-SERVER-MERGE-ENDPOINT.md). Until shipped,
  `server-merge` silently behaves like `server-raw` server-side. Cross-repo;
  promote to a wsprdaemon-server issue.
- `PSK-F-092` `[NEW]` ⬜ **Vestigial `pskreporter` validate check:**
  `contract.py` / config still reference a `pskreporter` binary that is no longer
  on the upload path; remove or document as legacy (ties §12.6 PyPI-lag retrofit).
- `PSK-F-093` `[NEW]` 🟡 **Doc-version drift:** `docs/SIGMOND-CONTRACT.md` still
  reads "v0.4 / greenfield v0.3 reference" while code/deploy are at v0.8 and the
  `id`-field binding it documents was removed at the status-name (Phase 6)
  cutover. Reconcile the doc to v0.8.
- `PSK-Q-009` (timing read-but-not-applied) and `PSK-F-033` (direct-dedup
  disabled-by-default due to a shared-sink `disk I/O error`) are the two known
  quality limits; the dedup CTE needs rework before it can default ON.

## 13. Traceability

| Requirement | #18 issue | Verification | PSWS #6 |
|---|---|---|---|
| PSK-I-001 (contract v0.8 conformance) | Clients: psk-recorder | `validate --json` exit 0 | #6:31 (sensor integ.) |
| PSK-F-020 (psk.spots sink tailer) | Clients: psk-recorder | sink schema_version 2 row test | #6:31 |
| PSK-F-030 (PSKReporter upload) | Clients: psk-recorder | `smd verifier report --target psk` | #6:40 (upload egress) |
| PSK-F-040 (delivery pipelines) | psk: server-merge endpoint | pipeline-resolution unit test | — |
| PSK-F-051 (SSRC validate fail) | — | 1.840 MHz collision fixture | — |
| PSK-F-090 (decoder-backend drift) | *(new — file)* | decoder_kind doc/code review | — |
| PSK-F-091 (server-merge endpoint) | *(new — file, wsprdaemon-server)* | merge-endpoint integration | #6:40 |
| PSK-F-092 (vestigial pskreporter check) | *(new — file)* | validate review | — |
| PSK-F-093 (SIGMOND-CONTRACT.md drift) | *(new — file)* | doc reconcile to v0.8 | — |
| PSK-I-002 (§18 partial consume) | psk: timing consumption | authority stamp test | #6:50 (timing tiering) |

*New rows (PSK-F-090/091/092/093) are this review's surfaced gaps; promote to
the #18 psk-recorder epic (091 cross-files to wsprdaemon-server).*
