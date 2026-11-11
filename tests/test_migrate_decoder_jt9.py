"""scripts/migrate-decoder-jt9.sh moves an existing config from decode_ft8 to
jt9 once, and never again (rob, 2026-11-11)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/migrate-decoder-jt9.sh"
OLD = '[paths]\nspool_dir = "/x"\ndecoder_kind        = "decode_ft8"\ndecoder_decode_ft8  = "/usr/local/bin/decode_ft8"\n'


class Migrate(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cfg = self.d / "psk-recorder-config.toml"

    def run_it(self):
        return subprocess.run(["bash", str(SCRIPT), str(self.cfg)],
                              capture_output=True, text=True)

    def test_decode_ft8_becomes_jt9_with_backup(self):
        self.cfg.write_text(OLD)
        self.assertEqual(self.run_it().returncode, 0)
        new = self.cfg.read_text()
        self.assertIn('decoder_kind        = "jt9"', new)
        self.assertIn('decoder_decode_ft8  = "/usr/local/bin/decode_ft8"', new)
        self.assertEqual((self.d / "psk-recorder-config.toml.pre-jt9").read_text(), OLD)
        self.assertTrue((self.d / ".decoder-jt9-migrated").exists())

    def test_runs_once_a_later_decode_ft8_choice_stays(self):
        self.cfg.write_text(OLD)
        self.run_it()
        self.cfg.write_text(OLD)              # operator chooses decode_ft8 again
        self.run_it()
        self.assertIn('"decode_ft8"', self.cfg.read_text().split("decoder_kind")[1].split("\n")[0])

    def test_jt9_config_untouched_but_marked(self):
        txt = OLD.replace('"decode_ft8"\ndecoder_decode', '"jt9"\ndecoder_decode', 1)
        self.cfg.write_text(txt)
        self.run_it()
        self.assertEqual(self.cfg.read_text(), txt)
        self.assertFalse((self.d / "psk-recorder-config.toml.pre-jt9").exists())
        self.assertTrue((self.d / ".decoder-jt9-migrated").exists())

    def test_no_config_does_nothing(self):
        self.assertEqual(self.run_it().returncode, 0)
        self.assertFalse((self.d / ".decoder-jt9-migrated").exists())

    def test_install_sh_calls_it(self):
        s = (SCRIPT.parent / "install.sh").read_text()
        self.assertIn('migrate-decoder-jt9.sh" "$CONFIG_FILE"', s)


if __name__ == "__main__":
    unittest.main()
