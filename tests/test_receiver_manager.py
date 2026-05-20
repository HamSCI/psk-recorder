"""Tests for psk_recorder.core.receiver_manager.

Covers the Phase B refactor of per-radiod state out of PskRecorder
into ReceiverManager.  These tests deliberately avoid importing
ka9q (the heavy provisioning path requires the C extension) — they
exercise the construction, accessor, and shutdown surfaces only.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from psk_recorder.core.receiver_manager import ReceiverManager


def _make_rx(radiod_block, *, lifetime=0):
    return ReceiverManager(
        config={"paths": {}, "station": {}, "processing": {}},
        radiod_block=radiod_block,
        spool_root=Path("/tmp/psk-test-spool"),
        log_dir=Path("/tmp/psk-test-log"),
        radiod_lifetime_frames=lifetime,
    )


class ConstructorTests(unittest.TestCase):

    def test_basic_construction(self):
        rx = _make_rx({"id": "rx888", "radiod_status": "rx888.local"})
        self.assertEqual(rx.radiod_id, "rx888")
        # rx_source derives from radiod_status (not radiod_id) so it
        # matches sigmond.sources.SourceKey form across clients.
        self.assertEqual(rx.rx_source, "radiod:rx888.local")
        # Sinks / multi_streams / lifetime_entries / ch_tailers all
        # start empty — provisioning is lazy and only happens via
        # provision_channels(), which the heavy ka9q import path.
        self.assertEqual(rx.sinks, [])
        self.assertEqual(rx.lifetime_entries, [])

    def test_unresolvable_radiod_block_falls_back_to_id_only_key(self):
        """A block missing radiod_status (and no env override) still
        constructs — provisioning will fail later, but the manager's
        rx_source falls back to ``radiod:<radiod_id>`` so logging and
        diagnostics aren't broken."""
        rx = _make_rx({"id": "bare"})
        self.assertEqual(rx.radiod_id, "bare")
        self.assertEqual(rx.rx_source, "radiod:bare")

    def test_spool_root_is_per_radiod(self):
        """The spool directory the manager reports is the radiod-scoped
        subdirectory of the recorder-wide spool root.  Multi-source
        deployments rely on this to keep per-radiod callhash tables
        and slot artifacts from colliding."""
        rx = _make_rx({"id": "alpha", "radiod_status": "alpha.local"})
        # _spool_root is private but its derivation is part of the
        # contract — assert the path includes the radiod_id segment.
        self.assertTrue(str(rx._spool_root).endswith("/alpha"))


class StopIsIdempotentTests(unittest.TestCase):

    def test_stop_without_provision_is_a_noop(self):
        """Calling stop() on a manager that never provisioned must
        not raise — covers shutdown paths where provisioning failed
        partway through (e.g. radiod not reachable)."""
        rx = _make_rx({"id": "rx", "radiod_status": "rx.local"})
        rx.stop()  # no exception
        rx.stop()  # second call also fine

    def test_stop_closes_log_fds(self):
        """Log fds (the only "real" resource the manager opens before
        provisioning would normally) are closed cleanly even when no
        sinks / multistreams / tailers exist."""
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td)
            rx = ReceiverManager(
                config={"paths": {}, "station": {}, "processing": {}},
                radiod_block={"id": "x", "radiod_status": "x.local"},
                spool_root=Path(td) / "spool",
                log_dir=log_dir,
                radiod_lifetime_frames=0,
            )
            # Manually open a log fd to simulate provision_channels
            # without invoking ka9q.
            log_path = log_dir / "x-ft8.log"
            rx._log_fds["ft8"] = open(log_path, "a", encoding="utf-8")
            self.assertFalse(rx._log_fds["ft8"].closed)
            rx.stop()
            # _log_fds dict is cleared (no dangling references).
            self.assertEqual(rx._log_fds, {})


class PskRecorderMultiSourceTests(unittest.TestCase):
    """Verify PskRecorder accepts both the legacy single-block and
    the new list-of-blocks signature, and creates one ReceiverManager
    per source.
    """

    def _cfg(self):
        return {
            "paths": {
                "spool_dir": "/tmp/psk-test-spool",
                "log_dir": "/tmp/psk-test-log",
            },
            "station": {},
            "processing": {"radiod_lifetime_frames": 0},
        }

    def test_single_dict_legacy_signature(self):
        from psk_recorder.core.recorder import PskRecorder
        rec = PskRecorder(
            self._cfg(),
            {"id": "solo", "radiod_status": "solo.local"},
        )
        self.assertEqual(len(rec.receivers), 1)
        self.assertEqual(rec.receivers[0].radiod_id, "solo")

    def test_list_of_blocks_multi_source(self):
        from psk_recorder.core.recorder import PskRecorder
        rec = PskRecorder(
            self._cfg(),
            [
                {"id": "local", "radiod_status": "local.local"},
                {"id": "bee1", "radiod_status": "bee1.local"},
                {"id": "bee2", "radiod_status": "bee2.local"},
            ],
        )
        self.assertEqual(len(rec.receivers), 3)
        self.assertEqual(
            [rx.radiod_id for rx in rec.receivers],
            ["local", "bee1", "bee2"],
        )
        # rx_source on each is the canonical form, distinct per source.
        self.assertEqual(
            [rx.rx_source for rx in rec.receivers],
            [
                "radiod:local.local",
                "radiod:bee1.local",
                "radiod:bee2.local",
            ],
        )

    def test_empty_list_rejected(self):
        from psk_recorder.core.recorder import PskRecorder
        with self.assertRaises(ValueError):
            PskRecorder(self._cfg(), [])


if __name__ == "__main__":
    unittest.main()
