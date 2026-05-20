"""HsPskReporterUploader shim — lifecycle + Pipeline construction.

These are integration-level tests against the real hs-uploader Pipeline
+ Uploader (no mocking of those).  Verifies:

* shim builds a Pipeline whose SqliteSource carries the multi-instance
  extra_where filter (radiod_id / tx_call / mode)
* missing callsign / grid is a clean no-op (matches legacy)
* hs-uploader missing is a clean no-op (matches legacy ImportError path)
* source selection: SqliteSource when a sink is configured, the
  per-slot FileTreeSource otherwise
* lifecycle: start spawns thread, stop joins, is_active reflects state
"""

from __future__ import annotations

import sqlite3

import pytest

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


def _make_sink(path):
    """Create an empty `pending_uploads` queue table — the shape
    `sigmond.hamsci_ch.SqliteWriter` materialises on its first flush."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pending_uploads ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "target_db TEXT, target_table TEXT, "
        "schema_version INTEGER, payload_json TEXT, queued_at TEXT)"
    )
    conn.commit()
    conn.close()


def _disable_default_sqlite_sink(monkeypatch):
    """Force SqliteSource.from_env to return a no-op source.

    Without this, a host that has `/var/lib/sigmond/sink.db` present
    would silently pick SqliteSource over the intended file fallback.
    """
    import hs_uploader.sources.sqlite as sqlite_mod
    monkeypatch.setattr(
        sqlite_mod._ConnectionConfig, "from_env",
        classmethod(lambda cls, env=None: None),
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


def test_sqlite_pipeline_filters_and_columns(monkeypatch, tmp_path):
    """The shim wires the schema-version + tx_call / mode filters into
    the SqliteSource, projects every column the dedup picker + per-rx
    counter need (Phase D Cut 1: ``rx_source`` is included), and
    anchors the watermark at ``now``.

    Phase D Cut 1 also DROPPED the ``radiod_id`` filter — a single-
    process / multi-source psk-recorder (Phase B) feeds spots from
    every receiver through one uploader, so scoping to one radiod_id
    would silently drop the others on the floor.
    """
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    sink = tmp_path / "sink.db"
    _make_sink(sink)
    monkeypatch.setenv("SIGMOND_SQLITE_PATH", str(sink))

    u = HsPskReporterUploader(
        callsign="AC0G/B1", grid_square="EM38ww",
        antenna="N6GN-SAS2", radiod_id="my-rx888",
    )
    src = u._build_source()  # noqa: SLF001
    assert type(src).__name__ == "SqliteSource"
    # tx_call / mode filters still anchored, but radiod_id filter is gone.
    assert ("tx_call", "!=", "") in src.extra_where
    assert ("mode", "IN", ["ft8", "ft4"]) in src.extra_where
    assert not any(c == "radiod_id" for (c, _, _) in src.extra_where), (
        "radiod_id filter must NOT scope the SqliteSource — multi-rx "
        "deployments need every receiver's spots through one uploader"
    )
    # rx_source projection is Phase D Cut 1's per-rx visibility +
    # Cut 2's dedup input.
    assert "rx_source" in src.select_columns
    assert src.accepted_schema_versions == [2]
    # Fresh-watermark anchor: start at now, not epoch — no historical
    # re-ship (see the start_at note in the shim).
    assert src.start_at == "now"


def test_lifecycle_thread_starts_and_stops(monkeypatch, tmp_path):
    """The thread comes up after start() with a file-fallback source
    available (no sink + spool_dir present) and exits before stop()
    returns.  Verifies thread management doesn't deadlock."""
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SIGMOND_SQLITE_PATH", raising=False)
    _disable_default_sqlite_sink(monkeypatch)

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
    """No SQLite sink and no spool_dir → start is a clean no-op.

    Without a source the pump has nothing to do; spawning a thread
    just to spin would burn cycles, so start() returns early and
    is_active stays False.
    """
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
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
        "no source available" in r.message for r in caplog.records
    )


def test_file_fallback_picks_up_spool_files(monkeypatch, tmp_path):
    """With no sink and a spool_dir containing per-slot files, the shim
    builds a FileTreeSource that finds them and fans them out into
    one Record per parsed line."""
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
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


def test_sqlite_source_picked_when_sigmond_sqlite_path_set(monkeypatch, tmp_path):
    """`SIGMOND_SQLITE_PATH` set → shim picks SqliteSource.

    Mirrors the standard sigmond host shape: the producer's
    hamsci_ch.SqliteWriter has written rows to the queue table, and
    the shim drains them via SqliteSource → PskReporterTcp.
    """
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    sink = tmp_path / "sink.db"
    # Materialise the queue table so SqliteSource doesn't go to
    # HEALTH_UNREACHABLE on the first poll — same shape the writer
    # creates on its first flush.
    _make_sink(sink)
    monkeypatch.setenv("SIGMOND_SQLITE_PATH", str(sink))

    u = HsPskReporterUploader(
        callsign="AC0G", grid_square="EM38ww", radiod_id="rid1",
        spool_dir=tmp_path / "unused-spool",
    )
    src = u._build_source()  # noqa: SLF001
    assert type(src).__name__ == "SqliteSource"
    assert src.source_id() == "sqlite:psk.spots"


# ── Phase D Cut 1: per-rx ship tally + _short_rx helper ────────────────────

def test_short_rx_renders_canonical_form():
    """``radiod:bee1-status.local`` should render as ``bee1`` for the
    log line tag; this keeps the multi-rx pump log readable.  The
    helper must be exact about the prefix / suffix it strips so a
    custom source key (e.g. ``radiod:my-rx888.local``) renders sanely
    too."""
    from psk_recorder.core.hs_uploader_shim import _short_rx
    assert _short_rx("radiod:bee1-status.local") == "bee1"
    assert _short_rx("radiod:B4-100-rx888mk2-status.local") == "B4-100-rx888mk2"
    assert _short_rx("radiod:custom.local") == "custom"
    assert _short_rx("") == "?"
    assert _short_rx("?") == "?"
    # Unfamiliar shape — passed through so we don't silently lose info.
    assert _short_rx("kiwi:bee1.local") == "kiwi:bee1"


def test_per_rx_tally_in_on_batch_outcome(monkeypatch, tmp_path):
    """The on_batch_outcome callback must tally shipped spots per
    ``rx_source`` so the per-pump log line can break down which
    receivers contributed in multi-source mode.  Mirrors how
    ``smd watch wspr`` surfaces per-rx contribution."""
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    u = HsPskReporterUploader(
        callsign="AC0G", grid_square="EM38ww", radiod_id="local",
    )
    # Stub records: hs-uploader's Record has .columns mapping field
    # names to values.  We only need .columns for the tally code.
    class _R:
        def __init__(self, mode, rx):
            self.columns = {"mode": mode, "rx_source": rx}

    class _Batch:
        records = [
            _R("ft8", "radiod:bee1-status.local"),
            _R("ft8", "radiod:bee1-status.local"),
            _R("ft4", "radiod:bee2-status.local"),
            _R("ft8", "radiod:B4-100-rx888mk2-status.local"),
            _R("ft8", ""),  # bare row — falls into "?" bucket
        ]

    class _Outcome:
        kind = "acked"

    u._on_batch_outcome(None, _Batch(), _Outcome())

    # Mode totals
    assert u._pump_ft8 == 4
    assert u._pump_ft4 == 1
    # Per-rx tally — each receiver shows up with its FT8/FT4 split
    assert u._pump_by_rx["radiod:bee1-status.local"] == {"ft8": 2, "ft4": 0}
    assert u._pump_by_rx["radiod:bee2-status.local"] == {"ft8": 0, "ft4": 1}
    assert u._pump_by_rx["radiod:B4-100-rx888mk2-status.local"] == {
        "ft8": 1, "ft4": 0,
    }
    # Empty rx_source bucketed under "?" so it's not silently lost.
    assert u._pump_by_rx["?"] == {"ft8": 1, "ft4": 0}


def test_per_rx_tally_skips_non_acked_outcomes(monkeypatch, tmp_path):
    """``retry_later`` / ``permanent`` outcomes mean the records did
    NOT reach PSKReporter — the per-rx tally must stay zero so the
    operator's log isn't lying about delivery."""
    monkeypatch.setenv("HS_UPLOADER_STATE_DIR", str(tmp_path))
    u = HsPskReporterUploader(
        callsign="AC0G", grid_square="EM38ww", radiod_id="local",
    )

    class _R:
        columns = {"mode": "ft8", "rx_source": "radiod:bee1-status.local"}

    class _Batch:
        records = [_R(), _R()]

    class _Outcome:
        kind = "retry_later"

    u._on_batch_outcome(None, _Batch(), _Outcome())
    assert u._pump_ft8 == 0
    assert u._pump_by_rx == {}
