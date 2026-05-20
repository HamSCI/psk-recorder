"""Tests for PSK_DELIVERY_PIPELINES resolution (Phase D Cut 3).

Replaces the Phase-2-era ``test_delivery_mode.py`` that exercised the
single-string ``PSK_DELIVERY_MODE`` enum.  The new resolver returns a
canonical-order tuple of pipeline names from ``PSK_DELIVERY_PIPELINES``,
with backward-compat translation from the legacy env var.
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

# Stub heavy deps that recorder.py pulls in transitively.
for _name in ("numpy",):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

from psk_recorder.core.recorder import (  # noqa: E402
    _LEGACY_MODE_TO_PIPELINES,
    _VALID_DELIVERY_PIPELINES,
    _resolve_delivery_pipelines,
)


def _clean_env(monkeypatch=None):
    """Drop both pipeline env vars so each test starts from defaults."""
    for k in ("PSK_DELIVERY_PIPELINES", "PSK_DELIVERY_MODE"):
        os.environ.pop(k, None)


class DefaultsTests(unittest.TestCase):

    def test_default_when_both_unset(self):
        """Default is ``server-merge`` — matches the historical
        ``PSK_DELIVERY_MODE=server`` default so existing deployments
        keep working without a config change."""
        with mock.patch.dict(os.environ, {}, clear=False):
            _clean_env()
            self.assertEqual(
                _resolve_delivery_pipelines(), ("server-merge",),
            )


class NewEnvTests(unittest.TestCase):
    """``PSK_DELIVERY_PIPELINES`` takes precedence over ``PSK_DELIVERY_MODE``
    and accepts any combination of the three pipeline names."""

    def setUp(self):
        _clean_env()

    def test_single_pipeline(self):
        for pipe in _VALID_DELIVERY_PIPELINES:
            with mock.patch.dict(
                os.environ, {"PSK_DELIVERY_PIPELINES": pipe},
            ):
                self.assertEqual(
                    _resolve_delivery_pipelines(), (pipe,),
                )

    def test_multiple_pipelines_canonical_order(self):
        """Result is always in canonical order regardless of input
        order — so log lines and downstream code see consistent
        tuples."""
        with mock.patch.dict(
            os.environ,
            {"PSK_DELIVERY_PIPELINES": "server-raw,direct"},
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(),
                ("direct", "server-raw"),
            )

    def test_whitespace_and_case_tolerant(self):
        with mock.patch.dict(
            os.environ,
            {"PSK_DELIVERY_PIPELINES": " DIRECT , Server-Merge "},
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(),
                ("direct", "server-merge"),
            )

    def test_duplicates_collapsed(self):
        with mock.patch.dict(
            os.environ,
            {"PSK_DELIVERY_PIPELINES": "direct,direct,direct"},
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(), ("direct",),
            )

    def test_unknown_dropped_known_honoured(self):
        """Unknown tokens are dropped with a warning; the rest of the
        list is honoured — typing one bad token shouldn't take down
        the whole delivery setup."""
        with mock.patch.dict(
            os.environ,
            {"PSK_DELIVERY_PIPELINES": "direct,foo,server-merge"},
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(),
                ("direct", "server-merge"),
            )

    def test_all_unknown_falls_through_to_default(self):
        """If every token in PIPELINES is bogus, we fall through to
        legacy MODE or the default — same as if PIPELINES were unset."""
        with mock.patch.dict(
            os.environ, {"PSK_DELIVERY_PIPELINES": "foo,bar"},
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(), ("server-merge",),
            )


class LegacyEnvTranslationTests(unittest.TestCase):
    """``PSK_DELIVERY_MODE`` is the legacy single-value env var.  It
    still works — translation table at the top of recorder.py."""

    def setUp(self):
        _clean_env()

    def test_legacy_server_translates(self):
        with mock.patch.dict(
            os.environ, {"PSK_DELIVERY_MODE": "server"},
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(), ("server-merge",),
            )

    def test_legacy_direct_translates(self):
        with mock.patch.dict(
            os.environ, {"PSK_DELIVERY_MODE": "direct"},
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(), ("direct",),
            )

    def test_legacy_both_translates(self):
        """``both`` → ``direct,server-raw`` so direct runs and the
        server stores raw without re-posting to PSKReporter."""
        with mock.patch.dict(
            os.environ, {"PSK_DELIVERY_MODE": "both"},
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(),
                ("direct", "server-raw"),
            )

    def test_legacy_unknown_falls_to_default(self):
        with mock.patch.dict(
            os.environ, {"PSK_DELIVERY_MODE": "neither"},
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(), ("server-merge",),
            )

    def test_new_env_overrides_legacy(self):
        """When both env vars are set, ``PSK_DELIVERY_PIPELINES``
        wins — operators migrating should set the new var and the old
        one becomes irrelevant."""
        with mock.patch.dict(
            os.environ,
            {
                "PSK_DELIVERY_PIPELINES": "direct",
                "PSK_DELIVERY_MODE": "server",   # would translate to server-merge
            },
        ):
            self.assertEqual(
                _resolve_delivery_pipelines(), ("direct",),
            )


class LegacyMappingShapeTests(unittest.TestCase):
    """The legacy → new translation table itself — pin in case a
    future refactor accidentally drops one of the historical modes."""

    def test_all_legacy_modes_have_a_mapping(self):
        self.assertEqual(
            set(_LEGACY_MODE_TO_PIPELINES),
            {"server", "direct", "both"},
        )

    def test_all_translated_targets_are_valid(self):
        for src, targets in _LEGACY_MODE_TO_PIPELINES.items():
            for t in targets:
                self.assertIn(
                    t, _VALID_DELIVERY_PIPELINES,
                    f"legacy mode {src!r} translates to unknown {t!r}",
                )


if __name__ == "__main__":
    unittest.main()
