"""Tests for the jt9 decoder line parser (psk_recorder.core.ch_tailer).

Covers parse_jt9_line against the canonical jt9 log line that
``slot.SlotWorker`` normalizes the resident wrapper's stdout into
(see docs/jt9-decoder.md §4):

    YYMMDD HHMMSS BAND_FREQ_HZ SYNC SNR DT FREQ_OFFSET_HZ MARKER MESSAGE… MODE

Key jt9-vs-decode_ft8 distinctions asserted here:
  - snr_db is populated (jt9's calibrated dB) where decode_ft8 leaves it None
  - dt is NOT calibrated (jt9 is WSJT-X; the decode_ft8→WSJT-X offset must
    not be applied)
  - frequency is absolute (dial + audio offset)
  - the router dispatches jt9 vs decode_ft8 lines by leading-token shape
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from datetime import timezone

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from psk_recorder.core.ch_tailer import (
    parse_jt9_line,
    parse_decoder_line,
)

# BAND(14074000) + OFFSET(1234) = 14075234 Hz absolute.
JT9_LINE = "260722 110115 14074000 39 9 0.9 1234. ~ GJ0KYZ RK9AX MO05 FT8"


class ParseJt9LineTest(unittest.TestCase):
    def test_happy_path_fields(self):
        row = parse_jt9_line(JT9_LINE)
        self.assertIsNotNone(row)
        self.assertEqual(row["decoder_kind"], "jt9")
        self.assertEqual(row["mode"], "ft8")
        self.assertEqual(row["score"], 39)
        self.assertEqual(row["snr_db"], 9)          # calibrated dB, populated
        self.assertIsNone(row["spectral_width_hz"])
        self.assertEqual(row["frequency"], 14075234)  # dial + audio offset
        self.assertAlmostEqual(row["frequency_mhz"], 14.075234, places=6)
        self.assertIn("GJ0KYZ", row["message"])

    def test_time_is_utc_aware(self):
        row = parse_jt9_line(JT9_LINE)
        ts = row["time"]
        self.assertEqual(ts.tzinfo, timezone.utc)
        self.assertEqual((ts.year, ts.month, ts.day), (2026, 7, 22))
        self.assertEqual((ts.hour, ts.minute, ts.second), (11, 1, 15))

    def test_dt_is_not_calibrated(self):
        # jt9 reports the WSJT-X convention already; the raw value must
        # survive unchanged (decode_ft8 would subtract ~0.65).
        row = parse_jt9_line(JT9_LINE)
        self.assertAlmostEqual(row["dt"], 0.9, places=6)

    def test_ft4_mode_tag(self):
        line = JT9_LINE.rsplit(" ", 1)[0] + " FT4"
        row = parse_jt9_line(line)
        self.assertEqual(row["mode"], "ft4")

    def test_missing_mode_token_falls_back_to_hint(self):
        # Drop the trailing MODE token; caller's hint should fill mode.
        no_mode = JT9_LINE.rsplit(" ", 1)[0]
        row = parse_jt9_line(no_mode, mode="ft8")
        self.assertIsNotNone(row)
        self.assertEqual(row["mode"], "ft8")

    def test_too_few_tokens_returns_none(self):
        self.assertIsNone(parse_jt9_line("260722 110115 14074000 39 9"))

    def test_bad_numeric_returns_none(self):
        bad = "260722 110115 14074000 xx 9 0.9 1234. ~ CQ K1ABC FN42 FT8"
        self.assertIsNone(parse_jt9_line(bad))


class RouterDispatchTest(unittest.TestCase):
    def test_router_sends_jt9_line_to_jt9_parser(self):
        row = parse_decoder_line(JT9_LINE, mode="ft8")
        self.assertIsNotNone(row)
        self.assertEqual(row["decoder_kind"], "jt9")

    def test_router_sends_ft8_line_to_decode_ft8_parser(self):
        ft8_line = "2026/07/22 11:01:15   9 +0.9 14075234 ~ GJ0KYZ RK9AX MO05"
        row = parse_decoder_line(ft8_line, mode="ft8")
        self.assertIsNotNone(row)
        self.assertEqual(row["decoder_kind"], "decode_ft8")

    def test_router_ignores_junk(self):
        self.assertIsNone(parse_decoder_line("not a decode line", mode="ft8"))
        self.assertIsNone(parse_decoder_line("", mode="ft8"))


if __name__ == "__main__":
    unittest.main()
