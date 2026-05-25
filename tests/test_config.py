"""Tests for psk_recorder.config helpers.

Covers the Phase A plumbing for multi-source psk-recorder:
  * ``derive_source_key`` — canonical ``radiod:<status_address>`` form
    matching wspr-recorder's ``SourceConfig.key`` and
    ``sigmond.sources.SourceKey``.
  * ``ensure_sources`` — synthesise the per-source descriptor list
    from ``[[radiod]]`` blocks.  Used by future multi-rx daemon
    bootstrap; today each daemon still serves one radiod.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from psk_recorder.config import (
    DEFAULT_CONFIG_PATH,
    PER_INSTANCE_CONFIG_DIR,
    derive_source_key,
    ensure_sources,
    extract_reporter_id,
    resolve_config_path,
    resolve_radiod_status,
)


class TestDeriveSourceKey(unittest.TestCase):

    def test_from_radiod_status_field(self):
        block = {"id": "bee1", "radiod_status": "bee1-status.local"}
        self.assertEqual(derive_source_key(block), "radiod:bee1-status.local")

    def test_env_override_wins(self):
        block = {"id": "bee1", "radiod_status": "stale-status.local"}
        env_key = "RADIOD_BEE1_STATUS"
        old = os.environ.get(env_key)
        try:
            os.environ[env_key] = "fresh-status.local"
            self.assertEqual(
                derive_source_key(block), "radiod:fresh-status.local",
            )
        finally:
            if old is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = old

    def test_missing_status_raises(self):
        block = {"id": "no-status"}
        with self.assertRaises(ValueError):
            derive_source_key(block)


class TestEnsureSources(unittest.TestCase):

    def test_single_radiod_dict_form(self):
        """TOML's ``[radiod]`` (single dict) is accepted alongside
        ``[[radiod]]`` (list of dicts)."""
        config = {
            "radiod": {"id": "rx888", "radiod_status": "rx888.local"},
        }
        sources = ensure_sources(config)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["key"], "radiod:rx888.local")
        self.assertEqual(sources[0]["radiod_id"], "rx888")
        self.assertEqual(sources[0]["status_address"], "rx888.local")
        # The original block is preserved so per-source freq lookups
        # (get_freqs, get_mode_params) keep working unchanged.
        self.assertIs(sources[0]["radiod_block"], config["radiod"])

    def test_multiple_radiod_blocks(self):
        config = {
            "radiod": [
                {"id": "local",
                 "radiod_status": "local-status.local"},
                {"id": "bee1",
                 "radiod_status": "bee1-status.local"},
                {"id": "bee2",
                 "radiod_status": "bee2-status.local"},
            ],
        }
        sources = ensure_sources(config)
        keys = [s["key"] for s in sources]
        self.assertEqual(keys, [
            "radiod:local-status.local",
            "radiod:bee1-status.local",
            "radiod:bee2-status.local",
        ])

    def test_unresolvable_block_skipped_not_raised(self):
        """A block missing radiod_status (and no env override) is
        silently skipped — callers should rely on
        ``resolve_radiod_block`` to surface that as a hard error when
        the block is actually selected."""
        config = {
            "radiod": [
                {"id": "good", "radiod_status": "good.local"},
                {"id": "bad"},                          # no status
            ],
        }
        sources = ensure_sources(config)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["radiod_id"], "good")

    def test_empty_config(self):
        self.assertEqual(ensure_sources({}), [])
        self.assertEqual(ensure_sources({"radiod": []}), [])


class TestResolveRadiodStatusContract(unittest.TestCase):
    """derive_source_key must call resolve_radiod_status, so the env
    override path is the single source of truth.  Anchored here in
    case someone "optimises" derive_source_key to read the field
    directly later."""

    def test_status_matches_resolver(self):
        block = {"id": "anchored", "radiod_status": "anchored.local"}
        self.assertEqual(
            derive_source_key(block),
            f"radiod:{resolve_radiod_status(block)}",
        )


class TestResolveConfigPath(unittest.TestCase):
    """Per-instance config resolution per sigmond MULTI-INSTANCE-ARCHITECTURE.md §4."""

    def setUp(self):
        self._old_env = os.environ.get("PSK_RECORDER_CONFIG")
        os.environ.pop("PSK_RECORDER_CONFIG", None)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("PSK_RECORDER_CONFIG", None)
        else:
            os.environ["PSK_RECORDER_CONFIG"] = self._old_env

    def test_explicit_path_wins(self):
        explicit = Path("/tmp/some-config.toml")
        result = resolve_config_path(instance="AC0G-B1", explicit_path=explicit)
        self.assertEqual(result, explicit)

    def test_env_var_wins_over_instance(self):
        os.environ["PSK_RECORDER_CONFIG"] = "/tmp/from-env.toml"
        result = resolve_config_path(instance="AC0G-B1")
        self.assertEqual(result, Path("/tmp/from-env.toml"))

    def test_per_instance_when_file_exists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            instance_file = Path(tmp) / "AC0G-B1.toml"
            instance_file.write_text("[instance]\nreporter_id = 'AC0G-B1'\n")
            # Monkey-patch PER_INSTANCE_CONFIG_DIR for the test
            from psk_recorder import config as cfg_mod
            old = cfg_mod.PER_INSTANCE_CONFIG_DIR
            cfg_mod.PER_INSTANCE_CONFIG_DIR = Path(tmp)
            try:
                result = resolve_config_path(instance="AC0G-B1")
                self.assertEqual(result, instance_file)
            finally:
                cfg_mod.PER_INSTANCE_CONFIG_DIR = old

    def test_deprecation_warning_when_instance_file_missing(self):
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_config_path(instance="NEVER-EXISTS-XYZ")
        self.assertEqual(result, DEFAULT_CONFIG_PATH)
        self.assertTrue(
            any(issubclass(w.category, DeprecationWarning) for w in caught),
            f"expected DeprecationWarning, got {[w.category for w in caught]}",
        )

    def test_silent_fallback_when_no_instance(self):
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_config_path()
        self.assertEqual(result, DEFAULT_CONFIG_PATH)
        self.assertFalse(
            any(issubclass(w.category, DeprecationWarning) for w in caught),
            "no instance arg = pre-instance world; should not warn",
        )


class TestExtractReporterId(unittest.TestCase):

    def test_present(self):
        self.assertEqual(
            extract_reporter_id({"instance": {"reporter_id": "AC0G-B1"}}),
            "AC0G-B1",
        )

    def test_missing_block(self):
        self.assertIsNone(extract_reporter_id({"paths": {}}))

    def test_block_without_key(self):
        self.assertIsNone(extract_reporter_id({"instance": {"antenna": "loop"}}))

    def test_empty_string(self):
        self.assertIsNone(extract_reporter_id({"instance": {"reporter_id": ""}}))

    def test_non_string(self):
        self.assertIsNone(extract_reporter_id({"instance": {"reporter_id": 42}}))


if __name__ == "__main__":
    unittest.main()
