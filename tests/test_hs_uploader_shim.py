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
    """The thread comes up after start() with a file-fallback source
    available (CH unset + spool_dir present) and exits before stop()
    returns.  Verifies thread management doesn't deadlock."""
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SIGMOND_CLICKHOUSE_URL", raising=False)

    spool = tmp_path / "spool" / "rid1"
    spool.mkdir(parents=True)

    u = HsPskReporterUploader(
        callsign="AC0G/B1", grid_square="EM38ww", radiod_id="rid1",
        spool_dir=spool,
    )
    u.start()
    # FileTreeSource health is "ok" once the spool dir exists; pump()
    # returns False because there are no .spots.txt files yet.  The
    # thread still runs.
    assert u.is_active
    u.stop(timeout=2.0)
    assert u.is_active is False


def test_no_source_is_clean_noop(monkeypatch, tmp_path, caplog):
    """No CH, no SQLite, and no spool_dir → start is a clean no-op.

    Without a source the pump has nothing to do; spawning a thread
    just to spin would burn cycles, so start() returns early and
    is_active stays False.
    """
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SIGMOND_CLICKHOUSE_URL", raising=False)
    monkeypatch.delenv("SIGMOND_SQLITE_PATH", raising=False)
    _disable_default_sqlite_sink(monkeypatch)

    u = HsPskReporterUploader(
        callsign="AC0G/B1", grid_square="EM38ww", radiod_id="rid1",
        spool_dir=None,
    )
    with caplog.at_level("WARNING", logger="psk_recorder.core.hs_uploader_shim"):
        u.start()
    assert u.is_active is False
    assert any(
        "neither source available" in r.message for r in caplog.records
    )


def _disable_default_sqlite_sink(monkeypatch):
    """Force SqliteSource.from_env to return a no-op source.

    Without this, a test host that happens to have
    `/var/lib/sigmond/sink.db` present (e.g. ran a migrate dry-run)
    would silently pick SqliteSource over the file fallback.
    """
    import hs_uploader.sources.sqlite as sqlite_mod
    monkeypatch.setattr(
        sqlite_mod._ConnectionConfig, "from_env",
        classmethod(lambda cls, env=None: None),
    )


def test_file_fallback_picks_up_spool_files(monkeypatch, tmp_path):
    """With no CH and a spool_dir containing per-slot files, the shim
    builds a FileTreeSource that finds them and fans them out into
    one Record per parsed line."""
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SIGMOND_CLICKHOUSE_URL", raising=False)
    monkeypatch.delenv("SIGMOND_SQLITE_PATH", raising=False)
    _disable_default_sqlite_sink(monkeypatch)

    # Mirror what SlotWorker writes: <spool>/<radiod>/<mode>/<slot>.spots.txt
    spool = tmp_path / "spool" / "rid1"
    ft8 = spool / "ft8"
    ft8.mkdir(parents=True)
    (ft8 / "260510_171530_14074.spots.txt").write_text(
        "2026/05/10 17:15:30  28 +0.11 14,074,461.9 ~ KJ5LMM LU1ALF -10\n"
        "2026/05/10 17:15:30  25 +0.75 14,075,396.3 ~ CQ CX6TE GF26\n"
    )

    u = HsPskReporterUploader(
        callsign="AC0G", grid_square="EM38ww", radiod_id="rid1",
        spool_dir=spool,
    )
    src = u._build_source()  # noqa: SLF001
    assert src is not None
    batches = list(src.iter_batches(b"", limit=100))
    assert len(batches) == 1
    records = batches[0].records
    assert len(records) == 2
    # Mode comes from path.parent.name; tx_call from message body.
    assert records[0].columns["mode"] == "ft8"
    assert records[0].columns["tx_call"] == "LU1ALF"
    assert records[1].columns["tx_call"] == "CX6TE"
    # `time` was lifted out of cols into Record.time.
    assert "time" not in records[0].columns
    assert records[0].time.year == 2026


def test_file_fallback_skips_unparseable_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGMOND_CLICKHOUSE_URL", raising=False)
    monkeypatch.delenv("SIGMOND_SQLITE_PATH", raising=False)
    _disable_default_sqlite_sink(monkeypatch)
    spool = tmp_path / "rid1"
    ft8 = spool / "ft8"
    ft8.mkdir(parents=True)
    (ft8 / "260510_171530_14074.spots.txt").write_text(
        "header line that does not match\n"
        "\n"
        "2026/05/10 17:15:30  28 +0.11 14,074,461.9 ~ KJ5LMM LU1ALF -10\n"
        "garbage in the middle\n"
    )

    u = HsPskReporterUploader(
        callsign="AC0G", grid_square="EM38ww", radiod_id="rid1",
        spool_dir=spool,
    )
    src = u._build_source()  # noqa: SLF001
    batches = list(src.iter_batches(b"", limit=100))
    assert len(batches[0].records) == 1
    assert batches[0].records[0].columns["tx_call"] == "LU1ALF"


def test_ch_env_takes_precedence_over_spool(monkeypatch, tmp_path):
    """If both CH env and spool_dir are set, CH wins (the 5c.2 path
    is preferred — file is the fallback)."""
    monkeypatch.setenv("SIGMOND_CLICKHOUSE_URL", "http://localhost:8123")
    monkeypatch.setenv("SIGMOND_CLICKHOUSE_USER", "test")
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))

    fake_client = MagicMock()

    def fake_query(sql, parameters=None):
        result = MagicMock()
        result.result_rows = [("time", "DateTime"), ("ingested_at", "DateTime")]
        result.column_names = ["name", "type"]
        return result

    fake_client.query = MagicMock(side_effect=fake_query)
    from hs_uploader.sources import clickhouse as ch_mod
    monkeypatch.setattr(
        ch_mod, "_default_client_factory", lambda cfg: fake_client,
    )

    spool = tmp_path / "spool" / "rid1"
    spool.mkdir(parents=True)

    u = HsPskReporterUploader(
        callsign="AC0G", grid_square="EM38ww", radiod_id="rid1",
        spool_dir=spool,
    )
    src = u._build_source()  # noqa: SLF001
    # ClickHouseSource — the schema-validation query went out.
    assert type(src).__name__ == "ClickHouseSource"


def test_sqlite_source_picked_when_sigmond_sqlite_path_set(monkeypatch, tmp_path):
    """`SIGMOND_SQLITE_PATH` set + no CH → shim picks SqliteSource.

    Mirrors the post-`smd storage migrate-to-sqlite` host shape: the
    producer's hamsci_ch.SqliteWriter has written rows to the queue
    table, and the shim drains them via SqliteSource → PskReporterTcp.
    """
    import sqlite3
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SIGMOND_CLICKHOUSE_URL", raising=False)
    sink = tmp_path / "sink.db"
    # Materialise the queue table so SqliteSource doesn't go to
    # HEALTH_UNREACHABLE on the first poll — same shape the writer
    # creates on its first flush.
    conn = sqlite3.connect(sink)
    conn.execute(
        "CREATE TABLE pending_uploads ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "target_db TEXT, target_table TEXT, "
        "schema_version INTEGER, payload_json TEXT, queued_at TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SIGMOND_SQLITE_PATH", str(sink))

    u = HsPskReporterUploader(
        callsign="AC0G", grid_square="EM38ww", radiod_id="rid1",
        spool_dir=tmp_path / "unused-spool",
    )
    src = u._build_source()  # noqa: SLF001
    assert type(src).__name__ == "SqliteSource"
    assert src.source_id() == "sqlite:psk.spots"


def test_ch_takes_precedence_over_sqlite(monkeypatch, tmp_path):
    """When both CH and SQLite env are set, CH wins.

    Matches the documented priority: SIGMOND_CLICKHOUSE_URL → CH,
    SIGMOND_SQLITE_PATH → SQLite, else file.  Operators flipping from
    CH to SQLite go via `smd storage migrate-to-sqlite`, which
    neutralises SIGMOND_CLICKHOUSE_URL — so on a healthy host only
    one of the two is set.
    """
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SIGMOND_CLICKHOUSE_URL", "http://localhost:8123")
    monkeypatch.setenv("SIGMOND_CLICKHOUSE_USER", "test")
    monkeypatch.setenv("SIGMOND_SQLITE_PATH", str(tmp_path / "sink.db"))

    fake_client = MagicMock()
    fake_client.query = MagicMock(side_effect=lambda *a, **kw: _ch_columns_fixture())
    from hs_uploader.sources import clickhouse as ch_mod
    monkeypatch.setattr(
        ch_mod, "_default_client_factory", lambda cfg: fake_client,
    )

    u = HsPskReporterUploader(
        callsign="AC0G", grid_square="EM38ww", radiod_id="rid1",
        spool_dir=tmp_path / "unused-spool",
    )
    src = u._build_source()  # noqa: SLF001
    assert type(src).__name__ == "ClickHouseSource"


def _ch_columns_fixture():
    """Minimal CH client `query()` response — used by the priority test."""
    r = MagicMock()
    r.result_rows = [("time", "DateTime"), ("ingested_at", "DateTime")]
    r.column_names = ["name", "type"]
    return r
