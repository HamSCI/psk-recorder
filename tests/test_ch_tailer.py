"""Tests for psk_recorder.core.ch_tailer (CONTRACT v0.6 §17 wiring).

Covers:
  - parse_decode_ft8_line: line-format from `decode_ft8.c:363`
  - _parse_message: best-effort callsign/grid/report extraction
  - ChTailer: tail/insert flow with a fake writer (no CH server needed)
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from psk_recorder.core.ch_tailer import (
    ChTailer,
    parse_decode_ft8_line,
    parse_decoder_line,
    _parse_message,
)


# Sample lines as `decode_ft8` writes them (from decode_ft8.c:363):
#   "%4d/%02d/%02d %02d:%02d:%02d %3d %+4.2lf %'.1lf ~ %s\n"
# Format note: `%'.1lf` uses locale grouping ("14,074,131.2" in en_US.UTF-8;
# bare digits in C locale).  Parser must tolerate either.

LINE_GROUPED = "2026/05/07 12:34:56 -15 +0.50 14,074,131.2 ~ K1ABC W1XYZ EM26"
LINE_PLAIN   = "2026/05/07 12:34:56 -15 +0.50 14074131.2 ~ K1ABC W1XYZ EM26"
LINE_DECODE_FT8 = "2026/05/07 12:34:56 -15 +0.50 14074131.2 ~ K1ABC W1XYZ EM26"

# Every row dict the decode_ft8 parser emits carries this fixed key
# set, so the psk.spots schema columns map to stable keys.
EXPECTED_ROW_KEYS = {
    "time", "mode", "decoder_kind",
    "score", "snr_db", "spectral_width_hz",
    "dt", "frequency", "frequency_mhz",
    "message", "tx_call", "rx_call", "grid", "report",
}


class TestLineParser(unittest.TestCase):

    def test_full_line_grouped(self):
        row = parse_decode_ft8_line(LINE_GROUPED, mode="ft8")
        self.assertIsNotNone(row)
        self.assertEqual(row["mode"], "ft8")
        self.assertEqual(row["score"], -15)
        self.assertAlmostEqual(row["dt"], 0.50, places=2)
        self.assertEqual(row["frequency"], 14_074_131)
        self.assertAlmostEqual(row["frequency_mhz"], 14.0741312, places=4)
        self.assertEqual(row["message"], "K1ABC W1XYZ EM26")
        self.assertEqual(
            row["time"], datetime(2026, 5, 7, 12, 34, 56, tzinfo=timezone.utc)
        )

    def test_full_line_plain(self):
        # C locale: no thousands separator
        row = parse_decode_ft8_line(LINE_PLAIN, mode="ft4")
        self.assertIsNotNone(row)
        self.assertEqual(row["mode"], "ft4")
        self.assertEqual(row["frequency"], 14_074_131)

    def test_blank_line_returns_none(self):
        self.assertIsNone(parse_decode_ft8_line("", mode="ft8"))
        self.assertIsNone(parse_decode_ft8_line("   ", mode="ft8"))

    def test_no_tilde_returns_none(self):
        self.assertIsNone(parse_decode_ft8_line(
            "2026/05/07 12:34:56 -15 +0.50 14074131.2 K1ABC W1XYZ EM26",
            mode="ft8"))

    def test_short_line_returns_none(self):
        self.assertIsNone(parse_decode_ft8_line(
            "2026/05/07 ~ junk", mode="ft8"))

    def test_garbled_freq_returns_none(self):
        bad = LINE_GROUPED.replace("14,074,131.2", "not-a-number")
        self.assertIsNone(parse_decode_ft8_line(bad, mode="ft8"))

    def test_unparseable_message_keeps_raw_text(self):
        line = "2026/05/07 12:34:56 -15 +0.50 14074131.2 ~ ??random gibberish??"
        row = parse_decode_ft8_line(line, mode="ft8")
        self.assertIsNotNone(row)
        self.assertEqual(row["message"], "??random gibberish??")
        self.assertEqual(row["tx_call"], "")    # parse failed but row still emitted


class TestMessageParser(unittest.TestCase):

    def test_simple_first_contact(self):
        out = _parse_message("K1ABC W1XYZ EM26")
        self.assertEqual(out["rx_call"], "K1ABC")
        self.assertEqual(out["tx_call"], "W1XYZ")
        self.assertEqual(out["grid"], "EM26")
        self.assertNotIn("report", out)

    def test_six_char_grid(self):
        out = _parse_message("K1ABC W1XYZ EM26ov")
        self.assertEqual(out["grid"], "EM26ov")

    def test_signal_report(self):
        out = _parse_message("K1ABC W1XYZ -15")
        self.assertEqual(out["rx_call"], "K1ABC")
        self.assertEqual(out["tx_call"], "W1XYZ")
        self.assertEqual(out["report"], -15)

    def test_signed_positive_report(self):
        out = _parse_message("K1ABC W1XYZ +05")
        self.assertEqual(out["report"], 5)

    def test_roger_report(self):
        out = _parse_message("K1ABC W1XYZ R-15")
        self.assertEqual(out["report"], -15)

    def test_cq_message(self):
        out = _parse_message("CQ K1ABC FN42")
        self.assertEqual(out["tx_call"], "K1ABC")
        self.assertEqual(out["grid"], "FN42")
        self.assertNotIn("rx_call", out)

    def test_cq_with_target(self):
        # "CQ DX K1ABC FN42" — "DX" is a region tag (not a callsign).
        # The parser scans past it and pulls K1ABC as the tx (sender).
        out = _parse_message("CQ DX K1ABC FN42")
        self.assertEqual(out["tx_call"], "K1ABC")
        self.assertEqual(out["grid"], "FN42")

    def test_freeform_returns_empty(self):
        out = _parse_message("hello world")
        self.assertEqual(out, {})

    def test_call_with_slash_suffix(self):
        out = _parse_message("K1ABC/QRP W1XYZ FN42")
        self.assertEqual(out["rx_call"], "K1ABC/QRP")


class TestDecoderLineRouter(unittest.TestCase):
    """`parse_decoder_line` routes decode_ft8 lines and rejects junk."""

    def test_routes_decode_ft8_format(self):
        row = parse_decoder_line(LINE_DECODE_FT8, mode="ft8")
        self.assertIsNotNone(row)
        self.assertEqual(row["decoder_kind"], "decode_ft8")

    def test_unrecognised_returns_none(self):
        self.assertIsNone(parse_decoder_line("hello world"))
        self.assertIsNone(parse_decoder_line(""))
        # 4-digit numeric prefix but not the YYYY/MM/DD shape — also rejected.
        self.assertIsNone(parse_decoder_line("1234 something else"))


class TestDecodeFt8RowShape(unittest.TestCase):
    """The decode_ft8 parser populates the fixed psk.spots key set."""

    def test_decode_ft8_row_has_all_keys(self):
        row = parse_decode_ft8_line(LINE_DECODE_FT8, mode="ft8")
        self.assertEqual(set(row.keys()), EXPECTED_ROW_KEYS)

    def test_decode_ft8_snr_is_none(self):
        """decode_ft8 reports an internal "score"; snr_db and
        spectral_width_hz are the documented sentinels (None)."""
        ft8 = parse_decode_ft8_line(LINE_DECODE_FT8, mode="ft8")
        self.assertIsNone(ft8["snr_db"])             # nullable column
        self.assertIsNone(ft8["spectral_width_hz"])
        self.assertEqual(ft8["score"], -15)          # decode_ft8's internal metric


# ── ChTailer with a fake writer ─────────────────────────────────────────────

class FakeWriter:
    def __init__(self, noop=False):
        self._noop = noop
        self.health = "noop" if noop else "ok"
        self.inserts: list = []
        self.flushed = 0
        self.closed = False

    @property
    def is_noop(self):
        return self._noop

    def insert(self, rows):
        self.inserts.extend(rows)

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed = True


class TestChTailer(unittest.TestCase):

    def _make_tailer(self, log_path: Path, *, noop=False, **kw):
        fake = FakeWriter(noop=noop)
        tailer = ChTailer(
            log_path=log_path, mode="ft8", radiod_id="test-rx888",
            host_call="AC0G", host_grid="EM38ww",
            processing_version="0.1.0+abc",
            writer_factory=lambda batch_rows: fake,
            **kw,
        )
        return tailer, fake

    def test_noop_mode_starts_thread_but_inserts_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text("")
            tailer, fake = self._make_tailer(log_path, noop=True)
            tailer.start()
            try:
                # Even with noop writer, lines accumulating on disk are
                # ignored (no insert call).
                log_path.write_text(LINE_PLAIN + "\n")
                time.sleep(0.05)         # but is_noop short-circuits in poll
            finally:
                tailer.stop(timeout=2.0)
            self.assertEqual(fake.inserts, [])
            self.assertEqual(tailer.health, "noop")

    def test_skips_history_at_startup(self):
        """A line written before .start() should NOT be replayed."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            # Pre-existing content (history).
            log_path.write_text(LINE_PLAIN + "\n")
            tailer, fake = self._make_tailer(log_path)
            tailer.start()
            try:
                # Wait for the first poll cycle to elapse.
                time.sleep(1.5)
            finally:
                tailer.stop(timeout=2.0)
            self.assertEqual(fake.inserts, [],
                             "tailer should not replay pre-existing log content")

    def test_consumes_appended_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text("")            # empty start
            tailer, fake = self._make_tailer(log_path)
            tailer.start()
            try:
                # Append two lines after startup.
                with open(log_path, "a") as f:
                    f.write(LINE_PLAIN + "\n")
                    f.write(LINE_GROUPED + "\n")
                # POLL_INTERVAL_SEC = 1.0; wait two cycles.
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline and len(fake.inserts) < 2:
                    time.sleep(0.1)
            finally:
                tailer.stop(timeout=2.0)
            self.assertEqual(len(fake.inserts), 2)
            for row in fake.inserts:
                self.assertEqual(row["host_call"], "AC0G")
                self.assertEqual(row["host_grid"], "EM38ww")
                self.assertEqual(row["radiod_id"], "test-rx888")
                self.assertEqual(row["processing_version"], "0.1.0+abc")
                self.assertEqual(row["mode"], "ft8")
                # PR 3: forward_to_pskreporter defaults to True (matches
                # PSK_DELIVERY_MODE=server, the new default — wsprdaemon
                # server is responsible for posting to PSKReporter).
                self.assertEqual(row["forward_to_pskreporter"], True)

    def test_forward_to_pskreporter_false_when_constructed_false(self):
        """PSK_DELIVERY_MODE=both → constructor receives forward=False,
        each inserted row carries the flag through unchanged."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text("")
            tailer, fake = self._make_tailer(
                log_path, forward_to_pskreporter=False,
            )
            tailer.start()
            try:
                with open(log_path, "a") as f:
                    f.write(LINE_PLAIN + "\n")
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline and len(fake.inserts) < 1:
                    time.sleep(0.1)
            finally:
                tailer.stop(timeout=2.0)
            self.assertGreaterEqual(len(fake.inserts), 1)
            for row in fake.inserts:
                self.assertEqual(row["forward_to_pskreporter"], False)

    def test_handles_log_rotation(self):
        """File-shrunk-below-last-pos → tailer resets to head and replays.

        Real-world logrotate uses rename+create (new inode); the
        truncate-rewrite path here is a stricter test of the size-comparison
        branch.  Make the truncate explicit by writing an empty string
        before the new content so size briefly drops to 0.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "test.log"
            log_path.write_text("")
            tailer, fake = self._make_tailer(log_path)
            tailer.start()
            try:
                with open(log_path, "a") as f:
                    f.write(LINE_PLAIN + "\n")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and len(fake.inserts) < 1:
                    time.sleep(0.1)
                self.assertGreaterEqual(len(fake.inserts), 1)
                # Explicit rotation: truncate to 0 first, give the tailer
                # a chance to observe the shrink, then write the new line.
                log_path.write_text("")
                time.sleep(1.5)
                log_path.write_text(LINE_GROUPED + "\n")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and len(fake.inserts) < 2:
                    time.sleep(0.1)
            finally:
                tailer.stop(timeout=2.0)
            self.assertGreaterEqual(len(fake.inserts), 2)


if __name__ == "__main__":
    unittest.main()
