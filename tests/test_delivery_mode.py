"""Tests for PSK_DELIVERY_MODE resolution in recorder.py (Phase 2 PR 3)."""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

# Stub heavy deps that recorder.py pulls in transitively. The resolver
# itself only uses `os` and `logging`, so we don't need real numpy or the
# DSP modules just to test it.
for _name in ("numpy",):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

from psk_recorder.core.recorder import _resolve_delivery_mode  # noqa: E402


class TestResolveDeliveryMode(unittest.TestCase):

    def test_default_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PSK_DELIVERY_MODE", None)
            self.assertEqual(_resolve_delivery_mode(), "server")

    def test_explicit_server(self):
        with mock.patch.dict(os.environ, {"PSK_DELIVERY_MODE": "server"}):
            self.assertEqual(_resolve_delivery_mode(), "server")

    def test_explicit_direct(self):
        with mock.patch.dict(os.environ, {"PSK_DELIVERY_MODE": "direct"}):
            self.assertEqual(_resolve_delivery_mode(), "direct")

    def test_explicit_both(self):
        with mock.patch.dict(os.environ, {"PSK_DELIVERY_MODE": "both"}):
            self.assertEqual(_resolve_delivery_mode(), "both")

    def test_case_insensitive(self):
        for raw in ("Direct", "DIRECT", " direct ", "BOTH"):
            with mock.patch.dict(os.environ, {"PSK_DELIVERY_MODE": raw}):
                self.assertEqual(
                    _resolve_delivery_mode(), raw.strip().lower(),
                )

    def test_unknown_falls_back_to_server(self):
        # Anything bogus -> server (the safe default — no double delivery).
        with mock.patch.dict(os.environ, {"PSK_DELIVERY_MODE": "neither"}):
            self.assertEqual(_resolve_delivery_mode(), "server")
        with mock.patch.dict(os.environ, {"PSK_DELIVERY_MODE": ""}):
            self.assertEqual(_resolve_delivery_mode(), "server")


if __name__ == "__main__":
    unittest.main()
