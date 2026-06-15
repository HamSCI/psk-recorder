"""Per-radiod timing-fault reporter for psk-recorder.

Aggregates the per-channel timing-fault detections (the sample-count
projection diverged from radiod's GPS reference) into a loud, rate-limited
operator alarm:

  * a ``#TIMINGFAULT …`` line in the per-receiver timing log
    ``<log_dir>/<radiod_id>-timing.log`` that ``smd watch psk`` surfaces, and
  * a journal ERROR.

Operator principle (2026-06-05): under a GPSDO-disciplined sample clock and a
lossless transport, drift and sample loss are impossible in normal operation,
so any such divergence is a real fault.  The recorder still re-anchors to keep
decoding, but the fault MUST be surfaced so the operator investigates the
system — recovery is never silent.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class TimingFaultReporter:
    """Thread-safe, rate-limited fault sink shared by one radiod's channels.

    ``report()`` is called from MultiStream callback threads (one per
    channel), so it locks.  The loud line is emitted at most once per
    ``cooldown_sec`` per radiod; every fault still bumps the incident
    counter so recurrence is visible rather than normalised away.
    """

    def __init__(self, radiod_id: str, log_dir, cooldown_sec: float = 30.0):
        self._rid = radiod_id or "?"
        self._path = Path(log_dir) / f"{self._rid}-timing.log"
        self._cooldown = cooldown_sec
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self._count = 0

    @property
    def incident_count(self) -> int:
        return self._count

    def report(self, mode: str, freq_hz: int, divergence_sec: float) -> None:
        with self._lock:
            self._count += 1
            now = time.time()
            if now - self._last_emit < self._cooldown:
                return
            self._last_emit = now
            count = self._count
        ts = time.strftime("%Y/%m/%d %H:%M:%S", time.gmtime(now))
        line = (
            f"#TIMINGFAULT {ts} rx={self._rid} mode={mode} "
            f"freq={freq_hz / 1e6:.4f}MHz diverged={divergence_sec:+.3f}s "
            f"re-anchored incident={count} INVESTIGATE\n"
        )
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:  # noqa: BLE001 — logging must not raise
            logger.debug("timing-fault log write failed: %s", e)
        logger.error(
            "TIMING FAULT rx=%s mode=%s freq=%.4fMHz: projection diverged "
            "%+.3fs from radiod GPS reference — re-anchored; INVESTIGATE "
            "(corrupt anchor / radiod clock / sample loss); incident=%d",
            self._rid, mode, freq_hz / 1e6, divergence_sec, count,
        )
