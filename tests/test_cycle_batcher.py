"""Tests for psk_recorder.core.cycle_batcher.

Covers Phase C: per-(cycle, rx_source) batching of decoded FT8/FT4
spots before they hit the SQLite sink.  Exercises:

  * Cycle-boundary math for both FT8 (15 s) and FT4 (7.5 s)
  * Batch keying — same cycle / different rx → distinct batches
  * Per-mode deadline behaviour
  * Cycle-commit log line in WSPR-parity format (parseable by
    ``smd watch psk``)
  * Shutdown drains pending batches
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from psk_recorder.core.cycle_batcher import (
    PskCycleBatcher,
    _cycle_start,
    _cycle_iso,
    _freq_to_band_name,
)


class FakeWriter:
    """Stand-in for sigmond.hamsci_ch.Writer.

    Tracks each insert() / flush() / close() call so tests can
    inspect what the batcher's writer thread wrote.
    """

    def __init__(self, batch_rows: int = 200):
        self.batch_rows = batch_rows
        self.inserts: list[list[dict]] = []
        self.flushes = 0
        self.closed = False
        self.is_noop = False
        self.health = "ok"
        self._lock = threading.Lock()

    def insert(self, rows):
        with self._lock:
            self.inserts.append(list(rows))

    def flush(self):
        with self._lock:
            self.flushes += 1

    def close(self):
        self.closed = True


def _row(*, mode="ft8", utc=(2026, 5, 20, 19, 14, 0), freq=14074000, **kw):
    """Build a minimal psk.spots row dict for the batcher."""
    return {
        "time": datetime(*utc, tzinfo=timezone.utc),
        "mode": mode,
        "frequency": freq,
        "tx_call": kw.get("tx_call", "K1ABC"),
        "grid": kw.get("grid", "FN42"),
        "message": "K1ABC W1XYZ FN42",
    }


class CycleBoundaryTests(unittest.TestCase):

    def test_ft8_cycle_floors_to_15s(self):
        ts = datetime(2026, 5, 20, 19, 14, 22, tzinfo=timezone.utc)
        start = _cycle_start(ts, "ft8")
        self.assertEqual(start.second, 15)
        self.assertEqual(start.microsecond, 0)

    def test_ft4_cycle_floors_to_7s5(self):
        ts = datetime(2026, 5, 20, 19, 14, 10, tzinfo=timezone.utc)
        start = _cycle_start(ts, "ft4")
        # 19:14:10 → floors to 19:14:07.5
        self.assertEqual(start.second, 7)
        self.assertEqual(start.microsecond, 500_000)

    def test_iso_renders_with_decisecond(self):
        ts = datetime(2026, 5, 20, 19, 14, 7, 500_000, tzinfo=timezone.utc)
        self.assertEqual(_cycle_iso(ts), "2026-05-20T19:14:07.5Z")

    def test_unknown_mode_falls_back_to_minute_boundary(self):
        """A bogus mode tag must not crash the floor — the batcher
        downgrades to coarse-grained batches (1-minute window)."""
        ts = datetime(2026, 5, 20, 19, 14, 22, tzinfo=timezone.utc)
        start = _cycle_start(ts, "weird-mode")
        self.assertEqual(start.second, 0)


class FreqToBandTests(unittest.TestCase):

    def test_standard_ft8_freqs_map_to_band(self):
        self.assertEqual(_freq_to_band_name(14074000), "20")
        self.assertEqual(_freq_to_band_name(7074000), "40")
        self.assertEqual(_freq_to_band_name(28074000), "10")

    def test_standard_ft4_freqs_map_to_band(self):
        self.assertEqual(_freq_to_band_name(14080000), "20")
        self.assertEqual(_freq_to_band_name(7047500), "40")

    def test_non_standard_freq_falls_back_to_khz_bucket(self):
        # 14076000 isn't in the table; nearest 100 kHz = 14100k
        # (the rounding bias is acceptable for the log-line tag).
        tag = _freq_to_band_name(14076000)
        self.assertTrue(tag.endswith("k"), f"unexpected tag: {tag}")


class BatcherFlushTests(unittest.TestCase):

    def _make(self, *, ft8_deadline=0.05, ft4_deadline=0.05):
        """Spin up a batcher with a tight deadline so tests fire quickly."""
        writer = FakeWriter()
        batcher = PskCycleBatcher(
            writer_factory=lambda batch_rows: writer,
            ft8_deadline_sec=ft8_deadline,
            ft4_deadline_sec=ft4_deadline,
        )
        batcher.start()
        return batcher, writer

    def test_ft8_batch_flushes_after_deadline(self):
        batcher, writer = self._make()
        try:
            batcher.add(
                [_row(mode="ft8")],
                rx_source="radiod:bee1-status.local",
                radiod_id="bee1",
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not writer.inserts:
                time.sleep(0.02)
        finally:
            batcher.stop()
        self.assertEqual(len(writer.inserts), 1)
        self.assertEqual(len(writer.inserts[0]), 1)
        self.assertEqual(writer.inserts[0][0]["mode"], "ft8")

    def test_same_cycle_different_rx_yields_separate_batches(self):
        """Multi-source decode of the same cycle: each rx flushes its
        own batch so per-rx visibility and Phase D dedup both work."""
        batcher, writer = self._make()
        try:
            batcher.add(
                [_row(mode="ft8", tx_call="X1")],
                rx_source="radiod:bee1-status.local", radiod_id="bee1",
            )
            batcher.add(
                [_row(mode="ft8", tx_call="X2")],
                rx_source="radiod:bee2-status.local", radiod_id="bee2",
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and len(writer.inserts) < 2:
                time.sleep(0.02)
        finally:
            batcher.stop()
        self.assertEqual(len(writer.inserts), 2)
        # Order isn't guaranteed (depends on scheduling), but the
        # combined batches must cover both calls.
        all_calls = {row["tx_call"]
                     for batch in writer.inserts for row in batch}
        self.assertEqual(all_calls, {"X1", "X2"})

    def test_same_rx_same_cycle_coalesces(self):
        """Two adds in one cycle from one rx → one batch with both
        rows.  Confirms the dict-keying behaviour."""
        batcher, writer = self._make()
        try:
            batcher.add(
                [_row(mode="ft8", tx_call="A")],
                rx_source="radiod:rx-status.local", radiod_id="rx",
            )
            batcher.add(
                [_row(mode="ft8", tx_call="B")],
                rx_source="radiod:rx-status.local", radiod_id="rx",
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not writer.inserts:
                time.sleep(0.02)
        finally:
            batcher.stop()
        self.assertEqual(len(writer.inserts), 1)
        calls = {row["tx_call"] for row in writer.inserts[0]}
        self.assertEqual(calls, {"A", "B"})

    def test_log_line_format_matches_wspr_parity(self):
        """The cycle commit log line must include rx, mode, spot
        count, and bands=[...] in the order ``smd watch psk`` will
        parse.  Format is anchored against a regex in the test rather
        than printing a fragile exact string."""
        import re
        batcher, writer = self._make()
        with self.assertLogs(
            "psk_recorder.core.cycle_batcher", level=logging.INFO,
        ) as cm:
            try:
                batcher.add(
                    [
                        _row(mode="ft8", freq=14074000),
                        _row(mode="ft8", freq=14074000),
                        _row(mode="ft8", freq=7074000),
                    ],
                    rx_source="radiod:bee1-status.local",
                    radiod_id="bee1",
                )
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and not writer.inserts:
                    time.sleep(0.02)
            finally:
                batcher.stop()
        joined = "\n".join(cm.output)
        # Required pieces — order matters for the watch parser.
        pat = re.compile(
            r"cycle UTC \S+ rx=radiod:bee1-status\.local mode=ft8 "
            r"→ 3 spots in psk\.spots "
            r"\(sqlite write \d+ ms\) bands=\[",
        )
        self.assertRegex(joined, pat)
        # Both bands should appear in the breakdown (40m and 20m).
        self.assertRegex(joined, r"bands=\[[^]]*20:2")
        self.assertRegex(joined, r"bands=\[[^]]*40:1")

    def test_stop_drains_pending_batches(self):
        """A batch under its deadline at stop() must still flush —
        we don't want shutdown to silently drop just-received spots."""
        batcher, writer = self._make(ft8_deadline=10.0)  # long deadline
        batcher.add(
            [_row(mode="ft8")],
            rx_source="radiod:bee1-status.local", radiod_id="bee1",
        )
        # Give the batcher's writer thread a tick to construct its
        # writer (writer_factory runs inside _run, not __init__).
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and batcher.writer is None:
            time.sleep(0.02)
        batcher.stop()
        self.assertEqual(len(writer.inserts), 1)
        self.assertEqual(len(writer.inserts[0]), 1)


if __name__ == "__main__":
    unittest.main()
