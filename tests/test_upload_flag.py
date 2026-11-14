"""PSK_USE_HS_UPLOADER — sigmond's per-instance upload on/off flag.

`smd config upload psk-recorder --off` writes PSK_USE_HS_UPLOADER=0.
sigmond treats 0/false/no/off as an order to stop and anything else
(including unset) as on (lib/sigmond/sink_carryover.py _OFF_WORDS).
Off is a bring-up hold: decode and stage rows into the sink exactly as
when on, start no uploader of psk's own.  sigmond's sink switch (fill)
holds the staged rows.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from psk_recorder.core import recorder as rec_mod
from psk_recorder.core.recorder import PskRecorder


def _make():
    cfg = {
        "paths": {"spool_dir": "/tmp/psk-test", "log_dir": "/tmp/psk-test"},
        "station": {"callsign": "AC0G", "grid_square": "EM38ww"},
        "processing": {"radiod_lifetime_frames": 0},
    }
    r = PskRecorder(cfg, {"status": "host"})
    for rx in r._receivers:
        rx.start_ch_tailers = mock.Mock()
    return r


def _env(value):
    env = {k: v for k, v in os.environ.items()
           if k not in ("PSK_USE_HS_UPLOADER", "PSK_DELIVERY_PIPELINES",
                        "PSK_DELIVERY_MODE")}
    if value is not None:
        env["PSK_USE_HS_UPLOADER"] = value
    return env


class UploadEnabledTests(unittest.TestCase):

    def test_off_words(self):
        for v in ("0", "false", "No", "OFF", " 0 "):
            with mock.patch.dict(os.environ, _env(v), clear=True):
                self.assertFalse(rec_mod._upload_enabled(), v)

    def test_on_and_default(self):
        for v in (None, "", "1", "true", "yes", "on"):
            with mock.patch.dict(os.environ, _env(v), clear=True):
                self.assertTrue(rec_mod._upload_enabled(), repr(v))


class HandoffTests(unittest.TestCase):

    def _run(self, value, pipelines="direct"):
        env = _env(value)
        env["PSK_DELIVERY_PIPELINES"] = pipelines
        r = _make()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(rec_mod, "HsPskReporterUploader") as up, \
                mock.patch.object(rec_mod, "PskCycleBatcher") as batcher:
            r._start_uploaders()
            r._start_all_ch_tailers()
        return r, up, batcher

    def test_on_reaches_upload_path(self):
        r, up, batcher = self._run("1")
        up.return_value.start.assert_called_once()
        for rx in r._receivers:
            rx.start_ch_tailers.assert_called_once()

    def test_absent_reaches_upload_path(self):
        r, up, batcher = self._run(None)
        up.return_value.start.assert_called_once()
        for rx in r._receivers:
            rx.start_ch_tailers.assert_called_once()

    def test_off_stages_rows_but_starts_no_uploader(self):
        r, up, batcher = self._run("0")
        up.assert_not_called()
        for rx in r._receivers:
            rx.start_ch_tailers.assert_called_once()
