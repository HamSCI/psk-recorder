"""SlotWorker: extracts cadence-aligned WAV slots and invokes the decoder.

One SlotWorker per channel. Runs as a daemon thread, polling the ring
buffer every 500 ms for completed slots.

The decoder backend (selected by ``decoder_kind``):

  * ``"decode_ft8"`` — ka9q/ft8_lib's decoder.  Default.  Streams
    its line-format output directly to stdout (we attach the log
    fd).  Reports an internal "score" (not a calibrated dB SNR).

The decoder appends to the ``<radiod>-<mode>.log`` file in its native
format; ``ch_tailer.parse_decoder_line`` parses each line.

FT8 cadence: 15 s (slots at :00, :15, :30, :45)
FT4 cadence: 7.5 s (slots at :00, :07.5, :15, :22.5, :30, :37.5, :45, :52.5)
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from ka9q import SlotClock, SlotClockDesyncError

from psk_recorder.core.ring import Ring
from psk_recorder.core.wav import write_wav, _float32_to_int16

logger = logging.getLogger(__name__)

SETTLE_SEC = 1.5

# A hung decode_ft8 (e.g. on a corrupt WAV) would otherwise sit in
# _pending_procs forever, leaking its two stdio FDs + the spool WAV.
# decode_ft8 finishes in well under a second on a 15 s/7.5 s slot, so any
# proc still alive after this deadline is killed.  Generous (4x the FT8
# cadence) to avoid false kills under top-of-minute CPU contention.
DECODE_TIMEOUT_SEC = 60.0

# decoder_kind values accepted by SlotWorker.
DECODER_FT8_LIB = "decode_ft8"
DECODER_JT9 = "jt9"
VALID_DECODER_KINDS = (DECODER_FT8_LIB, DECODER_JT9)

# How long to wait for jt9_decode to drain + decode its last fed slot on stop
# before terminating it (a deep FT8 slot is a few seconds; be generous).
JT9_STOP_DRAIN_SEC = 10.0

# ─── wall-clock slot guard defaults ──────────────────────────────────────
# A complete slot labeled start_utc with cadence C cannot finish filling
# before wall time start_utc+C — samples arrive in real time.  A harvest
# earlier than that (beyond jitter) means the RTP→UTC anchor runs AHEAD
# of true UTC; with a gross fault (minutes) there are zero decodes, so
# no dt-based guard can see it.  Observed B4 2026-07-23: recorders that
# anchored during radiod startup ran +10 min ahead — every slot silent,
# fleet watchdog inert (single-instance host has no decoding peer).
# Env: PSK_WALLCLOCK_GUARD_SEC (<=0 disables), PSK_WALLCLOCK_GUARD_STRIKES.
WALLCLOCK_GUARD_SEC_DEFAULT = 5.0
WALLCLOCK_GUARD_STRIKES_DEFAULT = 3


def _wallclock_guard_env() -> tuple[float, int]:
    try:
        threshold = float(os.environ.get(
            "PSK_WALLCLOCK_GUARD_SEC", str(WALLCLOCK_GUARD_SEC_DEFAULT)))
    except ValueError:
        threshold = WALLCLOCK_GUARD_SEC_DEFAULT
    try:
        strikes = int(os.environ.get(
            "PSK_WALLCLOCK_GUARD_STRIKES",
            str(WALLCLOCK_GUARD_STRIKES_DEFAULT)))
    except ValueError:
        strikes = WALLCLOCK_GUARD_STRIKES_DEFAULT
    return threshold, max(1, strikes)


def _build_jt9_log_line(
    jt9_line: str, slot_utc: float, frequency_hz: int, mode: str,
) -> Optional[str]:
    """Normalize one resident-jt9 stdout decode line into the canonical jt9
    log line that ``ch_tailer.parse_jt9_line`` parses.

    jt9's stdout decode line is::

        HHMMSS  SNR  DT  FREQ_OFFSET ~  MESSAGE

    Its HHMMSS is jt9's own (wall-clock) label and is discarded — psk-recorder
    stamps the authoritative slot UTC (``slot_utc``) and supplies the channel
    dial (``frequency_hz``), so jt9 contributes only the relative snr / dt /
    audio-frequency-offset / message.  Emits::

        YYMMDD HHMMSS BAND_FREQ_HZ SYNC SNR DT FREQ_OFFSET MARKER MESSAGE… MODE

    with SYNC=0 (jt9 stdout carries no sync column) and MARKER='~'.  Returns
    ``None`` on an unrecognised line so the caller skips control/diagnostic
    output.
    """
    if slot_utc is None or "~" not in jt9_line:
        return None
    head, _, message = jt9_line.partition("~")
    parts = head.split()
    # jt9: HHMMSS(0) SNR(1) DT(2) FREQ_OFFSET(3)
    if len(parts) < 4:
        return None
    snr, dt, freq_offset = parts[1], parts[2], parts[3]
    message = message.strip()
    if not message:
        return None
    t = time.gmtime(int(math.floor(slot_utc)))
    yymmdd = time.strftime("%y%m%d", t)
    hhmmss = time.strftime("%H%M%S", t)
    return (
        f"{yymmdd} {hhmmss} {frequency_hz} 0 {snr} {dt} {freq_offset} "
        f"~ {message} {mode.upper()}"
    )


class SlotWorker:
    """Extracts cadence-aligned audio slots from a Ring and decodes them."""

    def __init__(
        self,
        ring: Ring,
        mode: str,
        frequency_hz: int,
        cadence_sec: float,
        spool_dir: Path,
        log_fd,
        decoder_path: str,
        clock: SlotClock,
        get_latest_rtp: Callable[[], Optional[int]],
        clock_lock: threading.Lock,
        get_anchor_utc_now: Callable[[], Optional[float]],
        keep_wav: bool = False,
        decoder_kind: str = DECODER_FT8_LIB,
        spool_spots: bool = False,
        decoder_depth: int = 3,
        on_timing_fault: Optional[Callable[[float], None]] = None,
        on_desync: Optional[Callable[[], None]] = None,
    ):
        if decoder_kind not in VALID_DECODER_KINDS:
            raise ValueError(
                f"decoder_kind must be one of {VALID_DECODER_KINDS}; "
                f"got {decoder_kind!r}"
            )
        self._ring = ring
        self._mode = mode
        self._frequency_hz = frequency_hz
        self._cadence_sec = cadence_sec
        self._spool_dir = spool_dir
        self._log_fd = log_fd
        self._decoder_path = decoder_path
        # Epoch-aligned, RTP-referenced slot timing (shared ka9q.SlotClock).
        # The clock is anchored by ChannelSink.on_samples off the GPS-true RTP
        # timestamp; this worker only harvests completed slots and extracts
        # their exact sample windows by absolute offset.
        self._clock = clock
        self._get_latest_rtp = get_latest_rtp
        self._clock_lock = clock_lock
        # Returns the CURRENT UTC of the (fixed) SlotClock anchor_rtp per
        # radiod's live rtp_to_utc + authority offset.  We re-pin every slot's
        # RTP window to this each tick, so the windows follow radiod's slow
        # RTP↔UTC slide instead of freezing — without the per-batch re-anchor
        # storm (this is a smooth, sub-sample nudge once the grid is running).
        self._get_anchor_utc_now = get_anchor_utc_now
        # Next clean cadence-multiple UTC boundary to emit (None until first).
        self._next_boundary_utc: Optional[float] = None
        self._sr = clock.sample_rate
        self._decoder_kind = decoder_kind
        self._decoder_depth = decoder_depth
        self._keep_wav = keep_wav
        self._spool_spots = spool_spots
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Each entry: (proc, wav_path, slot_start_utc, fork_monotonic).
        self._pending_procs: list[tuple[subprocess.Popen, Path,
                                        float, float]] = []
        # --- resident jt9 state (decoder_kind == DECODER_JT9) -----------------
        # One long-lived `jt9_decode -T` per (band, mode) — this worker.  Slots
        # are fed to its stdin in cadence order; it decodes each in order and
        # emits exactly one terminal <DecodeStats> per slot.  We map decode
        # lines to the slot that produced them purely by ORDER: _jt9_pending is
        # the FIFO of authoritative slot UTCs we fed (appended by the harvest
        # loop, popped by the reader thread on each <DecodeStats>).  Keeping
        # jt9 resident is what preserves its in-RAM compound-callsign hash
        # table across slots (see docs/jt9-decoder.md §2).
        self._jt9_proc: Optional[subprocess.Popen] = None
        self._jt9_reader: Optional[threading.Thread] = None
        self._jt9_pending: deque[float] = deque()
        self._jt9_restarts = 0
        # Wall-clock slot guard (module docstring above _wallclock_guard_env):
        # strikes count consecutive impossibly-early harvests; firing invokes
        # on_timing_fault (ChannelSink re-anchor) — detect + alarm + recover.
        self._wallclock_threshold, self._wallclock_max_strikes = (
            _wallclock_guard_env())
        self._wallclock_strikes = 0
        self._on_timing_fault = on_timing_fault
        self._on_desync = on_desync
        # Counters read by the recorder's stats thread. int ops are atomic
        # under CPython GIL; no lock needed for the single-reader case.
        self.decodes_ok = 0
        self.decodes_fail = 0
        self.slots_empty = 0

    def reset_boundary(self) -> None:
        """Drop the cached next-boundary UTC so the worker re-seeds at the new
        leading edge.  Called by ChannelSink.on_stream_restored after a genuine
        radiod restart re-anchors the clock to a fresh RTP reference."""
        self._next_boundary_utc = None

    def start(self) -> None:
        self._running = True
        if self._decoder_kind == DECODER_JT9:
            self._start_jt9_process()
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f"slot-{self._mode}-{self._frequency_hz}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._decoder_kind == DECODER_JT9:
            self._stop_jt9_process()
        else:
            self._reap_all(wait=True)

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("SlotWorker tick error")
            time.sleep(0.5)

    def _tick(self) -> None:
        # decode_ft8 forks one proc per slot to reap here; jt9 uses a resident
        # process (no per-slot procs to reap).
        if self._decoder_kind != DECODER_JT9:
            self._reap_finished()

        latest_rtp = self._get_latest_rtp()
        if latest_rtp is None:
            return
        # Current UTC of the FIXED anchor_rtp, per radiod's live rtp_to_utc.
        # This is what lets the grid FOLLOW radiod's RTP↔UTC slide: anchor_rtp
        # never moves (so ring offsets stay valid), but its UTC — and thus the
        # RTP offset of each clean cadence boundary — tracks radiod every tick.
        anchor_utc_now = self._get_anchor_utc_now()
        if anchor_utc_now is None:
            return

        cadence_samples = self._clock.cadence_samples
        settle_samples = self._clock.settle_samples
        harvested: list[tuple[int, float]] = []
        try:
            with self._clock_lock:
                if not self._clock.anchored:
                    return
                latest_off = self._clock.offset_of_rtp(latest_rtp)
                # Seed the next boundary at the first clean cadence multiple at/after
                # the STREAM START (anchor_rtp is the first sample, so anchor_utc_now
                # ~ the stream-start UTC).  A stream that starts mid-slot correctly
                # begins at the next clean boundary, skipping the partial slot.
                if self._next_boundary_utc is None:
                    self._next_boundary_utc = (
                        math.ceil(anchor_utc_now / self._cadence_sec) * self._cadence_sec
                    )
                # Harvest each completed clean slot, computing its RTP window offset
                # from radiod's CURRENT mapping (anchor_utc_now) — not a frozen grid.
                while True:
                    start_off = round(
                        (self._next_boundary_utc - anchor_utc_now) * self._sr
                    )
                    if latest_off < start_off + cadence_samples + settle_samples:
                        break
                    harvested.append((start_off, self._next_boundary_utc))
                    self._next_boundary_utc += self._cadence_sec
        except SlotClockDesyncError as exc:
            logger.error(
                "%s %d Hz: SlotClock desync in harvest — %s; requesting "
                "anchor reset (audit F18)",
                self._mode.upper(), self._frequency_hz, exc,
            )
            if self._on_desync is not None:
                self._on_desync()
            return

        for start_off, start_utc in harvested:
            samples = self._ring.extract_by_offset(start_off, cadence_samples)
            if samples is None:
                self.slots_empty += 1
                logger.warning(
                    "%s %d Hz: slot at %.1f — insufficient samples, skipping",
                    self._mode.upper(), self._frequency_hz, start_utc,
                )
                continue
            if self._wallclock_guard(start_utc):
                # Fired: the sink dropped the anchor + ring — remaining
                # harvested offsets are from the dead reference space.
                break
            if self._decoder_kind == DECODER_JT9:
                self._feed_jt9(samples, start_utc)
            else:
                wav_path = self._write_spool_wav(samples, start_utc)
                self._fork_decoder(wav_path, start_utc)

    def _wallclock_guard(self, start_utc: float) -> bool:
        """Wall-clock slot guard step for one COMPLETE harvested slot.

        Returns True when the guard fired (anchor dropped via
        on_timing_fault) so the harvest loop stops consuming offsets
        from the now-dead reference space.  A plausible slot clears the
        strikes; late slots (harvest backlog) are plausible by
        construction — only the impossible early direction strikes.
        """
        if self._wallclock_threshold <= 0:
            return False
        early_by = (start_utc + self._cadence_sec) - time.time()
        if early_by <= self._wallclock_threshold:
            self._wallclock_strikes = 0
            return False
        self._wallclock_strikes += 1
        if self._wallclock_strikes < self._wallclock_max_strikes:
            logger.warning(
                "wallclock-guard %s %d Hz: slot at %.1f completed %.1fs "
                "before its nominal end [strike %d/%d]",
                self._mode.upper(), self._frequency_hz, start_utc,
                early_by, self._wallclock_strikes,
                self._wallclock_max_strikes,
            )
            return False
        self._wallclock_strikes = 0
        logger.error(
            "TIMING FAULT %s %d Hz: slots completing %.1fs BEFORE their "
            "nominal end — physically impossible unless the RTP→UTC "
            "anchor runs ahead of true UTC (anchor grabbed during radiod "
            "startup?); dropping anchor to re-anchor from radiod's live "
            "mapping; INVESTIGATE radiod restart/startup timing",
            self._mode.upper(), self._frequency_hz, early_by,
        )
        if self._on_timing_fault is not None:
            try:
                self._on_timing_fault(early_by)
            except Exception:
                logger.exception(
                    "%s %d Hz: on_timing_fault recovery failed",
                    self._mode.upper(), self._frequency_hz,
                )
        return True

    def _write_spool_wav(self, samples, slot_start_utc: float) -> Path:
        # Filename HHMMSS must be an integer second AND must parse via
        # ka9q/ft8_lib's `sscanf("%04d%02d%02d%c%02d%02d%02d", ...)`.
        # Three constraints together:
        #
        #   1. 4-digit year — otherwise the parse fails and decode_ft8
        #      falls back to file mod time → bogus +2.5 s dt bias.
        #
        #   2. For FT8 (integer-second slot boundaries :00/:15/:30/:45),
        #      use slot_start_utc as-is.  dt centers near 0.
        #
        #   3. For FT4 half-second slots (:07.5, :22.5, :37.5, :52.5),
        #      use math.ceil(slot_start_utc) — round UP to the next
        #      integer second.  Empirically this puts decode_ft8's FT4
        #      grid alignment 0.5 s past the true slot boundary, which
        #      it tolerates and reports as dt ≈ +1.0 s.  If we
        #      truncate instead (the strftime default), decode_ft8
        #      aligns to the WRONG grid point and reports dt ≈ +7.5 s
        #      (a full FT4 cadence period off).  Validated on B4-100
        #      2026-05-11 by renaming the same .wav with different
        #      second values: floor→+7.5, ceil→+1.0.
        #
        # WAV content is still extracted at the true slot_start_utc.
        # Only the FILENAME label is rounded.
        ceiled = int(math.ceil(slot_start_utc))
        slot_time = time.gmtime(ceiled)
        freq_khz = self._frequency_hz // 1000
        filename = time.strftime("%Y%m%d_%H%M%S", slot_time) + f"_{freq_khz}.wav"
        wav_path = self._spool_dir / filename

        write_wav(
            path=wav_path,
            samples=samples,
            sample_rate=self._ring.sample_rate,
            frequency_hz=self._frequency_hz,
        )
        return wav_path

    def _fork_decoder(self, wav_path: Path, slot_start_utc: float) -> None:
        self._fork_decoder_ft8_lib(wav_path, slot_start_utc)

    def _fork_decoder_ft8_lib(self, wav_path: Path, slot_start: float) -> None:
        """ka9q/ft8_lib decode_ft8 — captures stdout for tee on reap.

        CLI: ``decode_ft8 -f <freq_mhz> [-4 for FT4] <wav_path>``.
        Output format (per ft8_lib decode_ft8.c:363):
            YYYY/MM/DD HH:MM:SS  SCORE  DT  FREQ_HZ  ~  MESSAGE

        decode_ft8 emits its output as a single burst at decode end
        (~3KB for a busy slot), well below PIPE_BUF — capturing via
        PIPE doesn't block.  Reap reads stdout, writes to the per-mode
        log, and (when ``spool_spots``) tees a per-slot ``.spots.txt``
        file alongside the wav for the hs-uploader file fallback path.
        """
        freq_mhz = self._frequency_hz / 1e6
        cmd = [self._decoder_path, "-f", f"{freq_mhz:.6f}"]
        if self._mode == "ft4":
            cmd.append("-4")
        cmd.append(str(wav_path))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._pending_procs.append((proc, wav_path, slot_start, time.monotonic()))
            logger.debug(
                "%s %d Hz: decode_ft8 pid=%d on %s",
                self._mode.upper(), self._frequency_hz, proc.pid, wav_path.name,
            )
        except OSError as exc:
            logger.error("Failed to launch decode_ft8: %s", exc)
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)

    @staticmethod
    def _kill_proc(proc: subprocess.Popen) -> None:
        """Kill a hung decoder and free its zombie + stdio FDs immediately."""
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)  # reap the zombie
        except (subprocess.TimeoutExpired, OSError):
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _reap_finished(self) -> None:
        now = time.monotonic()
        still_pending = []
        for proc, wav_path, slot_start, fork_mono in self._pending_procs:
            ret = proc.poll()
            if ret is None:
                # Bound the leak: a proc still alive after DECODE_TIMEOUT_SEC
                # is hung.  Left here it leaks its two stdio FDs + the spool
                # WAV forever; across ~19 channels that grows until the
                # MemoryMax cgroup OOM-kills the daemon and Restart=always
                # re-enters the same state.  Kill, count a failure, drop it.
                if now - fork_mono > DECODE_TIMEOUT_SEC:
                    logger.warning(
                        "%s %d Hz: decode_ft8 pid=%d on %s exceeded %.0fs "
                        "deadline — killing (hung decode)",
                        self._mode.upper(), self._frequency_hz, proc.pid,
                        wav_path.name, DECODE_TIMEOUT_SEC,
                    )
                    self.decodes_fail += 1
                    self._kill_proc(proc)
                    if not self._keep_wav:
                        wav_path.unlink(missing_ok=True)
                    continue
                still_pending.append((proc, wav_path, slot_start, fork_mono))
                continue
            if ret == 0:
                self.decodes_ok += 1
                self._materialise_decode_ft8_output(proc, wav_path)
            else:
                self.decodes_fail += 1
                stderr = proc.stderr.read().decode(errors="replace").strip()[:200]
                logger.warning(
                    "decode_ft8 exit %d for %s: %s",
                    ret, wav_path.name, stderr,
                )
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)
        self._pending_procs = still_pending

    def _reap_all(self, wait: bool = False) -> None:
        for proc, wav_path, slot_start, _fork_mono in self._pending_procs:
            if wait:
                try:
                    proc.wait(timeout=5.0)
                    if proc.returncode == 0:
                        self._materialise_decode_ft8_output(proc, wav_path)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)
        self._pending_procs.clear()

    def _materialise_decode_ft8_output(
        self, proc: subprocess.Popen, wav_path: Path,
    ) -> None:
        """Read decode_ft8's captured stdout, write to log + per-slot spool.

        decode_ft8 writes WSJT-X-style lines (``YYYY/MM/DD HH:MM:SS …``)
        to stdout.  We capture via PIPE (see ``_fork_decoder_ft8_lib``)
        so we can fan out to both the per-mode log file (the legacy
        path ChTailer reads) and a per-slot ``.spots.txt`` file used by
        the hs-uploader file-fallback FileTreeSource.
        """
        try:
            data = proc.stdout.read() if proc.stdout is not None else b""
        except (OSError, ValueError):
            return
        if not data:
            return
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return

        lines = [ln + "\n" for ln in text.splitlines() if ln.strip()]
        if not lines:
            return

        try:
            for ln in lines:
                self._log_fd.write(ln)
            self._log_fd.flush()
        except OSError as exc:
            logger.warning(
                "%s: failed appending decode_ft8 output to log: %s",
                self._mode.upper(), exc,
            )

        if self._spool_spots:
            # decode_ft8 lines carry the slot's wallclock; the per-slot
            # file mirrors the wav_path so a FileTreeSource glob picks
            # them up alongside.
            spots_path = wav_path.with_suffix(".spots.txt")
            try:
                spots_path.parent.mkdir(parents=True, exist_ok=True)
                with open(spots_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
            except OSError as exc:
                logger.warning(
                    "%s: failed writing per-slot spots file %s: %s",
                    self._mode.upper(), spots_path, exc,
                )

    # ----- resident jt9 (decoder_kind == DECODER_JT9) ------------------------

    def _start_jt9_process(self) -> None:
        """Spawn the long-lived ``jt9_decode -T`` for this (band, mode).

        The jt9 binary is the sibling of the wrapper (both installed to
        /usr/local/bin by sigmond's wsjtx-decoders build).  Reads decode output
        on a daemon reader thread bound to this specific process, so a restart's
        new reader owns the new process and the old one exits on its EOF.
        """
        wrapper = self._decoder_path
        jt9_bin = str(Path(wrapper).with_name("jt9"))
        mode_arg = "FT4" if self._mode == "ft4" else "FT8"
        cmd = [wrapper, "-j", jt9_bin, "-m", mode_arg,
               "-d", str(self._decoder_depth), "-T"]
        try:
            self._jt9_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            logger.error("%s %d Hz: failed to start jt9_decode %s: %s",
                         self._mode.upper(), self._frequency_hz, cmd, exc)
            self._jt9_proc = None
            return
        self._jt9_reader = threading.Thread(
            target=self._jt9_reader_loop, args=(self._jt9_proc,),
            daemon=True, name=f"jt9-read-{self._mode}-{self._frequency_hz}",
        )
        self._jt9_reader.start()
        logger.info(
            "%s %d Hz: resident jt9_decode pid=%d (%s -d%d -T, jt9=%s)",
            self._mode.upper(), self._frequency_hz, self._jt9_proc.pid,
            mode_arg, self._decoder_depth, jt9_bin,
        )

    def _feed_jt9(self, samples, slot_start_utc: float) -> None:
        """Write one cadence-aligned slot's PCM to the resident jt9_decode.

        The extracted window is exactly cadence_samples — one jt9_decode cycle
        (FT8 180000 / FT4 90000 at 12 kHz).  We push the authoritative slot UTC
        onto the FIFO first so the reader can stamp decodes as they return.
        """
        proc = self._jt9_proc
        if proc is None or proc.poll() is not None:
            self._restart_jt9()
            proc = self._jt9_proc
            if proc is None or proc.poll() is not None:
                self.decodes_fail += 1
                return
        pcm = _float32_to_int16(samples).tobytes()
        self._jt9_pending.append(slot_start_utc)
        try:
            proc.stdin.write(pcm)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            logger.warning(
                "%s %d Hz: jt9_decode stdin write failed: %s — restarting",
                self._mode.upper(), self._frequency_hz, exc,
            )
            try:
                self._jt9_pending.pop()   # the slot we just failed to feed
            except IndexError:
                pass
            self.decodes_fail += 1
            self._restart_jt9()
            return
        if self._keep_wav:
            self._write_spool_wav(samples, slot_start_utc)

    def _jt9_reader_loop(self, proc: subprocess.Popen) -> None:
        """Read `proc`'s stdout, normalize decode lines to the canonical jt9 log
        line, and pop the FIFO on each terminal <DecodeStats>.

        jt9_decode processes slots strictly in fed order and emits exactly one
        <DecodeStats cycle_num=N …> per slot, so the FIFO front is always the
        slot the current decode lines belong to — no cycle_num arithmetic
        needed (which would break across restarts anyway).
        """
        stream = proc.stdout
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("<DecodeStats>"):
                if self._jt9_pending:
                    try:
                        self._jt9_pending.popleft()
                    except IndexError:
                        pass
                # watchdog=1 → jt9 hung on that slot (counted a failure).
                if "watchdog=1" in line:
                    self.decodes_fail += 1
                else:
                    self.decodes_ok += 1
                continue
            if line.startswith("<"):
                continue   # <DecodeFinished> / other control lines
            slot_utc = self._jt9_pending[0] if self._jt9_pending else None
            canonical = _build_jt9_log_line(
                line, slot_utc, self._frequency_hz, self._mode,
            )
            if canonical is None:
                continue
            try:
                self._log_fd.write(canonical + "\n")
                self._log_fd.flush()
            except OSError as exc:
                logger.warning("%s %d Hz: jt9 log write failed: %s",
                               self._mode.upper(), self._frequency_hz, exc)
        # stdout closed → the resident process exited.  _feed_jt9 will restart
        # it on the next slot while we're still running.
        if self._running:
            logger.warning(
                "%s %d Hz: jt9_decode stdout closed (process exited)",
                self._mode.upper(), self._frequency_hz,
            )

    def _restart_jt9(self) -> None:
        """Tear down a dead/wedged jt9_decode and start a fresh one.

        The new process starts with an empty hash table (compound-call
        resolution resets) — logged, per docs/jt9-decoder.md §2.  Any slots we
        fed the old process that were never acked are dropped: without their
        <DecodeStats> the FIFO would desync, so we clear it.
        """
        self._jt9_restarts += 1
        logger.warning(
            "%s %d Hz: restarting jt9_decode (#%d) — hash table resets; "
            "%d unacked slot(s) dropped",
            self._mode.upper(), self._frequency_hz, self._jt9_restarts,
            len(self._jt9_pending),
        )
        self._jt9_pending.clear()
        old = self._jt9_proc
        self._jt9_proc = None
        if old is not None:
            try:
                if old.stdin:
                    old.stdin.close()
            except OSError:
                pass
            try:
                old.kill()
            except OSError:
                pass
        self._start_jt9_process()

    def _stop_jt9_process(self) -> None:
        """Close stdin so jt9_decode drains + decodes its last slot, then exits.

        Called from stop() after the harvest loop thread has joined, so no
        _feed_jt9 races this.
        """
        proc = self._jt9_proc
        self._jt9_proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=JT9_STOP_DRAIN_SEC)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._jt9_reader is not None:
            self._jt9_reader.join(timeout=5.0)
