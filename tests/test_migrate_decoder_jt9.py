"""scripts/migrate-decoder-jt9.sh moves every psk-recorder config in the
config dir from decode_ft8 to jt9, once (rob, 2026-11-11)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/migrate-decoder-jt9.sh"
OLD = '[paths]\nspool_dir = "/x"\ndecoder_kind        = "decode_ft8"\ndecoder_decode_ft8  = "/usr/local/bin/decode_ft8"\n'
JT9 = OLD.replace('= "decode_ft8"\ndecoder_decode', '= "jt9"\ndecoder_decode', 1)


class Migrate(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.inst = self.d / "AI6VN-1.toml"
        self.single = self.d / "psk-recorder-config.toml"

    def run_it(self, arg=None):
        return subprocess.run(["bash", str(SCRIPT), str(arg or self.d)],
                              capture_output=True, text=True)

    def kind(self, f):
        return f.read_text().split("decoder_kind")[1].split("\n")[0]

    def test_per_instance_config_moves(self):
        # AI6VN-1, 2026-11-11: the live config is <instance>.toml
        self.inst.write_text(OLD)
        self.assertEqual(self.run_it().returncode, 0)
        self.assertIn('"jt9"', self.kind(self.inst))
        self.assertEqual((self.d / "AI6VN-1.toml.pre-jt9").read_text(), OLD)
        self.assertTrue((self.d / ".decoder-jt9-migrated-v2").exists())

    def test_every_config_moves(self):
        self.inst.write_text(OLD)
        self.single.write_text(OLD)
        self.run_it()
        self.assertIn('"jt9"', self.kind(self.inst))
        self.assertIn('"jt9"', self.kind(self.single))

    def test_the_first_versions_marker_does_not_block(self):
        (self.d / ".decoder-jt9-migrated").write_text("x\n")
        self.single.write_text(JT9)
        self.inst.write_text(OLD)
        self.run_it()
        self.assertIn('"jt9"', self.kind(self.inst))

    def test_an_old_caller_passing_the_file_still_works(self):
        self.inst.write_text(OLD)
        self.single.write_text(OLD)
        self.run_it(self.single)
        self.assertIn('"jt9"', self.kind(self.inst))

    def test_runs_once_a_later_decode_ft8_choice_stays(self):
        self.inst.write_text(OLD)
        self.run_it()
        self.inst.write_text(OLD)              # operator chooses decode_ft8 again
        self.run_it()
        self.assertIn('"decode_ft8"', self.kind(self.inst))

    def test_backup_is_never_overwritten(self):
        self.inst.write_text(OLD)
        (self.d / "AI6VN-1.toml.pre-jt9").write_text("first backup\n")
        self.run_it()
        self.assertEqual((self.d / "AI6VN-1.toml.pre-jt9").read_text(), "first backup\n")

    def test_jt9_configs_untouched_but_marked(self):
        self.inst.write_text(JT9)
        self.run_it()
        self.assertEqual(self.inst.read_text(), JT9)
        self.assertFalse((self.d / "AI6VN-1.toml.pre-jt9").exists())
        self.assertTrue((self.d / ".decoder-jt9-migrated-v2").exists())

    def test_install_sh_runs_it_on_the_config_dir(self):
        s = (SCRIPT.parent / "install.sh").read_text()
        self.assertIn('migrate-decoder-jt9.sh" "$CONFIG_DIR"', s)


if __name__ == "__main__":
    unittest.main()
