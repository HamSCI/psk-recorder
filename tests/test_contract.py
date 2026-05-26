"""Contract v0.3 compliance tests for psk-recorder.

Tests that inventory --json and validate --json:
1. Emit clean JSON to stdout (no banners, no logging lines)
2. Include all required v0.3 fields
3. Report correct contract_version
4. Surface log_paths and log_level (v0.3 §10, §11)
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_CONFIG = FIXTURES / "test-config.toml"


class StdoutCleanlinessTests(unittest.TestCase):
    """Contract v0.3 §3: stdout must contain ONLY JSON, no banners."""

    def _run_subcommand(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PSK_RECORDER_CONFIG"] = str(TEST_CONFIG)
        env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "psk_recorder", *args,
             "--config", str(TEST_CONFIG)],
            capture_output=True, text=True, timeout=10,
            env=env,
            cwd=str(REPO_ROOT),
        )

    def test_inventory_stdout_is_valid_json(self):
        proc = self._run_subcommand("inventory", "--json")
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, dict)

    def test_inventory_stdout_no_banner(self):
        """No 'Logging configured' or similar text before JSON."""
        proc = self._run_subcommand("inventory", "--json")
        stdout = proc.stdout.strip()
        self.assertTrue(
            stdout.startswith("{"),
            f"stdout does not start with '{{': {stdout[:80]!r}",
        )

    def test_validate_stdout_is_valid_json(self):
        proc = self._run_subcommand("validate", "--json")
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, dict)
        self.assertIn("ok", data)

    def test_version_stdout_is_valid_json(self):
        proc = self._run_subcommand("version", "--json")
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data["client"], "psk-recorder")


class InventoryV03Tests(unittest.TestCase):
    """Contract v0.3 field coverage."""

    @classmethod
    def setUpClass(cls):
        env = os.environ.copy()
        env["PSK_RECORDER_CONFIG"] = str(TEST_CONFIG)
        env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "psk_recorder",
             "inventory", "--json", "--config", str(TEST_CONFIG)],
            capture_output=True, text=True, timeout=10,
            env=env,
            cwd=str(REPO_ROOT),
        )
        cls.data = json.loads(proc.stdout)

    def test_client_name(self):
        self.assertEqual(self.data["client"], "psk-recorder")

    def test_contract_version(self):
        self.assertEqual(self.data["contract_version"], "0.7")

    def test_timing_authority_applied_explicit_null(self):
        """CONTRACT v0.7 §3/§18 — runtime-state field for the §18
        subscription. psk-recorder runs in RTP-default mode (PSK
        decoding is ms-tolerant; no hard-deadline scheduling), so
        the field is present and explicitly None — distinguishes
        contract-aware-in-default-mode from a pre-v0.7 client."""
        inst = self.data["instances"][0]
        self.assertIn("timing_authority_applied", inst)
        self.assertIsNone(inst["timing_authority_applied"])

    def test_data_sinks_present_v0_6(self):
        """CONTRACT v0.6 §17.3: every instance has a data_sinks array."""
        inst = self.data["instances"][0]
        self.assertIn("data_sinks", inst)
        sinks = inst["data_sinks"]
        kinds = {s["kind"] for s in sinks}
        # File sinks always declared (spool + log dir).
        self.assertIn("file", kinds)
        # psk-recorder writes to the local SQLite sink (file-based);
        # there is no ClickHouse sink.
        self.assertNotIn("clickhouse", kinds)
        for sink in sinks:
            self.assertIn("kind", sink)
            self.assertIn("target", sink)
            self.assertIn("retention_days", sink)
            self.assertIn("mb_per_day", sink)

    def test_has_config_path(self):
        self.assertIn("config_path", self.data)

    def test_has_instances(self):
        self.assertIsInstance(self.data["instances"], list)
        self.assertGreater(len(self.data["instances"]), 0)

    def test_instance_fields(self):
        inst = self.data["instances"][0]
        self.assertEqual(inst["instance"], "test-rx888")
        # RADIOD-IDENTIFICATION.md §3.2: inventory radiod_id is the
        # mDNS multicast status name, not the local `[[radiod]] id`
        # label.  The fixture's [[radiod]] block has
        # radiod_status="test-status.local", so that's the value
        # exposed via inventory.
        self.assertEqual(inst["radiod_id"], "test-status.local")
        self.assertEqual(inst["radiod_status_dns"], "test-status.local")
        self.assertIn("data_destination", inst)
        self.assertIn("ka9q_channels", inst)
        self.assertEqual(inst["ka9q_channels"], 4)
        self.assertIn("chain_delay_ns_applied", inst)
        self.assertIn("modes", inst)
        self.assertIn("ft8", inst["modes"])
        self.assertIn("ft4", inst["modes"])

    def test_frequencies(self):
        inst = self.data["instances"][0]
        freqs = inst["frequencies_hz"]
        self.assertIn(14074000, freqs)
        self.assertIn(7074000, freqs)
        self.assertIn(14080000, freqs)
        self.assertIn(7047500, freqs)

    def test_inventory_radiod_id_falls_back_when_no_status(self):
        """RADIOD-IDENTIFICATION.md §3.2 — when a legacy config has no
        radiod_status / status_address declared, inventory radiod_id
        falls back to the local `[[radiod]] id` label so the output is
        still parseable.  Greenfield configs (which always set status)
        get the multicast name in radiod_id; legacy configs keep the
        old behavior under the migration window."""
        from psk_recorder.contract import build_inventory
        legacy_config = {
            "station": {"callsign": "AC0G"},
            "paths": {"log_dir": "/tmp/log", "spool_dir": "/tmp/spool"},
            "radiod": [{
                "id": "legacy-label",
                # NO radiod_status field.
                "ft8": {"freqs_hz": [14074000]},
            }],
        }
        payload = build_inventory(legacy_config, Path("/tmp/legacy.toml"))
        inst = payload["instances"][0]
        self.assertEqual(inst["radiod_id"], "legacy-label")
        self.assertEqual(inst["radiod_status_dns"], "")

    def test_log_paths_present(self):
        """§10: log_paths must be present and list the spot-log files.

        Process log is journal-routed (StandardOutput=journal in the
        systemd unit), so the ``process`` key intentionally does not
        appear in log_paths — only file-based logs are listed (for
        ``smd log --files``).  See the log_paths builder in
        ``src/psk_recorder/contract.py`` for the design rationale.
        """
        self.assertIn("log_paths", self.data)
        log_paths = self.data["log_paths"]
        self.assertIn("test-rx888", log_paths)
        self.assertIn("spots", log_paths["test-rx888"])
        self.assertNotIn("process", log_paths["test-rx888"])

    def test_log_level_present(self):
        """v0.3 §11: log_level must be present."""
        self.assertIn("log_level", self.data)
        self.assertIn(self.data["log_level"], [
            "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL",
        ])

    def test_deps_present(self):
        self.assertIn("deps", self.data)
        self.assertIn("git", self.data["deps"])
        self.assertIn("pypi", self.data["deps"])

    def test_issues_is_list(self):
        self.assertIsInstance(self.data["issues"], list)


class ValidateTests(unittest.TestCase):

    def _run_validate(self, config_path=TEST_CONFIG):
        env = os.environ.copy()
        env["PSK_RECORDER_CONFIG"] = str(config_path)
        env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "psk_recorder",
             "validate", "--json", "--config", str(config_path)],
            capture_output=True, text=True, timeout=10,
            env=env,
            cwd=str(REPO_ROOT),
        )

    def test_valid_config_returns_ok(self):
        proc = self._run_validate()
        data = json.loads(proc.stdout)
        self.assertIn("ok", data)
        self.assertIsInstance(data["issues"], list)

    def test_missing_config_returns_fail(self):
        proc = self._run_validate(Path("/nonexistent/config.toml"))
        data = json.loads(proc.stdout)
        self.assertFalse(data["ok"])
        self.assertEqual(proc.returncode, 1)


class ConfigTests(unittest.TestCase):
    """Config loader tests."""

    def test_load_test_config(self):
        from psk_recorder.config import load_config
        config = load_config(TEST_CONFIG)
        self.assertEqual(config["station"]["callsign"], "AC0G")

    def test_resolve_radiod_block(self):
        from psk_recorder.config import load_config, resolve_radiod_block
        config = load_config(TEST_CONFIG)
        block = resolve_radiod_block(config, "test-rx888")
        self.assertEqual(block["id"], "test-rx888")
        self.assertEqual(block["radiod_status"], "test-status.local")

    def test_resolve_radiod_block_missing(self):
        from psk_recorder.config import load_config, resolve_radiod_block
        config = load_config(TEST_CONFIG)
        with self.assertRaises(ValueError):
            resolve_radiod_block(config, "nonexistent")

    def test_single_radiod_no_id_required(self):
        from psk_recorder.config import load_config, resolve_radiod_block
        config = load_config(TEST_CONFIG)
        block = resolve_radiod_block(config, None)
        self.assertEqual(block["id"], "test-rx888")

    def test_get_freqs(self):
        from psk_recorder.config import get_freqs, load_config, resolve_radiod_block
        config = load_config(TEST_CONFIG)
        block = resolve_radiod_block(config, "test-rx888")
        ft8 = get_freqs(block, "ft8")
        self.assertEqual(ft8, [14074000, 7074000])
        ft4 = get_freqs(block, "ft4")
        self.assertEqual(ft4, [14080000, 7047500])


class RadiodSchemaPhase3Tests(unittest.TestCase):
    """RADIOD-IDENTIFICATION.md §3.1 — new `status` field acceptance.

    Phase 3 of the canonical-naming cleanup adds `[[radiod]] status`
    as the primary identifier (mDNS multicast name).  Legacy fields
    (`id`, `radiod_status`, RADIOD_<ID>_STATUS env var) are still
    accepted during the deprecation window but emit
    DeprecationWarning."""

    def test_resolve_status_prefers_new_field_no_warning(self):
        import warnings
        from psk_recorder.config import resolve_radiod_status
        block = {
            "status": "bee1-status.local",
            # Legacy fields ignored when `status` is present.
            "id": "should-be-ignored",
            "radiod_status": "should-be-ignored-too.local",
        }
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = resolve_radiod_status(block)
        self.assertEqual(result, "bee1-status.local")
        # No DeprecationWarning when the new field is used.
        self.assertEqual(
            [warning for warning in w
             if issubclass(warning.category, DeprecationWarning)],
            [])

    def test_resolve_status_legacy_radiod_status_warns(self):
        import warnings
        from psk_recorder.config import resolve_radiod_status
        block = {
            "id": "legacy",
            "radiod_status": "legacy.local",
        }
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = resolve_radiod_status(block)
        self.assertEqual(result, "legacy.local")
        self.assertTrue(any(
            issubclass(warning.category, DeprecationWarning)
            and "radiod_status" in str(warning.message)
            for warning in w))

    def test_resolve_block_matches_status_field(self):
        """A block with only `status` matches when caller passes the
        multicast name."""
        from psk_recorder.config import resolve_radiod_block
        config = {"radiod": [
            {"status": "bee1-status.local", "ft8": {"freqs_hz": [14074000]}},
            {"status": "other.local", "ft8": {"freqs_hz": [7074000]}},
        ]}
        block = resolve_radiod_block(config, "bee1-status.local")
        self.assertEqual(block["status"], "bee1-status.local")

    def test_resolve_block_legacy_id_match_warns(self):
        import warnings
        from psk_recorder.config import resolve_radiod_block
        config = {"radiod": [
            {"id": "my-rx888", "ft8": {"freqs_hz": [14074000]}},
        ]}
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            block = resolve_radiod_block(config, "my-rx888")
        self.assertEqual(block["id"], "my-rx888")
        self.assertTrue(any(
            issubclass(warning.category, DeprecationWarning)
            and "legacy `id`" in str(warning.message)
            for warning in w))


if __name__ == "__main__":
    unittest.main()
