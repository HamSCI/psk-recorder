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
from pathlib import Path
from typing import Callable, Optional

from ka9q import SlotClock

from psk_recorder.core.ring import Ring
from psk_recorder.core.wav import write_wav

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
VALID_DECODER_KINDS = (DECODER_FT8_LIB,)


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
        keep_wav: bool = False,
        decoder_kind: str = DECODER_FT8_LIB,
        spool_spots: bool = False,
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
        self._decoder_kind = decoder_kind
        self._keep_wav = keep_wav
        self._spool_spots = spool_spots
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Each entry: (proc, wav_path, slot_start_utc, fork_monotonic).
        self._pending_procs: list[tuple[subprocess.Popen, Path,
                                        float, float]] = []
        # Counters read by the recorder's stats thread. int ops are atomic
        # under CPython GIL; no lock needed for the single-reader case.
        self.decodes_ok = 0
        self.decodes_fail = 0
        self.slots_empty = 0

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f"slot-{self._mode}-{self._frequency_hz}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        self._reap_all(wait=True)

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("SlotWorker tick error")
            time.sleep(0.5)

    def _tick(self) -> None:
        self._reap_finished()

        latest_rtp = self._get_latest_rtp()
        if latest_rtp is None:
            return

        # Harvest every epoch-aligned slot that has fully arrived.  The clock
        # is shared with on_samples (which anchors / re-anchors it), so guard
        # all clock state with the lock.
        with self._clock_lock:
            if not self._clock.anchored:
                return
            slots = self._clock.advance(latest_rtp)
            # Resolve each slot's absolute ring offset while we hold the lock
            # (offset_of_rtp reads the anchor).
            resolved = [
                (self._clock.offset_of_rtp(s.start_rtp), s) for s in slots
            ]

        for start_off, slot in resolved:
            samples = self._ring.extract_by_offset(start_off, slot.n_samples)
            if samples is None:
                self.slots_empty += 1
                logger.warning(
                    "%s %d Hz: slot %d at %.1f — insufficient samples, skipping",
                    self._mode.upper(), self._frequency_hz,
                    slot.index, slot.start_utc,
                )
                continue
            wav_path = self._write_spool_wav(samples, slot.start_utc)
            self._fork_decoder(wav_path, slot.start_utc)

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
