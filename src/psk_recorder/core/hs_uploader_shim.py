"""hs-uploader-driven PSK Reporter spot uploader.

Drop-in replacement for ``PskReporterUploader`` (the legacy CH-poll +
``pskreporter`` library shim) that uses the hs-uploader library's
``Pipeline`` + ``Uploader`` orchestrator and the ``PskReporterTcp``
transport (which owns the TCP socket end-to-end — no third-party
library involvement).

Behind the ``PSK_USE_HS_UPLOADER`` env var feature flag for now;
becomes the default once the legacy path can be retired.

The lifecycle interface (``start`` / ``stop`` / ``is_active``) matches
the legacy class so ``recorder.PskRecorder._start_uploaders`` can pick
between the two without conditional plumbing downstream of construction.

Source selection:

* ``SqliteSource`` — reads sigmond's local SQLite sink
  (``/var/lib/sigmond/sink.db`` by default, or ``SIGMOND_SQLITE_PATH``),
  the queue ``sigmond.hamsci_ch.SqliteWriter`` fills.  ``extra_where``
  filters scope the queue to this daemon's ``radiod_id`` so multi-
  instance is safe by construction.
* No SQLite sink → fall back to ``FileTreeSource`` reading per-slot
  ``.spots.txt`` files the ``SlotWorker`` writes when
  ``PSK_USE_HS_UPLOADER=1`` and no sink is present.  Files are deleted
  on PSKReporter ack (delete_on_ack retention).

Other notes:

* ``PskReporterTcp`` owns the socket — no ``pskreporter`` Python
  library dependency, no PSKREPORTER_INTERVAL/NO_DEDUP env-var
  knobs.  Cadence is the pump interval here.
* Watermark + retry state persist to ``/var/lib/hs-uploader/
  watermarks.db`` (sqlite); restarts pick up where they left off
  rather than re-deriving from "now".
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# How often we ask the orchestrator to pump.  Each pump pulls one
# batch from the source and ships it via the transport, so this IS the
# upload cadence.  30 s aligns with the FT4/FT8 slot rate; same value
# as the legacy PSKREPORTER_INTERVAL.
PUMP_INTERVAL_SEC = 30.0


def _short_rx(rx_source: str) -> str:
    """Render the multi-rx source key compactly for log lines.

    ``radiod:bee1-status.local`` → ``bee1``; falls back to the raw
    string for any unexpected form so we never silently lose info.
    Mirrors the rendering ``smd watch wspr`` uses on its per-cycle
    output for the same kind of tag.
    """
    base = rx_source or "?"
    for prefix in ("radiod:",):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    for suffix in ("-status.local", ".local"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base or "?"


class HsPskReporterUploader:
    """Pump psk.spots → PSK Reporter via hs-uploader's Pipeline."""

    def __init__(
        self,
        callsign: str,
        grid_square: str,
        antenna: str = "",
        radiod_id: str = "",
        use_tcp: bool = True,
        spool_dir: Optional[Path] = None,
    ) -> None:
        self._callsign = callsign
        self._grid_square = grid_square
        self._antenna = antenna
        self._radiod_id = radiod_id
        self._use_tcp = use_tcp
        # spool_dir is the per-radiod spool root (`<spool>/<radiod_id>`)
        # — the directory containing ft8/ and ft4/ subdirs that the slot
        # writes per-slot ``.spots.txt`` files into when no SQLite sink
        # is present.  When None, file fallback isn't available.
        self._spool_dir = Path(spool_dir) if spool_dir is not None else None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._uploader = None
        self._transport = None
        self._pump_count = 0
        self._work_count = 0
        # Cumulative per-mode spot counts shipped to PSK Reporter,
        # accumulated from the hs-uploader on_batch_outcome callback
        # (see _on_batch_outcome below).  Works uniformly across
        # SqliteSource / FileTreeSource — replaces the spool-dir delta
        # logic that only worked for the file path.
        self._uploaded_ft8 = 0
        self._uploaded_ft4 = 0
        # Per-pump tallies; reset to 0 at the top of each pump cycle
        # so the journal log line reports just this cycle's deltas.
        self._pump_ft8 = 0
        self._pump_ft4 = 0
        # Phase D Cut 1: per-rx_source per-pump ship counts, populated
        # from the on_batch_outcome callback so the per-pump log line
        # can break "shipped ft8=N ft4=M" down by which receiver each
        # spot came from — mirrors how ``smd watch wspr`` surfaces
        # per-rx contribution.  Keyed by the row's ``rx_source`` field
        # (e.g. ``radiod:bee1-status.local``); blank-source rows fall
        # into a ``"?"`` bucket so they don't get silently dropped.
        self._pump_by_rx: dict[str, dict[str, int]] = {}

    # ----- lifecycle -----

    def start(self) -> None:
        if not self._callsign or not self._grid_square:
            logger.warning(
                "psk-uploader-hs: callsign / grid not configured; skipping",
            )
            return
        if not self._use_tcp:
            # PskReporterTcp transport is TCP-only; UDP isn't a knob it
            # exposes (per the design plan's "TCP-default per spec").
            # Keep going on TCP — log so operators notice the override.
            logger.info(
                "psk-uploader-hs: pskreporter_tcp=False is ignored; "
                "PskReporterTcp transport is TCP-only",
            )

        try:
            from hs_uploader import Pipeline, RetryPolicy, StationIdentity, Uploader
            from hs_uploader.transports.pskreporter import PskReporterTcp
            from hs_uploader.watermark.sqlite import (
                SqliteWatermarkStore, default_path,
            )
        except ImportError as exc:
            logger.warning("psk-uploader-hs disabled: %s", exc)
            return

        src = self._build_source()
        if src is None:
            return

        try:
            self._transport = PskReporterTcp(
                decoding_software=f"psk-recorder/0.1 (radiod={self._radiod_id})",
                antenna=self._antenna,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "psk-uploader-hs: PskReporterTcp construct failed: %s", exc,
            )
            return

        watermark = SqliteWatermarkStore(default_path())
        identity = StationIdentity(
            call=self._callsign,
            grid=self._grid_square,
            radiod_id=self._radiod_id,
        )
        pipeline = Pipeline(
            name=f"psk-recorder-{self._radiod_id}",
            source=src,
            transport=self._transport,
            watermark=watermark,
            identity=identity,
            retry=RetryPolicy.exponential(base=2.0, cap_sec=300.0),
            batch_limit=500,
        )
        # `on_batch_outcome` counts shipped records per-mode directly
        # from `batch.records`.  Works uniformly across SqliteSource /
        # file sources — the previous spool-dir delta only counted
        # under FileTreeSource and read 0 under SqliteSource (see the
        # comment at _count_spool_spots() below for the historical
        # constraint that's now lifted).
        self._uploader = Uploader(
            [pipeline],
            on_batch_outcome=self._on_batch_outcome,
        )

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="psk-uploader-hs",
        )
        self._thread.start()
        logger.info(
            "psk-uploader-hs started: %s/%s (radiod_id=%s, pump=%ds)",
            self._callsign, self._grid_square, self._radiod_id,
            int(PUMP_INTERVAL_SEC),
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        if self._pump_count:
            logger.info(
                "psk-uploader-hs stopped after %d pump(s), %d with work",
                self._pump_count, self._work_count,
            )

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ----- pump loop -----

    def _run(self) -> None:
        while not self._stop.wait(PUMP_INTERVAL_SEC):
            try:
                self._pump_count += 1
                # Reset per-pump per-mode tallies; the on_batch_outcome
                # callback fires inside pump() and fills these in.
                # Works across SqliteSource / file sources uniformly.
                self._pump_ft8 = 0
                self._pump_ft4 = 0
                self._pump_by_rx = {}

                if self._uploader is not None and self._uploader.pump():
                    self._work_count += 1
                    self._uploaded_ft8 += self._pump_ft8
                    self._uploaded_ft4 += self._pump_ft4
                    # Render per-rx breakdown when more than one source
                    # contributed this pump — keeps single-rx logs
                    # unchanged, surfaces multi-rx visibility when it
                    # matters.  Operators reading ``smd watch psk`` see
                    # which receivers are actually contributing spots.
                    rx_part = ""
                    if len(self._pump_by_rx) > 1:
                        rx_part = " by_rx=[" + " ".join(
                            f"{_short_rx(rx)}:{counts.get('ft8', 0)}/{counts.get('ft4', 0)}"
                            for rx, counts in sorted(self._pump_by_rx.items())
                        ) + "]"
                    logger.info(
                        "psk-uploader-hs: shipped ft8=%d ft4=%d "
                        "(total ft8=%d ft4=%d, work=%d)%s",
                        self._pump_ft8, self._pump_ft4,
                        self._uploaded_ft8, self._uploaded_ft4,
                        self._work_count, rx_part,
                    )
            except Exception:
                logger.exception(
                    "psk-uploader-hs: unhandled error in pump loop",
                )

    def _on_batch_outcome(self, pipeline, batch, outcome) -> None:
        """hs-uploader callback: tally per-mode records as they ship.

        Only count on ``acked`` / ``partial_ack`` — ``retry_later`` and
        ``permanent`` mean the records didn't reach PSKReporter on
        this attempt.  partial_ack is approximate (the record list
        includes the records the transport tried; rejects within the
        batch still get counted), but PskReporterTcp's normal flow is
        all-or-nothing.
        """
        if outcome.kind not in ("acked", "partial_ack"):
            return
        for r in batch.records:
            cols = r.columns or {}
            mode = str(cols.get("mode", "")).lower()
            if mode == "ft8":
                self._pump_ft8 += 1
            elif mode == "ft4":
                self._pump_ft4 += 1
            # Phase D Cut 1: per-rx tally so the pump-log line can
            # surface per-receiver contribution in multi-source mode.
            rx = str(cols.get("rx_source", "")) or "?"
            by = self._pump_by_rx.setdefault(rx, {"ft8": 0, "ft4": 0})
            if mode in ("ft8", "ft4"):
                by[mode] = by.get(mode, 0) + 1

    # ----- source selection -----

    def _build_source(self):
        """Pick the source based on env, in priority order:

        1. ``SIGMOND_SQLITE_PATH`` set, OR the default sink
           ``/var/lib/sigmond/sink.db`` exists → ``SqliteSource``
           (producer-side is ``sigmond.hamsci_ch.SqliteWriter``).
        2. Else fall through to the per-slot ``FileTreeSource`` — the
           SlotWorker populates that spool when no local SQLite sink
           is configured.

        `database`, `table`, `select_columns`, and `extra_where` scope
        the SQLite queue to this daemon (``radiod_id`` filter) and
        project the columns ``PskReporterTcp`` needs.
        """
        sqlite_kwargs = dict(
            database="psk",
            table="spots",
            accepted_schema_versions=[2],
            select_columns=[
                "time", "frequency", "mode", "snr_db", "tx_call",
                "grid", "score", "message",
                # Phase D Cut 1: surface the per-spot receiver tag so
                # the on_batch_outcome callback can count ships per
                # rx_source (operator visibility — "bee1 contributed
                # 40 FT8, bee2 contributed 35, local contributed 38")
                # and so Cut 2's cross-rx dedup picker has the input
                # it needs without re-reading the row from sqlite.
                "rx_source",
            ],
            extra_where=[
                # Phase D Cut 1: dropped the radiod_id filter.  In
                # single-process / multi-source mode (Phase B), one
                # uploader pumps spots from EVERY receiver — filtering
                # to a single radiod_id would silently drop bee1 / bee2
                # spots on the floor.  The legacy single-source case
                # is unaffected: the queue only contains one radiod's
                # rows anyway.
                ("tx_call", "!=", ""),
                ("mode", "IN", ["ft8", "ft4"]),
            ],
            # First-pump-after-fresh-watermark anchor: start at
            # wallclock-now, not epoch.  Without this, an empty
            # watermark.db (first deploy, lost state, switched
            # uploaders) re-ships every historical row, which the
            # legacy uploader has likely already shipped —
            # PSKReporter shows duplicates.
            start_at="now",
            # Don't delete on ack — the same psk.spots queue is also
            # consumed by the forthcoming wsprdaemon-tar transport (PR
            # 5) for the via-server delivery path, and by future
            # query-style consumers like Andrew Roland's scrapers.
            # Cleanup is centralized in `smd storage trim --all`
            # (PSK_RETENTION_MIN, default 60 min, 30-min floor).
            delete_on_commit=False,
        )

        # SQLite path — sigmond.hamsci_ch.SqliteWriter is the producer
        # half; this shim is the consumer.  Try construction
        # unconditionally; SqliteSource.from_env returns a no-op source
        # when neither SIGMOND_SQLITE_PATH nor the default file exists.
        try:
            from hs_uploader.sources.sqlite import SqliteSource, HEALTH_NOOP
            sqlite_src = SqliteSource.from_env(**sqlite_kwargs)
            if sqlite_src.health() != HEALTH_NOOP:
                logger.info(
                    "psk-uploader-hs: using SqliteSource (sink at %s)",
                    sqlite_src._config.path if sqlite_src._config else "?",
                )
                return sqlite_src
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "psk-uploader-hs: SqliteSource construct failed: %s",
                exc,
            )

        if self._spool_dir is None:
            logger.warning(
                "psk-uploader-hs: no SQLite sink and no spool_dir "
                "— no source available",
            )
            return None

        try:
            from hs_uploader.sources.files import FileSpec, FileTreeSource
        except ImportError as exc:
            logger.warning(
                "psk-uploader-hs: FileTreeSource import failed: %s", exc,
            )
            return None

        return FileTreeSource(
            root=self._spool_dir,
            specs=[
                FileSpec(
                    pattern="*.spots.txt",
                    parser=_parse_spots_file,
                    table="psk.spots",
                ),
            ],
            retention=FileTreeSource.DELETE_ON_ACK,
            source_id=f"psk-spool:{self._radiod_id}",
        )


# ----- spool file parser -----


def _parse_spots_file(path, raw):
    """Parse a per-slot ``.spots.txt`` file into per-spot dicts.

    Each line is a decode_ft8 stdout line (WSJT-X-style timestamp +
    spot fields).  Mode is inferred from the file's parent directory
    (``ft8/`` or ``ft4/``); ``parse_decoder_line`` does the rest.

    Returns a list of dicts shaped to match what
    ``PskReporterTcp._record_to_spot`` expects: ``time`` (datetime,
    promoted to ``Record.time`` by FileTreeSource), ``tx_call``,
    ``frequency``, ``snr_db`` / ``score``, ``mode``, ``grid``,
    ``message``.  Lines that don't parse are skipped silently.
    """
    from psk_recorder.core.ch_tailer import parse_decoder_line

    mode = path.parent.name.lower()
    if mode not in ("ft8", "ft4"):
        mode = None

    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []

    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parsed = parse_decoder_line(line, mode=mode)
        if parsed is None:
            continue
        rows.append(parsed)
    return rows
