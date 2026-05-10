"""hs-uploader-driven PSK Reporter spot uploader.

Drop-in replacement for ``PskReporterUploader`` (the legacy CH-poll +
``pskreporter`` library shim) that uses the hs-uploader library's
``Pipeline`` + ``Uploader`` orchestrator and the ``PskReporterTcp``
transport (which owns the TCP socket end-to-end — no third-party
library involvement).

Behind the ``PSK_USE_HS_UPLOADER`` env var feature flag for now;
becomes the default once Phase 5c.3 (file-tree fallback) lands and
the legacy path can be retired.

The lifecycle interface (``start`` / ``stop`` / ``is_active``) matches
the legacy class so ``recorder.PskRecorder._start_uploaders`` can pick
between the two without conditional plumbing downstream of construction.

Differences from the legacy path:

* CH query uses ``cursor_column="ingested_at"`` (commit 93f4d5b's
  fix carried over) and ``extra_where`` filters scope to this
  daemon's ``radiod_id`` so multi-instance is safe by construction.
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
from typing import Optional

logger = logging.getLogger(__name__)


# How often we ask the orchestrator to pump.  Each pump pulls one
# batch from CH and ships it via the transport, so this IS the upload
# cadence.  30 s aligns with the FT4/FT8 slot rate; same value as
# the legacy PSKREPORTER_INTERVAL.
PUMP_INTERVAL_SEC = 30.0


class HsPskReporterUploader:
    """Pump psk.spots → PSK Reporter via hs-uploader's Pipeline."""

    def __init__(
        self,
        callsign: str,
        grid_square: str,
        antenna: str = "",
        radiod_id: str = "",
        use_tcp: bool = True,
    ) -> None:
        self._callsign = callsign
        self._grid_square = grid_square
        self._antenna = antenna
        self._radiod_id = radiod_id
        self._use_tcp = use_tcp
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._uploader = None
        self._transport = None
        self._pump_count = 0
        self._work_count = 0

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
            from hs_uploader.sources.clickhouse import ClickHouseSource
            from hs_uploader.transports.pskreporter import PskReporterTcp
            from hs_uploader.watermark.sqlite import (
                SqliteWatermarkStore, default_path,
            )
        except ImportError as exc:
            logger.warning("psk-uploader-hs disabled: %s", exc)
            return

        try:
            src = ClickHouseSource.from_env(
                database="psk", table="spots",
                accepted_schema_versions=[2],
                primary_key_columns=[
                    # psk.spots ORDER BY tuple — tiebreak hash takes
                    # this set so two rows with identical (host_call,
                    # mode, frequency, time, message) will tie on
                    # cityHash64 and ship in a stable order.
                    "host_call", "mode", "frequency", "time", "message",
                ],
                select_columns=[
                    "time", "frequency", "mode", "snr_db", "tx_call",
                    "grid", "score", "message",
                ],
                cursor_column="ingested_at",
                extra_where=[
                    ("radiod_id", "=", self._radiod_id),
                    ("tx_call", "!=", ""),
                    ("mode", "IN", ["ft8", "ft4"]),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "psk-uploader-hs: ClickHouseSource construct failed: %s", exc,
            )
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
        self._uploader = Uploader([pipeline])

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
                if self._uploader is not None and self._uploader.pump():
                    self._work_count += 1
            except Exception:
                logger.exception(
                    "psk-uploader-hs: unhandled error in pump loop",
                )
