# jt9 (WSJT-X) vs decode_ft8 (ka9q ft8_lib) — FT8 decoder comparison
2026-07-22, B4-100. Reference vectors: /opt/git/sigmond/ft8_lib/tests/*.wav (15s, 12k/16/mono).
Decoders pinned to cores 2-13 (off radiod's HT pair). CPU = user+sys seconds per 15s slot.

## Sensitivity + CPU (per-file, same WAV to each)
| WAV            | ft8_lib | ft8_lib cpu | jt9 d1 | cpu | jt9 d2 | cpu | jt9 d3 | cpu |
|----------------|---------|-------------|--------|-----|--------|-----|--------|-----|
| 191111_110615  |   17    |   0.04s     |  19    |1.81 |  22    |2.45 |  22    |3.53 |
| websdr_test1   |   13    |   0.03s     |  15    |0.64 |  19    |2.70 |  19    |4.78 |
| websdr_test5   |   17    |   0.03s     |  27    |0.73 |  26    |2.97 |  28    |3.89 |
| websdr_test10  |   12    |   0.07s     |  12    |0.71 |  17    |2.87 |  19    |6.33 |

## Findings
1. **jt9 is more sensitive.** Fast (d1) ≈ or slightly beats ft8_lib; deep (d3) finds +50–65%
   more (weak signals ft8_lib misses, e.g. RV6K/DL1UDO at SNR −2..−4).
2. **jt9 costs 15–150× the CPU.** ft8_lib ≈ 0.04s/slot; jt9 fast 0.6–1.8s; jt9 deep 3.5–6.3s.
3. **The madpsy/jt9-decode streaming stdin interface WORKS.** `jt9_decode -j /usr/bin/jt9
   -m FT8 -s` reads continuous 12k/16/mono PCM from stdin, keeps jt9 resident. Measured
   ~1.8s/slot (≈ jt9's default/fast depth) — i.e. it removes the per-invocation process
   spawn + FFTW-plan overhead but NOT jt9's intrinsic decode cost, which dominates.
   (Caveat: streaming decode COUNT from concatenated independent test files is unreliable —
   they don't form a period-aligned 60s stream; use the per-file table for sensitivity.)
4. **Instance model:** one MODE per instance (-m FT8 | FT4) and one stdin stream per
   instance ⇒ **one persistent jt9_decode process per (band, mode)**. ~10 bands × 2 modes
   ≈ 20 resident processes. Cannot multiplex bands/modes in one instance.

## Fleet implication (B4, ~10 FT8 bands, 15s cadence)
- ft8_lib: ~0.4 CPU-s per 15s slot across all bands — negligible (what we run today).
- jt9 streaming FAST: ~10 × 1.8s = ~18 CPU-s/15s ≈ **1.2 cores** continuous.
- jt9 streaming DEEP: ~10 × 5s = ~50 CPU-s/15s ≈ **3.3 cores** continuous.
- All must stay on cores 2-13 (radiod's pair 0-1 is off-limits — cache isolation).

## Gotchas learned
- Our /usr/local/bin/decode_ft8 is the ka9q FORK: daemon/inotify dir-watch + lockfiles,
  NO single-file mode. Built upstream kgoba/ft8_lib for a clean file-mode decoder (same
  ft8/decode.c core). Upstream: `decode_ft8 <wav>`, `-ft4` for FT4.
- jt9 parses the decode time from the FILENAME (YYMMDD_HHMMSS.wav) — rename breaks it.
- jt9 writes decoded.txt + timer.out to CWD and needs its data/wisdom files in CWD (or
  --data-path); a non-writable cwd makes it exit 0 with no decodes. Run from a writable
  dir that has the wsjtx data (the jt9-decode repo bundles wsjtx/ + jt9_wisdom.dat).

## Recommendation / next steps
- jt9 is a real sensitivity upgrade but a real CPU commitment. Options to weigh:
  (a) hybrid: ft8_lib for all bands + jt9-deep as a SECOND pass only on high-value bands
      or only on slots where ft8_lib found little; (b) jt9-fast fleet-wide (~1.2 cores,
      still beats ft8_lib on sensitivity); (c) jt9-deep on 1–2 priority bands.
- Next: A/B on OUR live signals (capture real per-band 15s WAVs from radiod, not just the
  reference vectors) to confirm the sensitivity delta holds on our antenna/noise, and to
  size CPU with real occupancy. Harness: /home/rob/jt9-experiment/compare.sh

## Compiler-optimization experiment (2026-07-22)
Stock Debian jt9 (wsjtx 2.7.0) is x86-64-v1: SSE2 only (0 AVX2/FMA insns), and
the RELEASE build shipped -fbounds-check (array bounds checking in the optimized
build!). CPU supports avx2+fma. Rebuilt jt9 from source with -march=native -O3
and -fbounds-check stripped (build-opt/jt9; 58,832 AVX2/FMA insns confirmed).

Result (min-of-3, live 20m/15m/40m FT8 slots, cores 2-13):
- DEEP (-d3): 1.36-1.60x faster (8.2->5.1s busy 20m; 3.0->2.1s; 3.2->2.3s).
- FAST (-d1): ~1.0x (no gain; FFT-bound and FFTW already dispatches AVX at
  runtime regardless of jt9's compile flags).
Decode counts identical (same algorithm).

Conclusion: the recompile is a real but partial win — only jt9's non-FFT deep-
search Fortran vectorizes; the FFTs were already AVX via libfftw3f. Even
optimized, jt9-deep ~5s/slot vs ft8_lib 0.03s (~150x). The gap is algorithmic
(jt9 finds 2-3x more by doing more work), not a build artifact. A pure-C rewrite
(as done for wsprd) would target the same deep-search kernels that just gained
1.5x from vectorization, so likely similar-order additional headroom, not a step
change. Optimized jt9 kept at build-opt/jt9 for further work.

## Live-signal sensitivity (our antenna, 2026-07-22) — much bigger than reference
On live slots jt9-deep finds 2-3x ft8_lib on busy bands:
  20m 14074000: ft8_lib 15 vs jt9-d3 45 (3.0x); next slot 14 vs 41.
  40m 7074000:  3 vs 5;  15m 21074000: 1 vs 4;  30m 10136000: 0-2 vs 4.
Harness: capture-live.sh (pcmrecord -j → sox float32->int16 slot-aligned) + compare.sh.

## FFTW wisdom experiment (2026-07-22) — the real FFTW-bound lever
WSJT-X defaults to FFTW_ESTIMATE (four2a.f90:62) — suboptimal plans. This is
the SAME lever radiod uses for its 2-3x (high-patience wisdom for the CPU).

- jt9: ALREADY imports/exports <data_dir>/jt9_wisdom.dat (jt9.f90:242,504);
  patience via -w 0..4 (default 1=ESTIMATE_PATIENT). Measured (opt jt9, live 20m
  d3, min-of-3): patience-1 no-wisdom 5.37s -> MEASURE(-w2) wisdom 4.48s = 1.2x.
  PATIENT/EXHAUSTIVE wisdom (generated once, minutes-hours) should give more,
  per radiod experience. Wisdom is per-CPU: generate on the target, not shipped.
- wsprd: hardcodes `#define PATIENCE FFTW_ESTIMATE` (wsprd.c:52) and imports NO
  wisdom — on a 1.47M-point FFT (nfft1=46080*32). It CANNOT benefit from wisdom
  today; needs a small patch (import/export system wisdom + raise PATIENCE), then
  it gets the same FFTW-plan speedup. This is likely where a real wsprd win lives
  (the compile-flag recompile gave ~0 precisely because ESTIMATE plans dominate).

## SYNTHESIS — three orthogonal levers
1. Compile flags (-march=native -O3, drop -fbounds-check): jt9-deep 1.5x,
   wsprd ~0, jt9-fast ~0 (FFT-bound; FFTW already AVX at runtime).
2. FFTW wisdom (high patience, per-CPU, generated once — the radiod method):
   jt9 +1.2x at MEASURE (more at PATIENT); wsprd needs a code patch to use it.
3. Version: build current 3.0.2 (Debian ships 2.7.0), both decoders.
Best case stacked: jt9-deep native+wisdom ~2x vs stock; wsprd needs the wisdom
patch to move at all. None closes the algorithmic 2-3x-more-work gap vs ft8_lib.

## Proposed Sigmond integration (mirror _install_radiod_native + radiod wisdom)
A `wsjtx-decoders` build recipe: clone v3.0.2, strip -fbounds-check, cmake
-march=native -O3, build jt9+wsprd, install /usr/local/bin (shadow apt); THEN a
one-time high-patience wisdom-gen step on the target host (like radiod's), placing
jt9_wisdom.dat + patching wsprd to load system wisdom. Replaces the `wsjtx` apt
dep in wspr-recorder/psk-recorder deploy.toml. Stage + test on B3 before standard.

## wsprd wisdom patch + PROFILE (2026-07-22) — corrects the "FFT-bound" premise
Patched wsprd PATIENCE ESTIMATE->PATIENT/MEASURE (wisdom import/export already
wired to <data_dir>/wspr_wisdom.dat). Result: MEASURE+wisdom 1.27s vs stock
ESTIMATE 1.23s = NO speedup. (PATIENT unusable: >5min to plan the 1.47M
mixed-radix FFT.) Reverted.

perf profile of wsprd (root, 7537 samples) — the real hot path:
  fano (Fano sequential decoder)  54.7%
  sync_and_demodulate             23.1%
  main                            15.5%
  libfftw3f (ALL ffts)            ~0.5%   <-- NOT FFT-bound
=> wsprd is Fano/sync-bound, not FFT-bound. My earlier "FFT-bound" call was an
unprofiled assumption and was WRONG. Neither compile flags nor FFTW wisdom help
wsprd because <1% of its time is FFT. The radiod 2-3x wisdom win does NOT
transfer: radiod does continuous large FFTs every block; wsprd does one FFT +
Fano decode. fano is serial/branchy -> poor SIMD target too.

CORRECTED SYNTHESIS
- wsprd: no meaningful gain from build flags OR wisdom (algorithm-bound: Fano).
  Any real wsprd speedup would require algorithmic work (or the pure-C rewrite,
  but the bottleneck fano is already C, so a rewrite mainly buys maintainability
  not speed). Prior "wsprd much faster when recompiled" not reproducible here.
- jt9: build flags 1.5x on DEEP (deep-search Fortran vectorizes); wisdom +1.2x
  (jt9 has proportionally more FFT work than wsprd). Real but modest.
- The "build optimized from source, standard" case rests on CONTROL /
  REPRODUCIBILITY / current-version / jt9-deep-1.5x — NOT on a wsprd speedup and
  NOT on FFTW wisdom for these decoders.

## CORRECTION (2026-07-22) — from-source wsprd parity + the -fbounds-check trap
The earlier "wsprd recompile ~0 speedup, ~1.2s parity" line was measured
misleadingly. LIVE parity on a real 20m WSPR window (captured off B4 radiod,
group 239.23.78.83, window 260722_1918), production args `-c -C 500 -o 4 -f`:

  Debian 2.7.0                         2.71s  6 spots
  3.0.2 stock (-O3 -fbounds-check)     0.83s  6 spots   OK
  3.0.2 -march=native + bounds-check   0.73s  6 spots   OK (fastest)
  3.0.2 -march=native, bounds STRIPPED 69.30s 0 spots   BROKEN

=> Stripping -fbounds-check (which our _build_wsjtx_decoders draft did, thinking
it debug cruft) MISCOMPILES wsprd's WSPR Fortran under -O3/-march=native: 0
decodes, ~70s spin. -fbounds-check inhibits the offending vectorization at ~no
cost. jt9's FT8 path did NOT hit this (jt9 decoded fine stripped) — so the
build-only "it runs + has AVX2" check missed it; only a live DECODE-parity test
caught it. Fix (smd 634df7d): keep -fbounds-check, keep native flags. wsprd is
then correct AND slightly faster than Debian. NOTE: jt9's 1.5x deep-decode gain
was measured with bounds-check STRIPPED; re-measure jt9 with it kept before
claiming that speedup for the shipped build.

## FINAL live benchmark (2026-07-22) — shipped config = native + -fbounds-check KEPT
jt9 DEEP (-8 -d 3), live 20m FT8 slots, min-of-5, cores 2-13:
  slot(dec)     Debian2.7.0   native-STRIPPED   native-KEPT(ship)
  20:08:00      5.94s/26      4.88s/29          4.81s/29
  20:07:45      4.42s/23      3.93s/22          4.00s/22
  20:08:15      5.76s/17      4.74s/20          4.53s/20
=> KEPT ≈ STRIPPED (±2%, noise; identical decode counts) — keeping -fbounds-check
   costs jt9 NOTHING. Shipped jt9 vs Debian = ~1.2x faster on live deep decode
   (NOT 1.5x — earlier figure was optimistic). Debian-vs-native decode-count
   differences are the 2.7.0->3.0.2 VERSION, not the flags (strip==kept counts).

wsprd, live 20m WSPR window, production args -c -C 500 -o 4, min-of-3:
  Debian 2.7.0                1.55s/6spots
  our 3.0.2 (native+bounds)   0.77s/6spots   => ~2x faster, same 6 spots
=> BUT the ~2x is the VERSION (3.0.2 vs 2.7.0): stock-3.0.2 = 0.83s ≈ native 0.77s.
   -march=native adds ~nothing to wsprd (Fano-bound, as profiled). The wsprd win
   is "current version -> 2x faster + same accuracy", a free byproduct of 3.0.2,
   not an engineered optimization.

NET (shipped config, live-validated): wsprd correct + ~2x faster (version); jt9
correct + ~1.2x faster (native); keeping -fbounds-check has no cost and is
required for wsprd. Fix in smd 634df7d.
