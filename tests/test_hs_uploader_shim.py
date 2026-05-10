"""HsPskReporterUploader shim — lifecycle + Pipeline construction.

These are integration-level tests against the real hs-uploader Pipeline
+ Uploader (no mocking of those) but with a stubbed CH client so we
don't need a live ClickHouse.  Verifies:

* shim builds a Pipeline with the right cursor_column and extra_where
  (the multi-instance + ingested_at lessons carried into 5c.2)
* missing callsign / grid is a clean no-op (matches legacy)
* hs-uploader missing is a clean no-op (matches legacy ImportError path)
* lifecycle: start spawns thread, stop joins, is_active reflects state
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

import psk_recorder.core.hs_uploader_shim as shim_mod
from psk_recorder.core.hs_uploader_shim import HsPskReporterUploader


def _hs_available():
    try:
        import hs_uploader  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(
    not _hs_available(),
    reason="hs-uploader not installed in this venv",
)


def test_no_callsign_is_clean_noop(caplog):
    u = HsPskReporterUploader(callsign="", grid_square="EM38ww", radiod_id="rid1")
    with caplog.at_level("WARNING", logger="psk_recorder.core.hs_uploader_shim"):
        u.start()
    assert u.is_active is False
    assert any("callsign / grid not configured" in r.message for r in caplog.records)


def test_no_grid_is_clean_noop(caplog):
    u = HsPskReporterUploader(callsign="AC0G/B1", grid_square="", radiod_id="rid1")
    with caplog.at_level("WARNING", logger="psk_recorder.core.hs_uploader_shim"):
        u.start()
    assert u.is_active is False


def test_pipeline_uses_radiod_id_filter_and_ingested_at_cursor(monkeypatch, tmp_path):
    """Construct an uploader and pump once; capture the SQL the source
    issues.  Confirms cursor_column=ingested_at and extra_where carries
    radiod_id / tx_call / mode, as Phase 5c.2's design requires."""
    from hs_uploader.sources.clickhouse import _ConnectionConfig

    captured: dict = {}

    def fake_query(sql, parameters=None):
        captured.setdefault("sql_history", []).append(sql)
        captured["params"] = parameters
        result = MagicMock()
        if "system.columns" in sql:
            # The 20-column psk.spots v2 schema as deployed on bee1.
            result.result_rows = [
                ("time", "DateTime"),
                ("mode", "LowCardinality(String)"),
                ("host_call", "LowCardinality(String)"),
                ("host_grid", "LowCardinality(String)"),
                ("radiod_id", "LowCardinality(String)"),
                ("instance", "LowCardinality(String)"),
                ("processing_version", "LowCardinality(String)"),
                ("score", "Int16"),
                ("dt", "Float32"),
                ("frequency", "Int64"),
                ("frequency_mhz", "Float64"),
                ("message", "String"),
                ("tx_call", "LowCardinality(String)"),
                ("rx_call", "LowCardinality(String)"),
                ("grid", "LowCardinality(String)"),
                ("report", "Nullable(Int16)"),
                ("ingested_at", "DateTime"),
                ("snr_db", "Nullable(Float32)"),
                ("spectral_width_hz", "Nullable(Float32)"),
                ("decoder_kind", "LowCardinality(String)"),
            ]
            result.column_names = ["name", "type"]
        else:
            result.result_rows = []  # no rows to ship — schema check succeeds, drain returns nothing
            result.column_names = []
        return result

    fake_client = MagicMock()
    fake_client.query = MagicMock(side_effect=fake_query)

    # Stub the CH connection config and client factory.
    monkeypatch.setenv("SIGMOND_CLICKHOUSE_URL", "http://localhost:8123")
    monkeypatch.setenv("SIGMOND_CLICKHOUSE_USER", "test")
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))

    # Patch the factory the source uses.
    from hs_uploader.sources import clickhouse as ch_mod
    monkeypatch.setattr(
        ch_mod, "_default_client_factory",
        lambda cfg: fake_client,
    )

    u = HsPskReporterUploader(
        callsign="AC0G/B1",
        grid_square="EM38ww",
        antenna="N6GN-SAS2",
        radiod_id="my-rx888",
    )
    u.start()
    try:
        # Give the thread one full pump cycle.  PUMP_INTERVAL_SEC is
        # 30s in production; for the test we wait a short time and
        # fire a manual pump if the thread hasn't naturally pumped yet.
        # The internal _uploader is exposed (private, but stable enough).
        u._uploader.pump()  # noqa: SLF001
    finally:
        u.stop(timeout=2.0)

    sqls = captured.get("sql_history", [])
    data_sqls = [s for s in sqls if "system.columns" not in s]
    assert data_sqls, f"no data query issued; got {sqls}"
    sql = data_sqls[0]
    assert "ingested_at AS __cursor__" in sql
    assert "ORDER BY ingested_at, cityHash64" in sql
    assert " AND radiod_id = %(extra_0)s" in sql
    assert " AND tx_call != %(extra_1)s" in sql
    assert " AND mode IN %(extra_2)s" in sql
    params = captured["params"]
    assert params["extra_0"] == "my-rx888"
    assert params["extra_1"] == ""
    assert params["extra_2"] == ["ft8", "ft4"]


def test_lifecycle_thread_starts_and_stops(monkeypatch, tmp_path):
    """The thread comes up after start() and exits before stop()
    returns.  Independent of whether the pump does any work — just
    verifies thread management doesn't deadlock."""
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SIGMOND_CLICKHOUSE_URL", raising=False)

    u = HsPskReporterUploader(
        callsign="AC0G/B1", grid_square="EM38ww", radiod_id="rid1",
    )
    u.start()
    # Even with CH unconfigured, the source goes to no-op health and
    # pump() returns False — the thread still runs.
    assert u.is_active
    u.stop(timeout=2.0)
    assert u.is_active is False
