"""Tests for ChannelSink's anchor-once + gross-error-tripwire model.

The recorder reads wall clock ONCE on the first batch (via
rtp_to_wallclock when channel_info is available, else time.time()
fallback), saves an `_anchor_utc` + `_anchor_total_samples` pair,
and projects every subsequent batch's UTC by pure sample-count
arithmetic.  No further wall-clock reads are used for timing.

A gross-error tripwire runs on every batch — if the projected UTC
diverges from wall_now by GROSS_THRESHOLD_SEC for GROSS_TRIPS_FOR_EXIT
consecutive batches, the recorder exits non-zero so systemd restarts
it with a fresh anchor.

These tests use a fake Ring + SlotWorker via monkey-patching so we
can drive on_samples() in isolation.
"""

import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import numpy as np

from psk_recorder.core.stream import (
    GROSS_EXIT_CODE,
    GROSS_THRESHOLD_SEC,
    GROSS_TRIPS_FOR_EXIT,
    ChannelSink,
)


@dataclass
class _FakeQuality:
    """Stand-in for MultiStream's StreamQuality."""
    total_samples_delivered: int = 0
    first_rtp_timestamp: int = 0


class _FakeChannelInfo:
    """Stand-in for ka9q's ChannelInfo carrying gps_time / rtp_timesnap."""
    def __init__(self, gps_time=1462564880_000_000_000, rtp_timesnap=1):
        self.gps_time = gps_time
        self.rtp_timesnap = rtp_timesnap


def _make_sink() -> ChannelSink:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "ft8").mkdir(exist_ok=True)
    log_fd = open(tmp / "log", "ab")
    sink = ChannelSink(
        mode="ft8",
        frequency_hz=14_074_000,
        sample_rate=12_000,
        preset="usb",
        encoding=0,
        spool_dir=tmp,
        log_fd=log_fd,
        decoder_path="/nonexistent",
        keep_wav=False,
        authority_reader=None,
    )
    sink._tmp_dir = tmp  # type: ignore[attr-defined]
    return sink


def _cleanup_sink(sink) -> None:
    tmp = getattr(sink, "_tmp_dir", None)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


class TestAnchorOnce(unittest.TestCase):

    def test_first_batch_anchors_from_wall_clock_fallback(self):
        """No channel_info → time.time()-based anchor on first batch."""
        sink = _make_sink()
        try:
            samples = np.zeros(2400, dtype=np.float32)   # 200 ms at 12 kHz
            q = _FakeQuality(total_samples_delivered=2400, first_rtp_timestamp=0)
            with mock.patch("psk_recorder.core.stream.time.time",
                            return_value=1_700_000_000.0):
                with mock.patch.object(sink._ring, "push") as push:
                    sink.on_samples(samples, q)
            self.assertEqual(sink._anchor_source, "wallclock_fallback")
            # First-sample UTC = wall_now - n/sample_rate = 1700000000.0 - 0.2
            self.assertAlmostEqual(sink._anchor_utc, 1_699_999_999.8, places=3)
            self.assertEqual(sink._anchor_total_samples, 0)
            # Ring received the projected UTC for the first sample.
            push.assert_called_once()
            self.assertAlmostEqual(push.call_args[0][1],
                                   1_699_999_999.8, places=3)
        finally:
            _cleanup_sink(sink)

    def test_first_batch_anchors_via_rtp_to_wallclock_when_available(self):
        """channel_info set + rtp_to_wallclock returns a number → use it."""
        sink = _make_sink()
        sink.set_channel_info(_FakeChannelInfo())
        try:
            samples = np.zeros(2400, dtype=np.float32)
            q = _FakeQuality(total_samples_delivered=2400,
                             first_rtp_timestamp=1_000_000)
            with mock.patch("ka9q.rtp_to_wallclock",
                            return_value=1_700_000_500.0):
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=1_700_000_500.0):
                    with mock.patch.object(sink._ring, "push"):
                        sink.on_samples(samples, q)
            self.assertEqual(sink._anchor_source, "rtp_to_wallclock")
            self.assertAlmostEqual(sink._anchor_utc, 1_700_000_500.0, places=3)
        finally:
            _cleanup_sink(sink)

    def test_subsequent_batches_use_pure_sample_count_projection(self):
        """Second batch's UTC = anchor + delivered_since_anchor/sample_rate.

        Critically: time.time() is moved during the test, but the
        projection IGNORES that — confirms zero wall-clock dependency
        after anchor."""
        sink = _make_sink()
        try:
            sr = sink.sample_rate
            with mock.patch.object(sink._ring, "push") as push:
                # Anchor at wall_now=1000.0 with 1 sec of samples.
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=1000.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr),
                    )

                # Now ANY value of time.time() must not affect the
                # projection — push another 0.5 s of samples and verify
                # we see anchor + 1.0 s (since anchor accounted for the
                # FIRST 1 s and the new batch's first sample is at +1.0 s).
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=999_999_999.0):  # absurd
                    sink.on_samples(
                        np.zeros(sr // 2, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr + sr // 2),
                    )

            # First push: utc_of_first = anchor (1000.0 - 1.0) = 999.0
            self.assertAlmostEqual(push.call_args_list[0][0][1], 999.0, places=4)
            # Second push: anchor + (sr / sr) = anchor + 1.0 = 1000.0
            self.assertAlmostEqual(push.call_args_list[1][0][1], 1000.0, places=4)
        finally:
            _cleanup_sink(sink)

    def test_on_stream_restored_does_not_change_anchor(self):
        """Stream-gap recovery must NOT re-anchor; that was the old
        wall-clock-correlated bug."""
        sink = _make_sink()
        sink.set_channel_info(_FakeChannelInfo(rtp_timesnap=1))
        try:
            sr = sink.sample_rate
            with mock.patch.object(sink._ring, "push"):
                with mock.patch("ka9q.rtp_to_wallclock",
                                return_value=2000.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr,
                                     first_rtp_timestamp=1),
                    )
            initial_anchor = sink._anchor_utc
            initial_total = sink._anchor_total_samples
            initial_channel = sink._channel_info

            # Stream "restored" with a totally different channel_info.
            sink.on_stream_restored(_FakeChannelInfo(rtp_timesnap=999))

            # Anchor MUST be unchanged.
            self.assertEqual(sink._anchor_utc, initial_anchor)
            self.assertEqual(sink._anchor_total_samples, initial_total)
            # channel_info MUST also be unchanged (any new snapshot ignored).
            self.assertIs(sink._channel_info, initial_channel)
        finally:
            _cleanup_sink(sink)


class TestGrossErrorTripwire(unittest.TestCase):

    def test_clean_batches_do_not_trip(self):
        """Projected UTC matches wall_now → counter stays at 0."""
        sink = _make_sink()
        try:
            sr = sink.sample_rate
            with mock.patch.object(sink._ring, "push"):
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=1000.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr),
                    )
                # Several more batches with consistent wall-clock advance.
                for i in range(5):
                    delivered = sr * (2 + i)
                    with mock.patch("psk_recorder.core.stream.time.time",
                                    return_value=1000.0 + (1 + i)):
                        sink.on_samples(
                            np.zeros(sr, dtype=np.float32),
                            _FakeQuality(total_samples_delivered=delivered),
                        )
            self.assertEqual(sink._gross_trips, 0)
        finally:
            _cleanup_sink(sink)

    def test_gross_error_increments_counter(self):
        """When wall_now is far from projection, the trip counter advances."""
        sink = _make_sink()
        try:
            sr = sink.sample_rate
            with mock.patch.object(sink._ring, "push"):
                # Anchor at wall_now=1000 — projected_utc tracks samples.
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=1000.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr),
                    )
                # Next batch: pretend wall clock jumped 10 sec — projection
                # is now 9s behind wall.  Above the 2s threshold → trip.
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=1010.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr * 2),
                    )
            self.assertEqual(sink._gross_trips, 1)
        finally:
            _cleanup_sink(sink)

    def test_gross_error_exits_after_consecutive_trips(self):
        """K consecutive trips → sys.exit(GROSS_EXIT_CODE)."""
        sink = _make_sink()
        try:
            sr = sink.sample_rate
            with mock.patch.object(sink._ring, "push"):
                # Anchor.
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=1000.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr),
                    )
                # Force GROSS_TRIPS_FOR_EXIT consecutive bad batches.
                with self.assertRaises(SystemExit) as cm:
                    for i in range(GROSS_TRIPS_FOR_EXIT):
                        with mock.patch("psk_recorder.core.stream.time.time",
                                        return_value=2000.0 + i):
                            sink.on_samples(
                                np.zeros(sr, dtype=np.float32),
                                _FakeQuality(
                                    total_samples_delivered=sr * (2 + i),
                                ),
                            )
                self.assertEqual(cm.exception.code, GROSS_EXIT_CODE)
        finally:
            _cleanup_sink(sink)

    def test_clean_batch_resets_trip_counter(self):
        """A single bad batch followed by a clean one must reset the counter
        (otherwise transient hiccups would accumulate to a false exit)."""
        sink = _make_sink()
        try:
            sr = sink.sample_rate
            with mock.patch.object(sink._ring, "push"):
                # Anchor at 1000.
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=1000.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr),
                    )
                # One trip.
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=1010.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr * 2),
                    )
                self.assertEqual(sink._gross_trips, 1)
                # Now a clean batch — wall_now matches projection (1.0 s into
                # stream means wall_now = anchor + sr/sr = 1000.0).
                with mock.patch("psk_recorder.core.stream.time.time",
                                return_value=1001.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr * 3),
                    )
                self.assertEqual(sink._gross_trips, 0)
        finally:
            _cleanup_sink(sink)


if __name__ == "__main__":
    unittest.main()
