"""SlotWorker: extracts cadence-aligned WAV slots and invokes the decoder.

One SlotWorker per channel. Runs as a daemon thread, polling the ring
buffer every 500 ms for completed slots.

Two decoder backends are supported (selected by ``decoder_kind``):

  * ``"jt9"`` — WSJT-X's canonical FT4/FT8 decoder.  Default.  Reports
    calibrated dB SNR, time-offset, frequency-offset, decoded message
    text, and a spectral-width metric.  Writes to ``<-a tmpdir>/
    decoded.txt`` per invocation; we read the file post-exit and
    append WSJT-X canonical lines to the per-mode log.
  * ``"decode_ft8"`` — fallback to ka9q/ft8_lib's decoder.  Streams
    its line-format output directly to stdout (we attach the log
    fd).  Reports an internal "score" (not a calibrated dB SNR).
    Used when jt9 is unavailable or as an explicit operator opt-out.

Both backends append to the same ``<radiod>-<mode>.log`` file in
their native formats; ``ch_tailer.parse_decoder_line`` auto-detects
which by structure.

FT8 cadence: 15 s (slots at :00, :15, :30, :45)
FT4 cadence: 7.5 s (slots at :00, :07.5, :15, :22.5, :30, :37.5, :45, :52.5)
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from psk_recorder.core.ring import Ring
from psk_recorder.core.wav import write_wav

logger = logging.getLogger(__name__)

SETTLE_SEC = 1.5

# decoder_kind values accepted by SlotWorker.
DECODER_JT9 = "jt9"
DECODER_FT8_LIB = "decode_ft8"
VALID_DECODER_KINDS = (DECODER_JT9, DECODER_FT8_LIB)


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
        keep_wav: bool = False,
        decoder_kind: str = DECODER_FT8_LIB,
        decoder_depth: int = 3,
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
        self._decoder_kind = decoder_kind
        self._decoder_depth = decoder_depth
        self._keep_wav = keep_wav
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._next_slot_start: Optional[float] = None
        # Each entry: (proc, wav_path, tmpdir_or_None, slot_start_utc).
        # tmpdir is set only for jt9 (per-slot --data-path); we read its
        # decoded.txt and rmtree it after the process exits.  slot_start
        # is the UTC epoch the slot started at (jt9 emits HHMM only;
        # we prefix the date when we materialise canonical lines).
        self._pending_procs: list[tuple[subprocess.Popen, Path,
                                        Optional[Path], float]] = []
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

        head = self._ring.head_utc()
        if head is None:
            return

        if self._next_slot_start is None:
            self._next_slot_start = self._last_completed_boundary(head)
            logger.info(
                "%s %d Hz: first slot at %.1f (head=%.1f)",
                self._mode.upper(), self._frequency_hz,
                self._next_slot_start, head,
            )
            return

        slot_end = self._next_slot_start + self._cadence_sec
        if head < slot_end + SETTLE_SEC:
            return

        samples = self._ring.extract_slot(
            self._next_slot_start, self._cadence_sec
        )
        if samples is None:
            self.slots_empty += 1
            logger.warning(
                "%s %d Hz: slot at %.1f — insufficient samples, skipping",
                self._mode.upper(), self._frequency_hz, self._next_slot_start,
            )
            self._next_slot_start = slot_end
            return

        wav_path = self._write_spool_wav(samples)
        self._fork_decoder(wav_path)

        self._next_slot_start = slot_end

    def _align_to_cadence(self, utc: float) -> float:
        """Find the next cadence boundary at or after utc."""
        cadence = self._cadence_sec
        return math.ceil(utc / cadence) * cadence

    def _last_completed_boundary(self, head_utc: float) -> float:
        """Find the start of the most recent slot whose end + settle <= head.

        This means: floor((head - settle - cadence) / cadence) * cadence,
        clamped so we never go negative. We start decoding from the most
        recently completed slot, not some future one.
        """
        cadence = self._cadence_sec
        latest_end = head_utc - SETTLE_SEC
        latest_start = latest_end - cadence
        if latest_start < 0:
            return 0.0
        return math.floor(latest_start / cadence) * cadence

    def _write_spool_wav(self, samples) -> Path:
        slot_time = time.gmtime(self._next_slot_start)
        freq_khz = self._frequency_hz // 1000
        filename = time.strftime("%y%m%d_%H%M%S", slot_time) + f"_{freq_khz}.wav"
        wav_path = self._spool_dir / filename

        write_wav(
            path=wav_path,
            samples=samples,
            sample_rate=self._ring.sample_rate,
            frequency_hz=self._frequency_hz,
        )
        return wav_path

    def _fork_decoder(self, wav_path: Path) -> None:
        slot_start = self._next_slot_start or time.time()
        if self._decoder_kind == DECODER_JT9:
            self._fork_decoder_jt9(wav_path, slot_start)
        else:
            self._fork_decoder_ft8_lib(wav_path, slot_start)

    def _fork_decoder_ft8_lib(self, wav_path: Path, slot_start: float) -> None:
        """ka9q/ft8_lib decode_ft8 — streams output directly to log_fd.

        CLI: ``decode_ft8 -f <freq_mhz> [-4 for FT4] <wav_path>``.
        Output format (per ft8_lib decode_ft8.c:363):
            YYYY/MM/DD HH:MM:SS  SCORE  DT  FREQ_HZ  ~  MESSAGE
        """
        freq_mhz = self._frequency_hz / 1e6
        cmd = [self._decoder_path, "-f", f"{freq_mhz:.6f}"]
        if self._mode == "ft4":
            cmd.append("-4")
        cmd.append(str(wav_path))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=self._log_fd,
                stderr=subprocess.PIPE,
            )
            self._pending_procs.append((proc, wav_path, None, slot_start))
            logger.debug(
                "%s %d Hz: decode_ft8 pid=%d on %s",
                self._mode.upper(), self._frequency_hz, proc.pid, wav_path.name,
            )
        except OSError as exc:
            logger.error("Failed to launch decode_ft8: %s", exc)
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)

    def _fork_decoder_jt9(self, wav_path: Path, slot_start: float) -> None:
        """WSJT-X jt9 — writes decoded.txt to a per-slot --data-path tmpdir.

        CLI: ``jt9 [-8|-7] -d <depth> -a <tmpdir> <wav_path>``.
        Each invocation needs its own data-path so concurrent slots
        don't clobber each other's decoded.txt.
        """
        try:
            tmpdir = Path(tempfile.mkdtemp(
                prefix=f"jt9-{self._mode}-",
                dir=str(self._spool_dir),
            ))
        except OSError as exc:
            logger.error("Failed to mkdtemp for jt9: %s", exc)
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)
            return

        mode_flag = "-7" if self._mode == "ft4" else "-8"
        cmd = [
            self._decoder_path,
            mode_flag,
            "-d", str(self._decoder_depth),
            "-a", str(tmpdir),
            str(wav_path),
        ]

        try:
            # cwd=tmpdir: jt9's Fortran runtime writes ./timer.out (and
            # other scratch files) relative to the process CWD.  Without
            # this it inherits the systemd WorkingDirectory (/opt/psk-
            # recorder, root-owned), pskrec can't write there, and the
            # Fortran runtime aborts before producing decoded.txt.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=str(tmpdir),
            )
            self._pending_procs.append((proc, wav_path, tmpdir, slot_start))
            logger.debug(
                "%s %d Hz: jt9 pid=%d on %s (data-path=%s)",
                self._mode.upper(), self._frequency_hz,
                proc.pid, wav_path.name, tmpdir.name,
            )
        except OSError as exc:
            logger.error("Failed to launch jt9: %s", exc)
            shutil.rmtree(tmpdir, ignore_errors=True)
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)

    def _reap_finished(self) -> None:
        still_pending = []
        for proc, wav_path, tmpdir, slot_start in self._pending_procs:
            ret = proc.poll()
            if ret is None:
                still_pending.append((proc, wav_path, tmpdir, slot_start))
                continue
            if ret == 0:
                self.decodes_ok += 1
                if self._decoder_kind == DECODER_JT9 and tmpdir is not None:
                    self._materialise_jt9_output(tmpdir, slot_start)
            else:
                self.decodes_fail += 1
                stderr = proc.stderr.read().decode(errors="replace").strip()[:200]
                kind_name = "jt9" if self._decoder_kind == DECODER_JT9 else "decode_ft8"
                logger.warning(
                    "%s exit %d for %s: %s",
                    kind_name, ret, wav_path.name, stderr,
                )
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)
        self._pending_procs = still_pending

    def _reap_all(self, wait: bool = False) -> None:
        for proc, wav_path, tmpdir, slot_start in self._pending_procs:
            if wait:
                try:
                    proc.wait(timeout=5.0)
                    if (proc.returncode == 0
                            and self._decoder_kind == DECODER_JT9
                            and tmpdir is not None):
                        self._materialise_jt9_output(tmpdir, slot_start)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)
            if not self._keep_wav:
                wav_path.unlink(missing_ok=True)
        self._pending_procs.clear()

    def _materialise_jt9_output(self, tmpdir: Path, slot_start: float) -> None:
        """Read jt9's decoded.txt and append native jt9 lines to the per-mode log.

        Output line shape (psk-recorder canonical, "native pass-through"):

            YYMMDD HHMMSS BAND_FREQ_HZ <jt9 native columns...>

        jt9 v27 ``decoded.txt`` format (one line per decoded packet):

            HHMMSS  SYNC  SNR  DT  FREQ_OFFSET_HZ  MARKER  MESSAGE  MODE

        Where:

            HHMMSS         time tag — *placeholder ``000000``* when jt9 is
                           invoked with ``-a tmpdir`` on a single wav file
                           (no realtime context).  We replace it with the
                           slot's actual UTC HHMMSS so ChTailer can recover
                           the real receive time.
            SYNC           sync confidence (0..~100, integer) — captured as
                           psk.spots.score.
            SNR            calibrated dB SNR (signed integer) — psk.spots.snr_db.
            DT             time-offset within slot (signed float, seconds).
            FREQ_OFFSET_HZ baseband frequency offset (float with trailing ``.``).
            MARKER         a single token ('0', '?', '~') flagging packet
                           quality / hash-resolution status.  Not currently
                           stored — present so the column count is stable.
            MESSAGE        one or more whitespace-separated tokens.
            MODE           "FT8" or "FT4" — emitted by jt9 itself, preserved.

        We prepend the slot's UTC ``YYMMDD HHMMSS BAND_FREQ_HZ`` so the
        line self-identifies its date AND the band's tuned frequency
        (downstream parsers compute absolute receive frequency as
        ``BAND_FREQ_HZ + jt9_FREQ_OFFSET_HZ``).  We do **not** append the
        mode token (jt9 already includes it) and do **not** otherwise
        reformat — the fields flow native into ``psk.spots`` via
        ``ch_tailer.parse_jt9_line``.

        Returns silently if decoded.txt is missing / unreadable / empty —
        that's the normal "no decodes this slot" case for a quiet band.
        """
        decoded = tmpdir / "decoded.txt"
        try:
            text = decoded.read_text()
        except (FileNotFoundError, OSError):
            return
        if not text.strip():
            return

        date_prefix = time.strftime("%y%m%d", time.gmtime(slot_start))
        slot_hhmmss = time.strftime("%H%M%S", time.gmtime(slot_start))
        band_freq_hz = self._frequency_hz
        try:
            for line in text.splitlines():
                if not line.strip():
                    continue
                # jt9 emits "000000" as HHMMSS when invoked with -a on
                # a single wav file (no realtime stream context).  Drop
                # whatever placeholder it has and substitute the slot's
                # actual UTC HHMMSS.  Then prepend BAND_FREQ_HZ so the
                # downstream parser can compute absolute receive freq.
                tokens = line.split(None, 1)
                rest = tokens[1] if len(tokens) > 1 else ""
                self._log_fd.write(
                    f"{date_prefix} {slot_hhmmss} {band_freq_hz} {rest.rstrip()}\n"
                )
            self._log_fd.flush()
        except OSError as exc:
            logger.warning(
                "%s: failed appending jt9 output to log: %s",
                self._mode.upper(), exc,
            )
