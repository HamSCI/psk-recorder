"""PskReporterUploader: ClickHouse-backed PSK Reporter spot uploader.

Polls ``psk.spots`` (the canonical native-jt9 sink populated by
``ChTailer``) for new rows and feeds them into the ``pskreporter`` UDP
client.  One uploader per psk-recorder daemon (PSK Reporter accepts an
unlimited mix of modes from a single station, so we don't need to fan
out per-mode like the old subprocess-tail approach did).

This replaces the prior ``pskreporter-sender`` subprocess wrapper, which
parsed the per-mode log file directly with a wsprdaemon-style WSPR
ALL.TXT regex incompatible with our native jt9 format.  The new design:

  * Single source of truth: ``psk.spots`` (rows already validated, parsed,
    callsign-extracted, with absolute receive frequency).
  * Single source of transformation: ``pskreporter`` library's UDP
    batcher (controlled by ``PSKREPORTER_INTERVAL`` env, default 180 s
    upstream / 30 s in coordination.env).
  * No subprocess plumbing, no log-file race, no double-parse.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# How often to poll ClickHouse for new spots.  Keep well below the
# pskreporter library's batch interval (default 30 s in our deploy) so
# spots accumulate in the library's batch before its next UDP flush.
POLL_INTERVAL_SEC = 5.0

# How often to emit the "batch summary" log line — per-mode cycle count
# and average spots/cycle.  Match the pskreporter library's batch
# interval so the summary lines up with the visible "uploading N spots"
# events.  PSKREPORTER_INTERVAL env defaults to 180 s upstream; 30 s in
# our coordination.env.
import os as _os  # local: avoid shadowing top-level imports
BATCH_REPORT_SEC = float(_os.environ.get("PSKREPORTER_INTERVAL", "30"))


class PskReporterUploader:
    """ClickHouse-backed PSK Reporter spot uploader.

    Lifecycle:
      * ``start()`` — connect to ClickHouse + pskreporter, spawn poll thread
      * ``stop()``  — signal thread, drain pskreporter timer, close client

    Failure modes degrade gracefully:
      * ``SIGMOND_CLICKHOUSE_URL`` unset       → no-op (debug log)
      * ``clickhouse_connect`` not installed   → warning, no-op
      * ``pskreporter`` library not installed  → warning, no-op
      * ClickHouse unreachable mid-run         → warning per poll, retry
    """

    def __init__(
        self,
        callsign: str,
        grid_square: str,
        antenna: str = "",
        radiod_id: str = "",
        use_tcp: bool = True,
    ):
        # TCP is the default: PSK Reporter accepts both UDP (lossy, fits
        # in a single datagram) and TCP (delivery-confirmed, larger
        # packets up to 25 KiB).  TCP is the right choice on a
        # high-volume FT8/FT4 receiver — UDP can drop spots silently
        # under load and provides no feedback that the server got them.
        # Operators on bandwidth-constrained links can pass use_tcp=False.
        self._callsign = callsign
        self._grid_square = grid_square
        self._antenna = antenna
        self._radiod_id = radiod_id
        self._use_tcp = use_tcp
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: Any = None
        self._reporter: Any = None
        # High-water mark on psk.spots.time — spots whose time > this
        # haven't been queued for upload yet.  Initialized to "now" at
        # start() so historical spots aren't re-uploaded.  Tracked in
        # UTC since psk.spots.time is a UTC DateTime column.
        self._last_seen_time: Optional[datetime] = None
        self._uploaded_count = 0
        # Cycle-stats accumulator.  Every BATCH_REPORT_SEC seconds (set
        # to match PSKREPORTER_INTERVAL so summaries align with the
        # library's "uploading N spots" log lines) we log a roll-up:
        # per-mode cycle count + average spots per cycle.  A "cycle" =
        # a distinct decode `time` value within the mode.
        self._batch_window_start: float = 0.0
        self._batch_cycle_times: dict = {"ft8": set(), "ft4": set()}
        self._batch_spot_counts: dict = {"ft8": 0, "ft4": 0}

    # ----- lifecycle -----

    def start(self) -> None:
        if not self._callsign or not self._grid_square:
            logger.warning(
                "psk-uploader-ch: callsign / grid not configured; skipping",
            )
            return

        # ClickHouse client.
        try:
            from sigmond.hamsci_ch.writer import ConnectionConfig
        except ImportError as e:
            logger.warning("psk-uploader-ch disabled: %s", e)
            return
        cfg = ConnectionConfig.from_env()
        if cfg is None:
            logger.debug(
                "psk-uploader-ch: SIGMOND_CLICKHOUSE_URL unset; noop",
            )
            return
        try:
            import clickhouse_connect  # type: ignore[import-not-found]
            from urllib.parse import urlparse
            u = urlparse(cfg.url)
            self._client = clickhouse_connect.get_client(
                host=u.hostname,
                port=u.port or 8123,
                username=cfg.user,
                password=cfg.password(),
            )
        except ImportError as e:
            logger.warning(
                "psk-uploader-ch disabled: clickhouse-connect missing (%s)", e,
            )
            return
        except Exception as e:
            logger.warning(
                "psk-uploader-ch: ClickHouse connect failed: %s", e,
            )
            return

        # PSK Reporter UDP client.  The library batches and self-flushes
        # on its `interval` (set via PSKREPORTER_INTERVAL env).
        try:
            import pskreporter  # type: ignore[import-not-found]
        except ImportError as e:
            logger.warning(
                "psk-uploader-ch: pskreporter library missing (%s)", e,
            )
            return
        try:
            self._reporter = pskreporter.PskReporter(
                callsign=self._callsign,
                grid=self._grid_square,
                antenna=self._antenna,
                tcp=self._use_tcp,
            )
        except Exception as e:
            logger.warning(
                "psk-uploader-ch: PskReporter init failed: %s", e,
            )
            return

        # Keep the watermark tz-aware UTC.  clickhouse-connect serializes
        # naive datetimes using the SERVER's configured timezone (which
        # may be America/Chicago on this host), causing the WHERE-clause
        # comparison to land 5 h in the future and return zero rows.
        self._last_seen_time = datetime.now(timezone.utc)
        self._batch_window_start = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="psk-uploader-ch",
        )
        self._thread.start()
        logger.info(
            "psk-uploader-ch started: %s/%s (poll=%ds, batch interval from "
            "PSKREPORTER_INTERVAL env)",
            self._callsign, self._grid_square, int(POLL_INTERVAL_SEC),
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        # Stop the pskreporter library's batch timer so the process
        # can exit cleanly.
        try:
            import pskreporter  # type: ignore[import-not-found]
            pskreporter.PskReporter.stop()
        except Exception:
            pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        if self._uploaded_count:
            logger.info(
                "psk-uploader-ch stopped after queueing %d spots",
                self._uploaded_count,
            )

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ----- polling loop -----

    def _run(self) -> None:
        while not self._stop.wait(POLL_INTERVAL_SEC):
            try:
                self._poll_once()
            except Exception:
                logger.exception(
                    "psk-uploader-ch: unhandled error in poll loop",
                )

    def _poll_once(self) -> None:
        # Pull rows newer than our high-water mark.  Filter out rows
        # without a parsed callsign (we can't report those to PSK
        # Reporter) and limit the per-poll batch so a backed-up
        # ClickHouse doesn't return a million rows in one go.
        if self._client is None or self._reporter is None:
            return
        try:
            # Filter on `ingested_at` (DEFAULT now() at INSERT) rather
            # than decode `time` (the spot's UTC decode timestamp).  The
            # decode timestamp is non-monotonic across modes: ChTailer
            # has separate per-mode batches that flush asynchronously
            # (ft8 every 15 s, ft4 every 7.5 s of decode cycle plus its
            # own 15 s flush window), so a poll that catches an ft4
            # batch first advances the watermark past the upcoming ft8
            # batch's `time` values, permanently excluding them.
            # `ingested_at` is set by ClickHouse at INSERT time and only
            # ever moves forward, so the watermark is race-free.
            # The original `time` is still in the row and is what we
            # ship to PSK Reporter.
            # Filter by radiod_id so each psk-recorder instance ships
            # only its own decodes.  Without this, two instances on the
            # same host both pull every row and upload everything under
            # their own callsign — every spot lands at PSK Reporter
            # twice, each attributed to a different reporter.  ChTailer
            # tags every row with radiod_id at insert time, so this is
            # a strict 1:1 with the running daemon.
            result = self._client.query(
                "SELECT time, frequency, mode, snr_db, tx_call, grid, "
                "ingested_at "
                "FROM psk.spots "
                "WHERE ingested_at > {since:DateTime} "
                "  AND radiod_id = {rid:String} "
                "  AND tx_call != '' "
                "  AND mode IN ('ft8', 'ft4') "
                "ORDER BY ingested_at "
                "LIMIT 1000",
                parameters={
                    "since": self._last_seen_time,
                    "rid": self._radiod_id,
                },
            )
            rows = result.result_rows
        except Exception as e:
            logger.warning("psk-uploader-ch: query failed: %s", e)
            return
        if not rows:
            return

        latest_ingested = self._last_seen_time
        ft8_n = 0
        ft4_n = 0
        for spot_time, freq_hz, mode, snr_db, tx_call, grid, ingested_at in rows:
            # Normalize spot_time to tz-aware UTC.  clickhouse-connect
            # may return naive datetimes (server-tz-relative) or
            # tz-aware ones depending on column metadata; standardize.
            if spot_time.tzinfo is None:
                # Naive datetimes from clickhouse-connect represent the
                # value in the *server's* timezone.  Reinterpret as
                # UTC so we don't shift by the server-local offset.
                spot_aware = spot_time.replace(tzinfo=timezone.utc)
            else:
                spot_aware = spot_time.astimezone(timezone.utc)
            try:
                self._reporter.spot(
                    callsign=tx_call,
                    frequency=int(freq_hz),
                    # PSK Reporter wants uppercase mode tags.
                    mode=mode.upper() if mode else "",
                    timestamp=int(spot_aware.timestamp()),
                    db=int(snr_db) if snr_db is not None else -128,
                    locator=grid or "",
                )
                self._uploaded_count += 1
                if mode == "ft8":
                    ft8_n += 1
                    self._batch_spot_counts["ft8"] += 1
                    self._batch_cycle_times["ft8"].add(spot_aware)
                elif mode == "ft4":
                    ft4_n += 1
                    self._batch_spot_counts["ft4"] += 1
                    self._batch_cycle_times["ft4"].add(spot_aware)
            except Exception as e:
                logger.warning(
                    "psk-uploader-ch: spot() rejected row "
                    "(call=%s freq=%s mode=%s): %s",
                    tx_call, freq_hz, mode, e,
                )
                continue
            # Advance watermark by INGEST order, not decode time.  See
            # the SELECT comment for why.
            ingested_aware = (
                ingested_at if ingested_at.tzinfo is not None
                else ingested_at.replace(tzinfo=timezone.utc)
            )
            if ingested_aware > latest_ingested:
                latest_ingested = ingested_aware
        self._last_seen_time = latest_ingested
        # INFO so `smd psk-watch` can pick this up without DEBUG noise.
        # The pskreporter library's own "uploading N spots" message
        # comes when the batch flushes (every PSKREPORTER_INTERVAL s);
        # this line is the per-poll feed rate going INTO that batch.
        logger.info(
            "psk-uploader-ch: queued ft8=%d ft4=%d (total %d)",
            ft8_n, ft4_n, self._uploaded_count,
        )

        # Per-batch summary (matches pskreporter's flush cadence):
        # cycle counts + avg spots/cycle for the last BATCH_REPORT_SEC.
        # Fires even when only one mode had cycles in the window.
        elapsed = time.monotonic() - self._batch_window_start
        if elapsed >= BATCH_REPORT_SEC:
            ft8_cycles = len(self._batch_cycle_times["ft8"])
            ft4_cycles = len(self._batch_cycle_times["ft4"])
            ft8_spots = self._batch_spot_counts["ft8"]
            ft4_spots = self._batch_spot_counts["ft4"]
            ft8_avg = ft8_spots / ft8_cycles if ft8_cycles else 0.0
            ft4_avg = ft4_spots / ft4_cycles if ft4_cycles else 0.0
            logger.info(
                "psk-uploader-ch: batch %.0fs window: "
                "ft8 cycles=%d (avg %.1f/cyc, %d total)  "
                "ft4 cycles=%d (avg %.1f/cyc, %d total)",
                elapsed, ft8_cycles, ft8_avg, ft8_spots,
                ft4_cycles, ft4_avg, ft4_spots,
            )
            # Reset the window.
            self._batch_window_start = time.monotonic()
            self._batch_cycle_times = {"ft8": set(), "ft4": set()}
            self._batch_spot_counts = {"ft8": 0, "ft4": 0}
