# jt9_decode (triggered-mode fork) — provenance & license

## Upstream

Forked from **madpsy/jt9-decode** `git@github.com:madpsy/jt9-decode.git`
at commit **1b8507a4a0148328c5d1754661f4c6626f753a5a** ("fix defaults",
2026-05-10). Files taken: `jt9_decode.cpp`, `Makefile`, `wsjtx/commons.h`.

## License — GPLv3

`wsjtx/commons.h` is taken verbatim from **WSJT-X** (the `dec_data`/`params`
shared-memory struct and constants), and `jt9_decode.cpp` drives WSJT-X's `jt9`
binary over WSJT-X's shared-memory decode protocol. It is therefore a
**derivative work of WSJT-X**, which is licensed under the **GNU General Public
License, version 3**. This fork is distributed under GPLv3 accordingly; there is
no warranty. WSJT-X's `COPYING` (full GPLv3 text) is retained on every host by
sigmond's `wsjtx-decoders` native build (`_build_wsjtx_decoders`, which also
installs `jt9`) under `/usr/local/share/doc/wsjtx-decoders-sigmond/`.

## Sigmond modifications (2026-07-22)

Build-behaviour changes only; **no decoder logic changed**:

1. **Triggered mode** (`-T` / `--triggered`). Decode each fed slot in stream
   order, immediately, with **no wall-clock timing**. The caller (psk-recorder)
   writes exactly one GPS/RTP-aligned slot's worth of 12 kHz s16 mono PCM per
   cadence and stamps the authoritative slot UTC downstream. The stock `-s`
   stream mode instead self-aligns decodes to wall-clock UTC boundaries and
   grabs "the last N samples", which drifts against radiod's RTP↔GPS anchor and
   is unusable for a GPS-disciplined site (see `docs/jt9-decoder.md` §3).
   Contract: reads `SAMPLES_PER_CYCLE` samples per slot in order, never skips,
   and emits exactly one terminal `<DecodeStats cycle_num=N …>` per slot (even
   on a watchdog timeout) so the caller can map decode lines to the slot it fed.

2. **ipc handshake idle-gate.** Back-to-back resident decodes raced the WSJT-X
   ipc protocol: `jt9a.f90` finishes a decode by waiting for the caller's ack
   `ipc[2]==1`, then clears `ipc[2]→0` and loops to wait for the next start
   `ipc[1]==1`. Firing the next decode before jt9 consumed the ack left it stuck
   waiting → the 2-cycle watchdog. Triggered mode now waits for `ipc[1]==0 &&
   ipc[2]==0` (jt9 idle) before issuing the next slot. The stock one-shot and
   15 s-gap stream paths never exercised back-to-back decodes, so this latent
   bug was never hit upstream.

## Build & home

Built by sigmond's `_build_wsjtx_decoders` (sigmond `bin/smd`) alongside `jt9`
and `wsprd` — it needs `jt9` + Qt5, both already in that recipe's deps — and
installed to `/usr/local/bin/jt9_decode`. This directory is the pinned source
of record; the smd recipe compiles it in place. `make` here (Qt5Core + `moc`)
produces the same binary for local testing.

## Validation

Bench-validated 2026-07-22 against WSJT-X 3.0.2 `jt9`: fed three distinct
reference FT8 slots as one stdin stream, triggered mode decoded all three in
order (1, 5, and 2 decodes), each with a terminal `<DecodeStats watchdog=0>`,
no wall-clock waiting, clean EOF exit. A live `<...>` unresolved compound-call
hash appeared in slot 3 — the exact case resident jt9 resolves across a session.
