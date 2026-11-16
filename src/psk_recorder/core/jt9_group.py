"""One resident jt9_decode shared by a group of channels of the same mode.

Until 2026-11-16 every (band, mode) channel ran its own resident jt9_decode.
All of them received their slot at the same boundary, so a station decoded ten
FT8 bands at once (a CPU burst every 15 s, with stream gaps in the recorder),
and each process held a ~43 MB System V segment sized by WSJT-X's NTMAX (30 min
of audio) — 1.1 GB on W2NAF, 1.7 GB on KFS-SE-NW.

Michael's rule: the recording keeps its precise timing; when it decodes can
follow resource allocation, provided each decode still meets its deadline.  So
a group of bands now shares one process.  Each channel submits its slot as
before; the shared jt9_decode decodes the group's slots one after another
(it already handles one slot at a time and answers in fed order), so the CPU
work spreads across the cycle and the station keeps a few segments, not one
per band.

Affinity stays fixed: a band always goes to the same process, so jt9's in-RAM
compound-callsign hash table keeps learning the same bands every cycle
(docs/jt9-decoder.md section 2).  A table shared by several bands also resolves
a call heard on one of them when it appears on another.

Feeding never stops.  jt9_decode reads stdin with fread in 4096-sample blocks,
so the tail of the last slot fed (an FT8 slot is 43 blocks plus 3872 samples)
reaches it only with the next slot's audio, or at EOF.  Withholding a slot to
"catch up" therefore leaves the previous one unanswerable and stalls the group
for good; the first version did exactly that on B4 (2026-11-16, no decodes for
5 minutes).  Instead a stall detector restarts a group whose oldest unanswered
slot ended more than STALL_CYCLES cycles ago (and at least STALL_MIN_SEC, which
leaves room for jt9's slow first decode after a start); its unanswered slots
drop, logged.  Capture and slot timestamps never depend on when a decode ran.
"""
from __future__ import annotations

import logging
import math
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from psk_recorder.core import shm_sweep

logger = logging.getLogger(__name__)

# How long stop() lets jt9_decode drain the slots it was fed before killing it.
JT9_STOP_DRAIN_SEC = 10.0

# A group whose oldest unanswered slot ended more than this many cycles ago
# (and at least STALL_MIN_SEC ago) is stuck: restart its process.
STALL_CYCLES = 4
STALL_MIN_SEC = 60.0


def assign_groups(freqs: list[int], group_size: int) -> list[list[int]]:
    """Split one mode's frequencies into groups of at most ``group_size``.

    Round-robin over the sorted frequencies, so neighbouring bands (and the
    usually busy 40/20 m pair) land in different groups and the decode load
    spreads evenly.  ``group_size`` below 1 reads as 1: one band per process,
    the behaviour before groups existed.
    """
    ordered = sorted(freqs)
    if not ordered:
        return []
    size = max(1, int(group_size))
    n_groups = math.ceil(len(ordered) / size)
    return [ordered[i::n_groups] for i in range(n_groups)]


class Jt9Decoder:
    """A resident ``jt9_decode -T`` fed by one or more channels of one mode.

    A client (SlotWorker) supplies three callbacks:
    ``on_jt9_line(jt9_line, slot_utc)`` for each decode line of its slot,
    ``on_jt9_done(ok)`` once per slot (ok=False when jt9's watchdog fired),
    and ``on_jt9_dropped()`` for a slot that was never decoded (deadline
    guard, dead process, write failure, restart).

    Results map to slots purely by order: ``_pending`` is the FIFO of
    (client, slot_utc) in fed order, and jt9_decode answers each fed slot with
    its decode lines followed by exactly one terminal <DecodeStats>.
    """

    def __init__(
        self,
        wrapper: str,
        mode: str,
        *,
        depth: int,
        cadence_sec: float,
        name: str,
        sweep: Callable[[], object] = shm_sweep.sweep,
        now: Callable[[], float] = time.time,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        self._wrapper = wrapper
        self._mode = mode
        self._depth = depth
        self._cadence_sec = cadence_sec
        self._name = name
        self._sweep = sweep
        self._now = now
        self._popen = popen
        # Guards _proc, _pending and the order of stdin writes, so the FIFO
        # matches what the process was fed even with several feeding threads.
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._pending: deque[tuple[object, float]] = deque()
        self._running = False
        self.restarts = 0
        self.dropped = 0
        self.pid_count = 0          # processes started over this object's life
        self._stall_after = max(STALL_CYCLES * cadence_sec, STALL_MIN_SEC)

    @property
    def name(self) -> str:
        return self._name

    # ----- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._spawn_locked()

    def stop(self) -> None:
        """Close stdin so jt9_decode decodes what it was fed, then exits."""
        with self._lock:
            if not self._running and self._proc is None:
                return
            self._running = False
            proc = self._proc
            reader = self._reader
        if proc is not None:
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
        if reader is not None:
            reader.join(timeout=5.0)
        with self._lock:
            leftover = list(self._pending)
            self._pending.clear()
            self._proc = None
            self._reader = None
        for client, _ in leftover:
            client.on_jt9_dropped()

    # ----- feeding ----------------------------------------------------------

    def submit(self, client, pcm: bytes, slot_utc: float) -> bool:
        """Feed one slot of int16 PCM.  False when it could not be fed.

        Never withholds a slot to catch up: see the module docstring."""
        with self._lock:
            fed = False
            if self._running:
                if self._pending:
                    oldest = self._pending[0][1]
                    behind = self._now() - (oldest + self._cadence_sec)
                    if behind > self._stall_after:
                        self._restart_locked(
                            f"stalled: oldest unanswered slot ended "
                            f"{behind:.0f} s ago")
                proc = self._proc
                if proc is None or proc.poll() is not None:
                    self._restart_locked("process exited")
                    proc = self._proc
                if proc is not None and proc.poll() is None:
                    self._pending.append((client, slot_utc))
                    try:
                        proc.stdin.write(pcm)
                        proc.stdin.flush()
                        fed = True
                    except (BrokenPipeError, OSError) as exc:
                        self._pending.pop()
                        self._restart_locked(f"stdin write failed: {exc}")
            if not fed:
                self.dropped += 1
        if not fed:
            client.on_jt9_dropped()
        return fed

    def pending_for(self, client) -> list[float]:
        """Slot UTCs this client fed that have not been answered yet."""
        with self._lock:
            return [s for c, s in self._pending if c is client]

    # ----- internals --------------------------------------------------------

    def _spawn_locked(self) -> None:
        # Clear segments older jt9_decode builds left behind when killed.
        try:
            self._sweep()
        except Exception:  # noqa: BLE001 — a sweep failure must not stop decoding
            logger.exception("jt9 %s: shm sweep failed", self._name)
        jt9_bin = str(Path(self._wrapper).with_name("jt9"))
        mode_arg = "FT4" if self._mode == "ft4" else "FT8"
        cmd = [self._wrapper, "-j", jt9_bin, "-m", mode_arg,
               "-d", str(self._depth), "-T"]
        try:
            proc = self._popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            logger.error("jt9 %s: failed to start %s: %s", self._name, cmd, exc)
            self._proc = None
            return
        self._proc = proc
        self.pid_count += 1
        self._reader = threading.Thread(
            target=self._reader_loop, args=(proc,), daemon=True,
            name=f"jt9-read-{self._name}",
        )
        self._reader.start()
        logger.info("jt9 %s: resident jt9_decode pid=%d (%s -d%d -T, jt9=%s)",
                    self._name, proc.pid, mode_arg, self._depth, jt9_bin)

    def _restart_locked(self, why: str) -> None:
        """Replace a dead or wedged process.  Its hash table starts empty, and
        slots it never answered are dropped (their <DecodeStats> will never
        come, so keeping them would desync the FIFO)."""
        self.restarts += 1
        lost = list(self._pending)
        self._pending.clear()
        logger.warning(
            "jt9 %s: restarting jt9_decode (#%d, %s) — hash table resets; "
            "%d unanswered slot(s) dropped",
            self._name, self.restarts, why, len(lost),
        )
        old = self._proc
        self._proc = None
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
        for client, _ in lost:
            self.dropped += 1
            client.on_jt9_dropped()
        self._spawn_locked()

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        """Route ``proc``'s decode lines to the client whose slot they belong
        to, and close each slot on its <DecodeStats>.  Lines from a process
        that has since been replaced are ignored: its slots were dropped."""
        stream = proc.stdout
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("<DecodeStats>") or line.startswith("<DecodeStats "):
                with self._lock:
                    if self._proc is not proc or not self._pending:
                        continue
                    client, _ = self._pending.popleft()
                client.on_jt9_done("watchdog=1" not in line)
                continue
            if line.startswith("<"):
                continue   # <DecodeFinished> and other control lines
            with self._lock:
                if self._proc is not proc or not self._pending:
                    continue
                client, slot_utc = self._pending[0]
            client.on_jt9_line(line, slot_utc)
        with self._lock:
            replaced = self._proc is not proc
            running = self._running
        if running and not replaced:
            logger.warning("jt9 %s: jt9_decode stdout closed (process exited)",
                           self._name)
