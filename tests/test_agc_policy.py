"""Policy (2026-10-23): no client asks radiod for per-channel AGC.

Every channel request carries agc_enable=0 and gain=0 dB explicitly, so the
preset default (``usb`` and ``iq`` default to ``agc = yes``) never applies.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from psk_recorder.core.receiver_manager import ReceiverManager


class ChannelRequestAgcTests(unittest.TestCase):

    def test_channel_request_sends_agc_off_and_zero_gain(self):
        rx = ReceiverManager(
            config={"paths": {}, "station": {}, "processing": {}},
            radiod_block={"status": "test.local"},
            spool_root=Path("/tmp/psk-test-spool"),
            log_dir=Path("/tmp/psk-test-log"),
            radiod_lifetime_frames=0,
        )
        control = mock.Mock()
        control.ensure_channel.return_value = SimpleNamespace(
            multicast_address="239.1.1.1", port=5004, ssrc=1)
        rx._control = control
        sink = mock.Mock(frequency_hz=14_074_000, preset="usb",
                         sample_rate=12000, encoding=4)
        multi = mock.Mock()
        with mock.patch("ka9q.MultiStream", return_value=multi):
            rx._add_sink_to_multi(sink, {})

        ens = control.ensure_channel.call_args.kwargs
        add = multi.add_channel.call_args.kwargs
        for name, kw in (("ensure_channel", ens), ("MultiStream.add_channel", add)):
            self.assertEqual(kw.get("agc_enable"), 0, name)
            self.assertEqual(kw.get("gain"), 0.0, name)


if __name__ == "__main__":
    unittest.main()
