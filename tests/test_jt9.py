"""Tests for the WSJT-X jt9 line parser + dual-format auto-detect router.

Covers:
  * `parse_jt9_line` — happy path, missing spectral_width, missing
    mode suffix, garbage / blank input, unparseable numbers.
  * `parse_decoder_line` — dual-format auto-detect (decode_ft8 vs jt9
    vs unrecognised).
  * Round-trip of the row dict — both parsers populate the SAME set
    of keys so the CH `psk.spots` schema's columns are filled
    consistently regardless of which decoder produced the line.

Live jt9 invocation by SlotWorker is not unit-tested here — it
requires WSJT-X on PATH.  Smoke-tested on bee1-rx888 instead.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from psk_recorder.core.ch_tailer import (
    parse_decode_ft8_line,
    parse_decoder_line,
    parse_jt9_line,
)


# Sample jt9 lines as SlotWorker._materialise_jt9_output emits them
# (jt9's native decoded.txt line + YYMMDD prefix + MODE suffix).
LINE_JT9_FT8_FULL = "260507 1234 -12 +0.45 1250 ` K1ABC W1XYZ EM26 0.0180 FT8"
LINE_JT9_FT8_NO_WIDTH = "260507 1234 -12 +0.45 1250 ` K1ABC W1XYZ EM26 FT8"
LINE_JT9_FT4 = "260507 1234 +5 -0.20 1500 ` CQ K1ABC FN42 0.0120 FT4"

# Sample decode_ft8 line (legacy fallback).
LINE_DECODE_FT8 = "2026/05/07 12:34:56 -15 +0.50 14074131.2 ~ K1ABC W1XYZ EM26"


# ── parse_jt9_line ─────────────────────────────────────────────────────────

class TestParseJt9Line:

    def test_full_line_with_spectral_width(self):
        row = parse_jt9_line(LINE_JT9_FT8_FULL)
        assert row is not None
        assert row["time"] == datetime(2026, 5, 7, 12, 34)
        assert row["mode"] == "ft8"
        assert row["decoder_kind"] == "jt9"
        assert row["snr_db"] == -12
        assert row["dt"] == pytest.approx(0.45)
        assert row["frequency"] == 1250
        assert row["frequency_mhz"] == pytest.approx(0.001250, abs=1e-6)
        assert row["message"] == "K1ABC W1XYZ EM26"
        assert row["spectral_width_hz"] == pytest.approx(0.0180)
        # decode_ft8 fields cleared.
        assert row["score"] == 0

    def test_line_without_spectral_width(self):
        row = parse_jt9_line(LINE_JT9_FT8_NO_WIDTH)
        assert row is not None
        assert row["spectral_width_hz"] is None
        assert row["message"] == "K1ABC W1XYZ EM26"

    def test_ft4_mode_token_picked_up(self):
        row = parse_jt9_line(LINE_JT9_FT4)
        assert row is not None
        assert row["mode"] == "ft4"
        assert row["spectral_width_hz"] == pytest.approx(0.0120)
        # CQ form → tx_call extracted, no rx_call.
        assert row["tx_call"] == "K1ABC"
        assert row["grid"] == "FN42"
        assert "rx_call" in row     # key present
        assert row["rx_call"] == ""

    def test_signal_report_in_message(self):
        line = "260507 1234 -8 +0.10 1500 ` K1ABC W1XYZ -15 FT8"
        row = parse_jt9_line(line)
        assert row is not None
        assert row["report"] == -15
        # spectral_width absent (no float at the penultimate slot).
        assert row["spectral_width_hz"] is None

    def test_blank_line_returns_none(self):
        assert parse_jt9_line("") is None
        assert parse_jt9_line("   ") is None

    def test_short_line_returns_none(self):
        assert parse_jt9_line("260507 1234 -12") is None

    def test_missing_backtick_returns_none(self):
        line = "260507 1234 -12 +0.45 1250 # K1ABC W1XYZ EM26 FT8"
        assert parse_jt9_line(line) is None

    def test_garbled_snr_returns_none(self):
        line = "260507 1234 not-a-number +0.45 1250 ` K1ABC W1XYZ EM26 FT8"
        assert parse_jt9_line(line) is None

    def test_garbled_date_returns_none(self):
        line = "26-05-07 1234 -12 +0.45 1250 ` K1ABC W1XYZ EM26 FT8"
        assert parse_jt9_line(line) is None

    def test_no_mode_suffix_falls_back_to_caller_hint(self):
        # If the slot.py suffix were ever missing, the parser uses the
        # caller-supplied `mode` arg.
        line = "260507 1234 -12 +0.45 1250 ` K1ABC W1XYZ EM26"
        row = parse_jt9_line(line, mode="ft8")
        assert row is not None
        assert row["mode"] == "ft8"

    def test_message_without_extra_metric_is_pure_message(self):
        # Bare message, no spectral width, no mode suffix.  We need
        # the caller's mode hint AND need to NOT mistake the last
        # message token for a number.
        line = "260507 1234 -12 +0.45 1250 ` RR73"
        row = parse_jt9_line(line, mode="ft8")
        assert row is not None
        assert row["message"] == "RR73"
        assert row["spectral_width_hz"] is None


# ── parse_decoder_line (auto-detect router) ────────────────────────────────

class TestRouter:

    def test_routes_jt9_format(self):
        row = parse_decoder_line(LINE_JT9_FT8_FULL)
        assert row is not None
        assert row["decoder_kind"] == "jt9"

    def test_routes_decode_ft8_format(self):
        row = parse_decoder_line(LINE_DECODE_FT8, mode="ft8")
        assert row is not None
        assert row["decoder_kind"] == "decode_ft8"

    def test_unrecognised_returns_none(self):
        assert parse_decoder_line("hello world") is None
        assert parse_decoder_line("") is None
        # 4-digit numeric prefix but not the YYYY/MM/DD shape — also rejected.
        assert parse_decoder_line("1234 something else") is None


# ── Row-shape compatibility (same keys regardless of decoder) ──────────────

EXPECTED_ROW_KEYS = {
    "time", "mode", "decoder_kind",
    "score", "snr_db", "spectral_width_hz",
    "dt", "frequency", "frequency_mhz",
    "message", "tx_call", "rx_call", "grid", "report",
}


class TestRowShape:
    """Both parsers populate the same key set so the CH writer treats
    rows identically (psk.spots schema columns map to fixed keys)."""

    def test_jt9_row_has_all_keys(self):
        row = parse_jt9_line(LINE_JT9_FT8_FULL)
        assert set(row.keys()) == EXPECTED_ROW_KEYS

    def test_decode_ft8_row_has_all_keys(self):
        row = parse_decode_ft8_line(LINE_DECODE_FT8, mode="ft8")
        assert set(row.keys()) == EXPECTED_ROW_KEYS

    def test_jt9_score_is_zero_decoder_ft8_snr_is_none(self):
        """The decoder-specific columns are filled per decoder; the
        other side's metric is the documented sentinel."""
        jt9 = parse_jt9_line(LINE_JT9_FT8_FULL)
        ft8 = parse_decode_ft8_line(LINE_DECODE_FT8, mode="ft8")
        assert jt9["score"] == 0          # not None — score is non-nullable
        assert jt9["snr_db"] == -12
        assert jt9["spectral_width_hz"] is not None
        assert ft8["snr_db"] is None      # nullable column
        assert ft8["spectral_width_hz"] is None
        assert ft8["score"] == -15        # decode_ft8's internal metric


# ── SlotWorker.decoder_kind validation ─────────────────────────────────────

class TestSlotWorkerKindValidation:

    def test_valid_kinds_accepted(self):
        from psk_recorder.core.slot import (
            DECODER_FT8_LIB, DECODER_JT9, VALID_DECODER_KINDS,
        )
        assert "jt9" in VALID_DECODER_KINDS
        assert "decode_ft8" in VALID_DECODER_KINDS
        assert DECODER_JT9 == "jt9"
        assert DECODER_FT8_LIB == "decode_ft8"

    def test_invalid_kind_rejected_by_constructor(self):
        from psk_recorder.core.ring import Ring
        from psk_recorder.core.slot import SlotWorker
        ring = Ring(max_seconds=60.0, sample_rate=12000)
        with pytest.raises(ValueError, match="decoder_kind"):
            SlotWorker(
                ring=ring, mode="ft8", frequency_hz=14074000,
                cadence_sec=15.0, spool_dir=Path("/tmp"),
                log_fd=None, decoder_path="/nope",
                decoder_kind="bogus",
            )


# ── _materialise_jt9_output (slot.py bridge) ──────────────────────────────

class TestMaterialiseJt9Output:
    """SlotWorker reads jt9's decoded.txt and writes WSJT-X-canonical
    lines (with YYMMDD prefix + MODE suffix) to the per-mode log."""

    def _slot_worker(self, mode: str, log_fd, tmp_path: Path):
        from psk_recorder.core.ring import Ring
        from psk_recorder.core.slot import SlotWorker
        ring = Ring(max_seconds=60.0, sample_rate=12000)
        return SlotWorker(
            ring=ring, mode=mode, frequency_hz=14_074_000,
            cadence_sec=15.0 if mode == "ft8" else 7.5,
            spool_dir=tmp_path,
            log_fd=log_fd,
            decoder_path="/usr/bin/jt9",
            decoder_kind="jt9",
        )

    def _epoch(self, year, month, day, hour=0, minute=0):
        from datetime import datetime, timezone
        return datetime(year, month, day, hour, minute,
                        tzinfo=timezone.utc).timestamp()

    def test_writes_lines_with_date_prefix_and_mode_suffix(self, tmp_path):
        import io
        log = io.StringIO()
        worker = self._slot_worker("ft8", log, tmp_path)
        decoded_dir = tmp_path / "decoded"
        decoded_dir.mkdir()
        (decoded_dir / "decoded.txt").write_text(
            "1234 -12  +0.45 1250  `  K1ABC W1XYZ EM26  0.0180\n"
            "1234  -8  +0.10 1500  `  CQ K1ABC FN42  0.0120\n"
        )
        slot_start = self._epoch(2026, 5, 7, 12, 34)
        worker._materialise_jt9_output(decoded_dir, slot_start)
        out = log.getvalue()
        assert out.startswith("260507 ")
        assert "FT8\n" in out
        lines = [l for l in out.splitlines() if l]
        assert len(lines) == 2
        # Each line round-trips through the parser.
        rows = [parse_jt9_line(l) for l in lines]
        assert all(r is not None for r in rows)
        assert rows[0]["mode"] == "ft8"
        assert rows[0]["snr_db"] == -12
        assert rows[0]["spectral_width_hz"] == pytest.approx(0.0180)

    def test_empty_decoded_txt_writes_nothing(self, tmp_path):
        import io
        log = io.StringIO()
        worker = self._slot_worker("ft8", log, tmp_path)
        (tmp_path / "decoded.txt").write_text("")
        worker._materialise_jt9_output(tmp_path, self._epoch(2026, 5, 7))
        assert log.getvalue() == ""

    def test_missing_decoded_txt_writes_nothing(self, tmp_path):
        import io
        log = io.StringIO()
        worker = self._slot_worker("ft8", log, tmp_path)
        # No decoded.txt exists.
        worker._materialise_jt9_output(tmp_path, self._epoch(2026, 5, 7))
        assert log.getvalue() == ""


# ── Bracketed compound-callsign extraction (callhash wire) ─────────────────

class TestBracketedCalls:
    """Compound calls (`<K1ABC/QRP>`) appear bracketed in jt9 output
    when the decoder resolved a hash from its session table.  The
    parser strips the brackets and extracts the call so it lands as a
    real value in `tx_call`/`rx_call` instead of being dropped."""

    def test_compound_tx_in_bracketed_form(self):
        # First-contact form: <rx_call> <tx_call> <grid>; bracketed compound
        line = "260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> W1XYZ EM26 FT8"
        row = parse_jt9_line(line)
        assert row is not None
        assert row["rx_call"] == "K1ABC/QRP"
        assert row["tx_call"] == "W1XYZ"
        assert row["grid"] == "EM26"

    def test_compound_rx_and_tx_both_bracketed(self):
        line = "260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> <VE3/W1XYZ> 73 FT8"
        row = parse_jt9_line(line)
        assert row is not None
        assert row["rx_call"] == "K1ABC/QRP"
        assert row["tx_call"] == "VE3/W1XYZ"

    def test_cq_form_with_bracketed_tx(self):
        line = "260507 1234 -12 +0.45 1250 ` CQ <K1ABC/QRP> FN42 FT8"
        row = parse_jt9_line(line)
        assert row is not None
        assert row["tx_call"] == "K1ABC/QRP"
        assert row["grid"] == "FN42"
        assert row["rx_call"] == ""

    def test_unresolved_placeholder_leaves_tx_call_empty(self):
        # `<...>` is the literal "I couldn't resolve this hash" marker.
        # The OTHER call may still be resolvable.
        line = "260507 1234 -12 +0.45 1250 ` <...> W1XYZ EM26 FT8"
        row = parse_jt9_line(line)
        assert row is not None
        assert row["rx_call"] == ""        # unresolved
        assert row["tx_call"] == "W1XYZ"
        assert row["grid"] == "EM26"


# ── ChTailer ↔ callhash integration ────────────────────────────────────────

class _FakeChWriter:
    """Minimal stand-in for sigmond.hamsci_ch.Writer — counts inserts."""

    def __init__(self):
        self.inserts = []
        self.health = "ok"
        self.is_noop = False
        self.database = "psk"

    def insert(self, rows):
        self.inserts.extend(rows)

    def flush(self):
        pass

    def close(self):
        pass


class TestChTailerCallhash:
    """ChTailer feeds each chunk of new log text to a CallHashTable
    before parsing; persistence is per-radiod (shared across modes)."""

    def _tailer(self, log_path, callhash_path, fake_writer):
        from psk_recorder.core.ch_tailer import ChTailer
        return ChTailer(
            log_path=log_path,
            mode="ft8",
            radiod_id="test-rx888",
            host_call="AC0G",
            host_grid="EM38ww",
            processing_version="0.4.0",
            writer_factory=lambda batch_rows: fake_writer,
            callhash_path=callhash_path,
        )

    def test_observe_picks_up_announcements_from_appended_text(self, tmp_path):
        import io, time
        log_path = tmp_path / "test-rx888-ft8.log"
        log_path.write_text("")
        callhash_path = tmp_path / "callhash.json"
        fake = _FakeChWriter()
        tailer = self._tailer(log_path, callhash_path, fake)
        tailer.start()
        try:
            with open(log_path, "a") as f:
                f.write("260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> CQ FT8\n")
                f.write("260507 1234 -8 +0.10 1500 ` <VE3/W1XYZ> 73 FT8\n")
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and len(fake.inserts) < 2:
                time.sleep(0.1)
        finally:
            tailer.stop(timeout=2.0)
        # CallHashTable should now contain both compound calls.
        assert tailer._callhash is not None
        assert "K1ABC/QRP" in tailer._callhash
        assert "VE3/W1XYZ" in tailer._callhash
        # And the table was persisted on stop.
        assert callhash_path.exists()
        import json
        data = json.loads(callhash_path.read_text())
        assert "K1ABC/QRP" in data["calls"]
        assert "VE3/W1XYZ" in data["calls"]

    def test_callhash_table_loads_existing_state_at_start(self, tmp_path):
        from sigmond.callhash import CallHashTable
        callhash_path = tmp_path / "callhash.json"
        # Seed the table first.
        seed = CallHashTable.load_or_new(callhash_path)
        seed.add("KH6/G0XYZ")
        seed.save()
        # Now build a tailer pointed at the same file — should
        # pre-populate.
        log_path = tmp_path / "test-rx888-ft8.log"
        log_path.write_text("")
        tailer = self._tailer(log_path, callhash_path, _FakeChWriter())
        assert tailer._callhash is not None
        assert "KH6/G0XYZ" in tailer._callhash
