# jt9 as an FT8/FT4 decoder option

Status: **in progress** (branch `feat/jt9-decoder-option`). Draft for mjh review.

Re-adds WSJT-X's `jt9` as a selectable FT8/FT4 decoder alongside the default
`decode_ft8` (ka9q/ft8_lib). jt9 was psk-recorder's default through v0.4.0 and
was removed in `ead51ca` (2026-05-19) as a "dormant, resource-heavy opt-in".
This brings it back on the current architecture, but with a fundamentally
different — and correct — process model (see §2).

## 1. Why jt9

* **Sensitivity.** On live busy bands jt9-deep finds ~2–3× the decodes of
  decode_ft8 (measured 2026-07-22, `jt9-experiment/FINDINGS.md`): e.g. 20 m
  14074 kHz, decode_ft8 15 vs jt9-d3 45.
* **Calibrated SNR.** jt9 reports real dB SNR; decode_ft8 reports only an
  uncalibrated internal "score". jt9 rows therefore populate `snr_db`
  (decode_ft8 leaves it `NULL`).
* **WSJT-X dt convention** natively (no calibration offset — see §5).

Cost: jt9 is 3–8× the CPU of decode_ft8 (~0.4 CPU-s/15 s today) — jt9-fast
≈ 1.2 cores continuous fleet-wide, jt9-deep ≈ 3.3 cores. It must stay pinned
off radiod's HT pair (cores 2–13 on B4). So it is **opt-in**; the default
stays `decode_ft8`.

## 2. The load-bearing decision: jt9 MUST run resident

FT8/FT4 nonstandard (compound) callsigns are transmitted as a 22-bit hash
after the full call has been sent once. The decoder resolves the hash from a
table it accumulates as it hears full calls.

In WSJT-X that table (`lib/77bit/packjt77.f90`) is a **module-level in-RAM
array** — `nzhash` counter, `save_hash_call` appends, `hash22` linear-searches
it — marked `save` (per-process lifetime) with **no disk persistence anywhere**
(verified in the 3.0.2 source: the only `fopen(hash_fname)` is in `wsprd.c`,
WSPR's unrelated type-3 table; the GUI writes no FT8 hashcalls file either).

Consequences:

* A fresh `jt9` process **always** starts at `nzhash=0`. The removed
  per-slot-fork code (new `mkdtemp` `-a` dir every slot) could therefore
  **never** resolve a hash learned in a prior slot — it emitted `<...>` and
  the spot was effectively lost.
* Unlike ka9q's `decode_ft8` — which was *patched* to emit the numeric hash
  `<NNNNNNN>` so sigmond's `callhash` library can resolve it tailer-side —
  **stock jt9 emits only `<...>`** for an unresolved hash. There is nothing
  for `callhash` to key on. Tailer-side rescue is impossible for jt9.
* Therefore the **only** way to resolve compound-call hashes with jt9 is to
  keep the jt9 process **resident** so its in-RAM table persists across slots.
  A persistent `-a` data dir cannot help (no file exists). The longer a
  resident jt9 lives, the more it resolves — so its processes should be
  long-lived and not restarted casually.

This is why the integration is a resident-process model, not the old fork.

## 3. Process model

One **resident `jt9`** per `(band, mode)`, driven over WSJT-X's `QSharedMemory`
IPC. We reuse the already-written `jt9_decode` wrapper
(`jt9-experiment/jt9-decode`, madpsy) which implements exactly that GUI-side
protocol: it holds one `jt9` via `QProcess`, fills the shmem `dec_data` ring,
triggers a decode per cycle, and relays jt9's decode lines on its own stdout.
(~19–20 resident processes for B4's FT8+FT4 band set.)

**Timing authority stays with psk-recorder.** We do *not* free-run the
wrapper off a continuous stdin stream. The stock `jt9_decode` self-times: a
wall-clock `QTimer` fires at UTC-aligned `cycle_ms` boundaries and grabs "the
last N samples" from a circular buffer, stamping the decode with `nutc =
gmtime()`. That clock is not GPS-anchored — when radiod's RTP↔UTC steps (the
fault psk-recorder's re-anchoring absorbs) it would grab the wrong span and
drift dt, uncorrectably. **Confirmed by bench probe 2026-07-22** (§9): fed a
reference slot, the wrapper emitted the decode stamped with the run's
wall-clock minute, not the audio's true time, and in stream mode waited for the
wall-clock boundary before decoding.

So we **fork the wrapper into a triggered mode**: remove the wall-clock timer
and the last-N grab; instead decode exactly the one slot's worth of PCM that
`SlotWorker` writes to its stdin, immediately, and loop. `SlotWorker` — which
already extracts each cadence slot's exact sample window from the RTP/GPS-
anchored ring — feeds that aligned slot and stamps the authoritative slot UTC
itself. jt9 contributes only *relative* quantities (dt, snr, audio-frequency
offset, message); psk-recorder supplies the absolute time and frequency anchor.
The change is small and well-bounded: the shmem trigger handshake (memcpy →
`dec_data->d2`, set `params.nutc/kin/newdat`, poke `ipc[]`) is reused verbatim;
only its *driver* (wall-clock timer → per-slot stdin) is replaced.

## 4. Wire formats (captured 2026-07-22 from jt9 3.0.2)

jt9's stdout decode line (what the wrapper relays):

```
110115   9  0.9 1234 ~  GJ0KYZ RK9AX MO05
HHMMSS  SNR  DT  FREQ ~  MESSAGE
```

* `HHMMSS`  — time of day only, **no date**, and in resident mode driven by
  the params we set. psk-recorder ignores it and uses the slot's true UTC.
* `SNR`     — calibrated dB (signed int).
* `DT`      — seconds within the slot (signed float).
* `FREQ`    — **audio offset** in Hz (0–3000), *not* absolute RF. Absolute =
  channel dial (`SlotWorker._frequency_hz`) + offset.
* control line `<DecodeFinished> …` is filtered by the wrapper (not a spot).

`SlotWorker` normalizes each relayed line into the canonical jt9 log line the
tailer parses (SYNC is unavailable on stdout → placeholder `0`; MODE from the
worker's mode):

```
YYMMDD HHMMSS BAND_FREQ_HZ SYNC SNR DT FREQ_OFFSET_HZ MARKER MESSAGE… MODE
```

`ch_tailer.parse_jt9_line` reads that, computes absolute freq = BAND_FREQ +
OFFSET, and emits a `psk.spots` row.

## 5. Field mapping & dt calibration

| psk.spots field | jt9 source | note |
|---|---|---|
| `time` | slot start UTC (SlotWorker) | authoritative, GPS-anchored |
| `dt` | jt9 DT | **no calibration** — jt9 *is* WSJT-X. `_FT8_DT_CAL_SEC`/`_FT4_DT_CAL_SEC` are decode_ft8→WSJT-X offsets and MUST NOT be applied to jt9 rows. |
| `snr_db` | jt9 SNR | real dB (decode_ft8 → `None`) |
| `score` | SYNC | `None`/`0` in resident-stdout mode (SYNC not on stdout) |
| `frequency` | BAND_FREQ + OFFSET | absolute Hz |
| `spectral_width_hz` | — | not surfaced |
| `decoder_kind` | `"jt9"` | tags the row |
| message/calls | `callhash.parse_message` | jt9 self-resolves compound calls it has heard; `<...>` stays unresolved |

## 6. Config surface

`[paths]` in the recorder config / deploy.toml:

```toml
decoder_kind  = "jt9"          # default "decode_ft8"
decoder_depth = 3              # jt9 -d: 1 fast (~1.2 cores) … 3 deep (~3.3 cores)
decoder_jt9   = "/usr/local/bin/jt9_decode"   # wrapper path (falls back to PATH)
```

v1 is **process-global** (recorder resolves one decoder per instance — matches
the existing "one decoder binary" assumption). Per-band mixing (decode_ft8 on
quiet bands, jt9-deep on 1–2 priority bands) is a deliberate follow-up, not v1.

## 7. Touch points

* `core/slot.py` — `DECODER_JT9`; `VALID_DECODER_KINDS`; resident-wrapper
  producer (spawn/supervise per (band,mode), feed slot PCM, relay+normalize
  stdout); thread `decoder_depth`.
* `core/ch_tailer.py` — `parse_jt9_line` + router branch; dt-cal branched by
  kind; `callhash` table arg.
* `core/recorder.py`, `core/stream.py`, `core/receiver_manager.py` — resolve +
  thread `decoder_kind`/`decoder_depth`; wire the resident path when kind=jt9.
* `config.py` — `decoder_kind`/`decoder_depth`/`decoder_jt9` defaults.
* `tests/test_jt9.py` — recovered + adapted to the resident wire format.

## 8. Native dependency

Our **forked** `jt9_decode` (triggered mode, §3) is vendored here at
`vendor/jt9-decode/` (GPLv3; PROVENANCE.md). **Wired into sigmond `bin/smd`**
(`_build_jt9_decode`, called from `_build_wsjtx_decoders`): built out-of-tree as
a cheap Qt5 add-on to the WSJT-X toolchain and installed to
`/usr/local/bin/jt9_decode`, idempotent via a source-hash marker, best-effort so
it can't fail wspr-recorder. Both wspr-recorder and psk-recorder installs
trigger it (order-independent). Open policy item for mjh: every psk-recorder
install then builds the WSJT-X toolchain even for the common decode_ft8 case —
flagged inline in smd to gate on `decoder_kind=jt9` if desired.

## 9. Bench probe (2026-07-22) + remaining validation

**Bench probe — DONE**, on the experiment jt9 3.0.2 + compiled `jt9_decode`
(no jt9 on B4 yet). Findings:

* Resident jt9+shmem decodes correctly through the wrapper: one-shot on
  `191111_110115.wav` → `... 11  0.9 1234 ~ GJ0KYZ RK9AX MO05` (matches bare
  jt9's message/dt/offset; SNR in dB).
* Stdout decode-line format confirmed: `HHMMSS SNR DT FREQ_OFFSET ~ MESSAGE`
  plus a `<DecodeFinished>`/`<DecodeStats>` control line the wrapper emits.
* **The wrapper self-times on wall-clock and is unusable as-is** — it stamped
  the decode with the run's wall-clock minute (`nutc=gmtime`, `001814`), not
  the audio's true time (`110115`), and in stream mode logged "Waiting … for
  cycle boundary" then "Triggering decode #1 … +30.000s" at the wall-clock
  boundary. ⇒ resolves the old open item: we must fork it to triggered mode
  (§3), not feed the stock streaming path.
* The trigger handshake is small/clean ⇒ the fork is low-risk.

**Triggered-mode fork — DONE + validated 2026-07-22** (`vendor/jt9-decode/`):

* Added `-T`/`--triggered`: decode each fed slot in stream order, immediately,
  no wall-clock timer; caller writes one aligned slot per cadence and owns the
  UTC. Emits exactly one terminal `<DecodeStats cycle_num=N …>` per slot.
* Fixed a latent ipc handshake race (idle-gate on `ipc[1]/ipc[2]`) that hung
  back-to-back resident decodes — never exercised by the stock one-shot/15 s
  stream paths. Root-caused against `jt9a.f90`'s ack loop.
* Bench: 3 distinct reference slots as one stdin stream → 3 in-order decodes
  (1/5/2), each `watchdog=0`, clean EOF exit; a live `<...>` compound-call hash
  appeared (the case resident jt9 resolves over a session).

**SlotWorker producer — DONE (code-complete, unit-tested 2026-07-22).** For
`decoder_kind="jt9"`, SlotWorker spawns one resident `jt9_decode -T` per (band,
mode), feeds each GPS-aligned slot's PCM to its stdin, and a reader thread
normalizes the relayed decode lines into the canonical jt9 log line (stamping
the authoritative slot UTC + dial+offset), mapping decodes to slots by order via
the terminal `<DecodeStats>` FIFO. Crash → restart (hash table resets, logged).
`decoder_depth` threaded recorder→manager→stream→worker; recorder resolves the
wrapper path for kind=jt9; `VALID_DECODER_KINDS` now includes jt9. Unit tests
round-trip stdout→canonical→`parse_jt9_line`. Pending live validation (no jt9 on
B4 yet) + the smd native-dep build (§8).

**Remaining (needs jt9 + the fork installed):**

0. **From-source toolchain parity — DONE off-production 2026-07-22** (live audio
   captured read-only off B4 radiod; full data in `docs/decoder-findings.md`).
   Did NOT need B3: captured real slots and bench-decoded Debian vs from-source.
   **It caught a WSPR-killer before any deployment** — the exact reason this
   check existed:
   * **`wsprd` was broken by our own build flags.** Stripping `-fbounds-check`
     (an earlier draft "optimization") miscompiles the WSPR Fortran under
     `-O3 -march=native`: **0 spots, ~70 s spin** where Debian decodes 6 spots
     in 1.5 s. Sole culprit — `-march=native` alone is fine. Fixed in smd
     `634df7d` (KEEP `-fbounds-check`; jt9's FT8 path never hit it, so only a
     live *decode* test caught it, not the build-only check).
   * **After the fix, `wsprd` is correct + faster:** same real 20 m window,
     min-of-3 — Debian 2.7.0 **1.55 s / 6 spots**, our 3.0.2 **0.77 s / 6 spots**
     (~2×). The speedup is the *version* (3.0.2 vs 2.7.0); `-march=native` adds
     ~nothing to wsprd (Fano-bound). Parity confirmed on 20 m; wider-band sweep
     still worth doing on the eventual live node.
   * **`jt9` (FT8) validated:** live 20 m deep decode ~1.2× faster than Debian
     (4.8 s vs 5.9 s), and keeping `-fbounds-check` costs it nothing
     (kept ≈ stripped, identical decode counts).
   * Still to confirm on a live receiving node: PATH shadowing after a real
     `smd install`, FST4W via from-source jt9, and before/after vs wsprnet/wd30.

1. FT4 path: confirm triggered mode at the 7.5 s cadence (`-m FT4`, 90 000
   samples/slot) as cleanly as FT8.
2. Supervision: exercise restart-on-crash live without losing the whole band; on
   restart the hash table resets (accepted) — already logged.
3. dt sanity: same on-time WAV, jt9 dt ≈ +0.17 vs decode_ft8 +0.82 (measured);
   confirm jt9 rows land near 0 with no calibration applied.
4. Absolute-frequency reconstruction matches decode_ft8 on the same slot.
5. CPU sizing on cores 2–13 at the chosen depth; B3 stage then live A/B on one
   band before fleet.
6. Compound-call hash resolution actually improves across a multi-slot session
   (the whole point of resident jt9) — verify `<...>` counts fall over time.
