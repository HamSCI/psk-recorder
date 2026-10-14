"""psk-recorder's two reporter-id sites (sigmond tasks/spec-reporter-id-v372.md
§5): the daemon's --instance/--radiod-id, which arrive as %i in systemd's
spelling (P1, P4), and the env file a sigmond call names (P5)."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from psk_recorder import configurator  # noqa: E402
from psk_recorder.cli import _normalize_instance_args  # noqa: E402


class InstanceArgTests(unittest.TestCase):

    def test_systemd_spelling_becomes_storage(self):
        ns = SimpleNamespace(instance="AC0G\\x3dB4", radiod_id="AC0G\\x3dB4")
        _normalize_instance_args(ns)
        self.assertEqual((ns.instance, ns.radiod_id), ("AC0G=B4", "AC0G=B4"))

    def test_every_escape_is_decoded_not_only_x3d(self):
        ns = SimpleNamespace(instance="W1ABC\\x2d5", radiod_id=None)
        _normalize_instance_args(ns)
        self.assertEqual(ns.instance, "W1ABC-5")
        self.assertIsNone(ns.radiod_id)

    def test_plain_names_pass_through(self):
        ns = SimpleNamespace(instance="AC0G-B4", radiod_id="bee1-status.local")
        _normalize_instance_args(ns)
        self.assertEqual((ns.instance, ns.radiod_id), ("AC0G-B4", "bee1-status.local"))


class EnvFilePathTests(unittest.TestCase):

    def test_env_file_path_takes_any_spelling(self):
        for typed in ("AC0G\\x3dB4", "AC0G/B4", "AC0G=B4", "ac0g/b4"):
            with self.subTest(typed=typed):
                self.assertEqual(configurator._env_file_path(typed),
                                 configurator._ENV_DIR / "AC0G=B4.env")
        self.assertEqual(configurator._env_file_path("bee1-status.local"),
                         configurator._ENV_DIR / "bee1-status.local.env")

    def test_env_file_path_falls_back_to_a_legacy_file_with_a_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_dir = Path(tmp)
            (env_dir / "my-rx888.env").write_text("")
            err = io.StringIO()
            with mock.patch.object(configurator, "_ENV_DIR", env_dir), \
                 contextlib.redirect_stderr(err):
                got = configurator._env_file_path("my-rx888")
            self.assertEqual(got, env_dir / "my-rx888.env")
            self.assertIn("legacy instance name my-rx888; run: smd admin instance migrate",
                          err.getvalue())
            with mock.patch.object(configurator, "_ENV_DIR", env_dir):
                self.assertEqual(configurator._env_file_path("my-rx999"),
                                 env_dir / "MY-RX999.env")


if __name__ == "__main__":
    unittest.main()
