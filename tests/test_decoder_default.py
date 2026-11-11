"""jt9 (the UberSDR build over shared memory) is the default FT8/FT4 decoder;
decode_ft8 (ka9q/ft8_lib) is the opt-in low-CPU choice and the fallback when
jt9_decode is not installed (rob, 2026-11-11)."""
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from psk_recorder.config import DEFAULTS
from psk_recorder.core import recorder as rec_mod

REPO = Path(__file__).resolve().parent.parent


def _resolve(paths):
    """Run the decoder resolution in _provision_all_receivers with no
    receivers, and return the (kind, path) it logged."""
    r = object.__new__(rec_mod.PskRecorder)
    r._paths = paths
    r._receivers = []
    r._lifetime_entries = []
    with unittest.TestCase().assertLogs(rec_mod.logger, "INFO") as cm:
        r._provision_all_receivers()
    line = next(m for m in cm.output if "decoder_kind=" in m and " path=" in m)
    kind = line.split("decoder_kind=")[1].split()[0]
    path = line.split(" path=")[1].split()[0]
    _resolve.warned = any(m.startswith("WARNING") for m in cm.output)
    return kind, path


class DecoderDefault(unittest.TestCase):
    def test_defaults_pick_jt9(self):
        self.assertEqual(DEFAULTS["paths"]["decoder_kind"], "jt9")

    def test_template_picks_jt9(self):
        t = tomllib.loads((REPO / "config/psk-recorder-config.toml.template")
                          .read_text())
        self.assertEqual(t["paths"]["decoder_kind"], "jt9")
        self.assertEqual(t["paths"]["decoder_jt9"], "/usr/local/bin/jt9_decode")

    def test_wizard_default_is_jt9(self):
        s = (REPO / "scripts/config-wizard.sh").read_text()
        self.assertIn('[[ -z "$decoder_kind" ]] && decoder_kind="jt9"', s)


class DecoderResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jt9 = os.path.join(self.tmp.name, "jt9_decode")
        Path(self.jt9).write_text("#!/bin/sh\n")
        os.chmod(self.jt9, stat.S_IRWXU)

    def test_unset_kind_uses_jt9_when_installed(self):
        self.assertEqual(_resolve({"decoder_jt9": self.jt9}), ("jt9", self.jt9))

    def test_missing_jt9_decode_falls_back_to_decode_ft8(self):
        missing = os.path.join(self.tmp.name, "nope")
        kind, path = _resolve({"decoder_jt9": missing,
                               "decoder_decode_ft8": "/x/decode_ft8"})
        self.assertEqual((kind, path), ("decode_ft8", "/x/decode_ft8"))
        self.assertTrue(_resolve.warned, "the fallback must be logged")

    def test_explicit_decode_ft8_is_honoured(self):
        self.assertEqual(
            _resolve({"decoder_kind": "decode_ft8", "decoder_jt9": self.jt9,
                      "decoder_decode_ft8": "/x/decode_ft8"}),
            ("decode_ft8", "/x/decode_ft8"))


if __name__ == "__main__":
    unittest.main()
