"""ClickHouse tailer for psk-recorder (CONTRACT v0.6 §17).

Watches the per-mode spot-log file `<log_dir>/<radiod_id>-{ft8,ft4}.log`
that `decode_ft8` writes to, parses each new line, and inserts rows
into `psk.spots` via `sigmond.hamsci_ch.Writer`.  Runs as a daemon
thread inside the PskRecorder process, parallel to PskReporterUploader
(which also tails the same log file via `pskreporter-sender`).

The CH path is additive: when `SIGMOND_CLICKHOUSE_URL` is unset the
tailer is a clean no-op (writer stays in noop mode), and pskreporter
uploads are unaffected.  When SFTP/PSKReporter is eventually retired
in favor of `hs-uploader`, this tailer remains as the producer-side
sink.

Wire format from `decode_ft8.c:363` (ka9q/ft8_lib):
    fprintf(stdout,"%4d/%02d/%02d %02d:%02d:%02d %3d %+4.2lf %'.1lf ~ %s\\n",
            year, mo, day, hr, mn, sec, score, dt, freq_hz, msg);

The `'.1lf` uses locale grouping — strip `,` (and any whitespace) from
the freq token before parsing.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Line parser ─────────────────────────────────────────────────────────────

# `<call>[/<suffix>]` — 1–3 alnum prefix, 1 digit, 1–4 alnum suffix
# (lossy, but it's only used for best-effort parse of the freeform
# message; the raw text is always preserved).
_CALL_RE = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}(?:/[A-Z0-9]{1,4})?$")
# Maidenhead 6-char form has uppercase field+square but lowercase
# subsquare (per IARU convention).  Tolerate either case for robustness.
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}(?:[A-Xa-x]{2})?$")
_REPORT_RE = re.compile(r"^R?([+-]?\d+)$")


def parse_decode_ft8_line(line: str, *, mode: str) -> Optional[dict]:
    """Parse one decode_ft8 stdout line into a psk.spots row.

    Returns None on any parse failure — callers should skip silently.
    `mode` is the mode tag from the slot worker ('ft8' or 'ft4'); the
    decoder line itself doesn't carry it.
    """
    line = line.strip()
    if not line or "~" not in line:
        return None
    head, _, message = line.partition("~")
    parts = head.split()
    if len(parts) < 5:
        return None
    try:
        ts = datetime.strptime(parts[0] + " " + parts[1], "%Y/%m/%d %H:%M:%S")
        score = int(parts[2])
        dt = float(parts[3])
        freq = float(parts[4].replace(",", "").replace(" ", ""))
    except (ValueError, IndexError):
        return None

    message = message.strip()
    parsed = _parse_message(message)
    return {
        "time":          ts,
        "mode":          mode,
        "score":         score,
        "dt":            dt,
        "frequency":     int(freq),
        "frequency_mhz": freq / 1_000_000.0,
        "message":       message,
        "tx_call":       parsed.get("tx_call", ""),
        "rx_call":       parsed.get("rx_call", ""),
        "grid":          parsed.get("grid", ""),
        "report":        parsed.get("report"),
    }


def _parse_message(message: str) -> dict:
    """Best-effort parse of a decoded FT8/FT4 message body.

    Recognized shapes (all approximate; freeform messages return empty):
      "CQ <tx_call> [<grid>]"           — directed CQ
      "<rx_call> <tx_call> <grid>"      — first contact w/ grid
      "<rx_call> <tx_call> [R]<report>" — signal report
      "<rx_call> <tx_call> [73|RR73]"   — close

    Anything not matching shape returns whatever fields we can pull
    out, the rest empty.  The raw `message` text is always preserved
    by the caller.
    """
    out: dict[str, Any] = {}
    tokens = message.split()
    if not tokens:
        return out

    # Slice off the first 1–3 tokens for call positions.
    if tokens[0] == "CQ":
        # "CQ [target] <tx_call> [grid]" — `target` may be a region
        # tag like "DX", "EU", "POTA" that isn't a callsign.  Scan
        # past non-call tokens until we hit the first call-shaped one
        # (the sender), then look for a grid in the remaining tokens.
        for i, tok in enumerate(tokens[1:], start=1):
            if _CALL_RE.match(tok):
                out["tx_call"] = tok
                for later in tokens[i + 1:]:
                    if _GRID_RE.match(later):
                        out["grid"] = later
                        break
                break
    else:
        # <rx_call> <tx_call> [grid|report|RR73|73]
        if _CALL_RE.match(tokens[0]):
            out["rx_call"] = tokens[0]
        if len(tokens) >= 2 and _CALL_RE.match(tokens[1]):
            out["tx_call"] = tokens[1]
        if len(tokens) >= 3:
            tail = tokens[2]
            if _GRID_RE.match(tail):
                out["grid"] = tail
            else:
                m = _REPORT_RE.match(tail)
                if m:
                    try:
                        out["report"] = int(m.group(1))
                    except ValueError:
                        pass
    return out


# ── Tailer ──────────────────────────────────────────────────────────────────

class ChTailer:
    """One tailer per (radiod, mode) log file.

    Spawns a daemon thread that polls the log for new lines, parses
    them, and inserts rows into `psk.spots` via hamsci_ch.Writer.
    No-op when SIGMOND_CLICKHOUSE_URL is unset.
    """

    POLL_INTERVAL_SEC = 1.0       # how often to read new lines
    FLUSH_INTERVAL_SEC = 15.0     # max age of an unflushed batch

    def __init__(
        self,
        *,
        log_path: Path,
        mode: str,
        radiod_id: str,
        host_call: str = "",
        host_grid: str = "",
        processing_version: str = "",
        batch_rows: int = 200,
        writer_factory=None,
    ) -> None:
        self._log_path = Path(log_path)
        self._mode = mode
        self._radiod_id = radiod_id
        self._host_call = host_call
        self._host_grid = host_grid
        self._processing_version = processing_version
        self._batch_rows = batch_rows
        self._writer_factory = writer_factory or _default_writer_factory
        self._writer = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_pos = 0
        self._last_flush = 0.0

    # ----- lifecycle -----

    def start(self) -> None:
        """Build the writer and start the polling thread.

        Returns immediately. If SIGMOND_CLICKHOUSE_URL is unset the
        writer is a no-op and we still start the thread (so health is
        observable via `is_active`).  Failure to import the writer
        package is logged and the thread exits.
        """
        try:
            self._writer = self._writer_factory(self._batch_rows)
        except Exception as e:
            logger.warning("ch_tailer disabled (%s): %s", self._mode, e)
            return
        if self._writer.is_noop:
            logger.debug("ch_tailer %s: SIGMOND_CLICKHOUSE_URL unset; noop",
                         self._mode)
        # Skip historical content — only tail from current end.
        if self._log_path.exists():
            try:
                self._last_pos = self._log_path.stat().st_size
            except OSError:
                self._last_pos = 0
        self._stop.clear()
        self._last_flush = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"ch-tail-{self._mode}-{self._radiod_id}",
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def health(self) -> str:
        if self._writer is None:
            return "noop"
        return self._writer.health

    # ----- polling loop -----

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.POLL_INTERVAL_SEC):
                self._poll_once()
        except Exception:
            logger.exception("ch_tailer %s: unhandled error in poll loop", self._mode)

    def _poll_once(self) -> None:
        if self._writer is None:
            return
        try:
            stat = self._log_path.stat()
        except FileNotFoundError:
            return
        size = stat.st_size
        if size < self._last_pos:
            # File was rotated; reset to head.
            self._last_pos = 0
        if size > self._last_pos:
            try:
                with open(self._log_path, "rb") as fh:
                    fh.seek(self._last_pos)
                    chunk = fh.read(size - self._last_pos)
                self._last_pos = size
            except OSError as e:
                logger.warning("ch_tailer %s: read failed: %s", self._mode, e)
                return
            self._consume(chunk.decode(errors="replace"))

        # Periodic flush even if no new data, so a partial batch
        # doesn't sit indefinitely.
        if (time.monotonic() - self._last_flush) > self.FLUSH_INTERVAL_SEC:
            try:
                self._writer.flush()
            except Exception as e:
                logger.warning("ch_tailer %s: flush failed: %s", self._mode, e)
            self._last_flush = time.monotonic()

    def _consume(self, text: str) -> None:
        rows: list[dict] = []
        for line in text.splitlines():
            row = parse_decode_ft8_line(line, mode=self._mode)
            if row is None:
                continue
            row["host_call"] = self._host_call
            row["host_grid"] = self._host_grid
            row["radiod_id"] = self._radiod_id
            row["instance"] = self._radiod_id
            row["processing_version"] = self._processing_version
            rows.append(row)
        if rows:
            try:
                self._writer.insert(rows)
            except Exception as e:
                logger.warning("ch_tailer %s: insert failed (%d rows): %s",
                               self._mode, len(rows), e)


def _default_writer_factory(batch_rows: int):
    """Lazy-import `sigmond.hamsci_ch.Writer` for `psk.spots`.

    Sigmond core stays stdlib-only; this import only happens when a
    tailer actually starts, and the writer is itself a no-op when CH
    is not configured.
    """
    from sigmond.hamsci_ch import Writer
    return Writer.from_env(
        table="spots", mode="psk",
        schema_version=1, batch_rows=batch_rows,
    )
