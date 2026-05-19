"""Spot-log tailer for psk-recorder (CONTRACT v0.6 §17).

Watches the per-mode spot-log file `<log_dir>/<radiod_id>-{ft8,ft4}.log`
that `decode_ft8` writes to, parses each new line, and inserts rows
into `psk.spots` via `sigmond.hamsci_sink.Writer.from_env()`.  Runs as a
daemon thread inside the PskRecorder process, parallel to the
HsPskReporterUploader's PSKReporter upload path.

`Writer.from_env()` stages rows into sigmond's local SQLite sink by
default (`/var/lib/sigmond/sink.db`); `hs-uploader`'s `SqliteSource`
is the reader half.  This tailer is the producer-side sink that path
depends on, and is additive — pskreporter uploads are unaffected.
The writer resolves to a clean no-op only when the sink path is
unwritable (e.g. a standalone host outside a sigmond install).

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Line parser ─────────────────────────────────────────────────────────────

# Standard callsigns + WSJT-X compound forms:
#   * standard ITU call:                        K1ABC, AC0G, JA1AAA
#   * suffix-form compound:                     K1ABC/QRP, K1ABC/MM
#   * prefix-form compound (region/portable):   VE3/K1ABC, G/K1ABC, KH6/AC0G
# The regex is intentionally lossy — it's a best-effort filter for
# the freeform message field; the raw `message` text is always
# preserved by the caller.
_CALL_RE = re.compile(
    r"^"
    r"(?:[A-Z0-9]{1,3}/)?"               # optional prefix (e.g. "VE3/", "G/")
    r"[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}"    # standard call body (XX[X][D][YY[Y][Y]])
    r"(?:/[A-Z0-9]{1,4})?"                # optional suffix (e.g. "/QRP", "/MM")
    r"$"
)
# Maidenhead 6-char form has uppercase field+square but lowercase
# subsquare (per IARU convention).  Tolerate either case for robustness.
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}(?:[A-Xa-x]{2})?$")
_REPORT_RE = re.compile(r"^R?([+-]?\d+)$")

# Format-detection regexes for the dual-decoder router.
_DECODE_FT8_PREFIX = re.compile(r"^\d{4}/\d{2}/\d{2}\s")     # YYYY/MM/DD …
# YYMMDD HHMMSS BAND_FREQ_HZ … — slot.py prepends the slot's date,
# 6-digit HHMMSS, and the band's tuned frequency in Hz so the parser
# can compute absolute receive frequency = BAND_FREQ_HZ + jt9 offset.
_JT9_PREFIX        = re.compile(r"^\d{6}\s\d{6}\s\d+\s")     # YYMMDD HHMMSS BAND_HZ …


def parse_decoder_line(line: str, *, mode: Optional[str] = None) -> Optional[dict]:
    """Auto-detect decoder format and parse.

    The per-mode log file (`<radiod_id>-<mode>.log`) may carry lines
    from either ``decode_ft8`` (legacy fallback) or ``jt9`` (current
    default; canonical WSJT-X-ish format with date prefix added by
    SlotWorker).  We look at the leading byte run to pick a parser.

    Returns ``None`` on unrecognised structure (header line, blank,
    junk).  Caller should skip silently.
    """
    stripped = line.strip()
    if not stripped:
        return None
    if _JT9_PREFIX.match(stripped):
        return parse_jt9_line(stripped, mode=mode)
    if _DECODE_FT8_PREFIX.match(stripped):
        # decode_ft8 emits its own mode-agnostic format; if we don't
        # know the mode (router called without a hint), we leave it
        # blank — caller may set it from the log file path.
        return parse_decode_ft8_line(stripped, mode=mode or "")
    return None


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
        # decode_ft8 emits timestamps in UTC; tag the parsed datetime
        # tz-aware so the sink writer serializes it unambiguously
        # rather than guessing a local timezone.
        ts = datetime.strptime(
            parts[0] + " " + parts[1], "%Y/%m/%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        score = int(parts[2])
        dt = float(parts[3])
        freq = float(parts[4].replace(",", "").replace(" ", ""))
    except (ValueError, IndexError):
        return None

    message = message.strip()
    parsed = _parse_message(message)
    return {
        "time":               ts,
        "mode":               mode,
        "decoder_kind":       "decode_ft8",
        "score":              score,
        "snr_db":             None,        # ft8_lib's `score` ≠ calibrated dB
        "spectral_width_hz":  None,        # not surfaced by decode_ft8
        "dt":                 dt,
        "frequency":          int(freq),
        "frequency_mhz":      freq / 1_000_000.0,
        "message":            message,
        "tx_call":            parsed.get("tx_call", ""),
        "rx_call":            parsed.get("rx_call", ""),
        "grid":               parsed.get("grid", ""),
        "report":             parsed.get("report"),
    }


def parse_jt9_line(line: str, *, mode: Optional[str] = None) -> Optional[dict]:
    """Parse one native jt9 line into a psk.spots row.

    Format emitted by ``slot.SlotWorker._materialise_jt9_output``::

        YYMMDD HHMMSS BAND_FREQ_HZ SYNC SNR DT FREQ_OFFSET_HZ MARKER MESSAGE... MODE

    Where::

        YYMMDD              decimal date (UTC, 6 digits)
        HHMMSS              slot start time (UTC, 6 digits)
        BAND_FREQ_HZ        the radiod channel's tuned frequency (integer Hz);
                            absolute receive freq = BAND_FREQ_HZ + FREQ_OFFSET_HZ
        SYNC                sync confidence (0..~100; jt9-internal score)
        SNR                 calibrated dB SNR (signed integer)
        DT                  time-offset within slot (signed float, seconds)
        FREQ_OFFSET_HZ      baseband frequency offset (float, trailing ``.``)
        MARKER              packet-quality / hash-resolution flag
                            ('0', '?', '~') — not stored
        MESSAGE             one or more whitespace-separated tokens
        MODE                "FT8" or "FT4" — emitted by jt9 itself

    Field mapping into ``psk.spots``:

        SYNC                  → score        (Int16; jt9 sync confidence)
        SNR                   → snr_db       (Float32; calibrated dB)
        DT                    → dt           (Float32)
        BAND_FREQ + OFFSET    → frequency    (Int64; absolute Hz)
        MSG                   → message      (raw String)
        MODE                  → mode

    Returns ``None`` on any parse failure.
    """
    parts = line.strip().split()
    # 10 = YYMMDD HHMMSS BAND SYNC SNR DT FREQ MARKER MSG_ONE_TOKEN MODE
    # — minimum plausible row with a single-token message.
    if len(parts) < 10:
        return None

    # Date + time → UTC datetime (slot HHMMSS has full second resolution).
    # slot.py emits these in UTC; tag tz-aware so the sink writer
    # doesn't reinterpret them in a local timezone.
    try:
        ts = datetime.strptime(
            parts[0] + parts[1], "%y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    try:
        band_freq_hz = int(parts[2])
        sync_score = int(parts[3])
        snr_db = int(parts[4])
        dt = float(parts[5])
        # jt9 emits frequency offset with trailing ``.`` ("1397.").
        freq_offset_hz = int(float(parts[6]))
    except (ValueError, IndexError):
        return None

    abs_freq_hz = band_freq_hz + freq_offset_hz

    # parts[7] is the MARKER ('0' / '?' / '~') — skip; not in schema.
    # Last token is the MODE tag; everything from parts[8] up to (but
    # not including) MODE is the message.
    last = parts[-1]
    if last.upper() in ("FT8", "FT4"):
        detected_mode = last.lower()
        message_end = len(parts) - 1
    else:
        # Mode token missing — use caller's hint if any; treat full tail
        # as message text.
        detected_mode = ""
        message_end = len(parts)

    message_tokens = parts[8:message_end]
    if not message_tokens:
        return None
    message = " ".join(message_tokens)
    parsed = _parse_message(message)

    return {
        "time":               ts,
        "mode":               detected_mode or (mode or ""),
        "decoder_kind":       "jt9",
        "score":              sync_score,
        "snr_db":             snr_db,
        "spectral_width_hz":  None,
        "dt":                 dt,
        # frequency is ABSOLUTE Hz (band + offset) — matches the
        # original 001_create_spots.sql schema intent and is what
        # PSK Reporter / wsprnet need.
        "frequency":          abs_freq_hz,
        "frequency_mhz":      abs_freq_hz / 1_000_000.0,
        "message":            message,
        "tx_call":            parsed.get("tx_call", ""),
        "rx_call":            parsed.get("rx_call", ""),
        "grid":               parsed.get("grid", ""),
        "report":             parsed.get("report"),
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
        # Bracketed compound calls (`<K1ABC/QRP>`) are stripped before
        # the regex match so they land as the tx_call.
        for i, tok in enumerate(tokens[1:], start=1):
            candidate = _strip_call_brackets(tok)
            if candidate is not None and _CALL_RE.match(candidate):
                out["tx_call"] = candidate
                for later in tokens[i + 1:]:
                    if _GRID_RE.match(later):
                        out["grid"] = later
                        break
                break
    else:
        # <rx_call> <tx_call> [grid|report|RR73|73]
        # Compound callsigns may appear bracketed (`<K1ABC/QRP>`) — that's
        # what jt9 / wsprd substitute when they resolved a hash from
        # their session table.  Strip the brackets before matching so
        # the call lands in the row instead of being dropped.
        rx_candidate = _strip_call_brackets(tokens[0])
        if rx_candidate is not None and _CALL_RE.match(rx_candidate):
            out["rx_call"] = rx_candidate
        if len(tokens) >= 2:
            tx_candidate = _strip_call_brackets(tokens[1])
            if tx_candidate is not None and _CALL_RE.match(tx_candidate):
                out["tx_call"] = tx_candidate
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


def _strip_call_brackets(token: str) -> Optional[str]:
    """Strip surrounding ``<>`` from a token if it matches the WSJT-X
    bracketed-call shape.

    Returns the stripped call, or the original token if no brackets.
    Returns ``None`` for the literal "<...>" placeholder (unresolved
    hash, no recoverable callsign).
    """
    if not token:
        return token
    if token == "<...>":
        return None
    if token.startswith("<") and token.endswith(">") and len(token) > 2:
        return token[1:-1]
    return token


# ── Tailer ──────────────────────────────────────────────────────────────────

class ChTailer:
    """One tailer per (radiod, mode) log file.

    Spawns a daemon thread that polls the log for new lines, parses
    them, and inserts rows into `psk.spots` via hamsci_sink.Writer.
    Clean no-op only when the sink path is unwritable.
    """

    POLL_INTERVAL_SEC = 1.0       # how often to read new lines
    FLUSH_INTERVAL_SEC = 15.0     # max age of an unflushed batch
    CALLHASH_SAVE_INTERVAL_SEC = 300.0  # persist callhash table at most every 5 min

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
        callhash_path: Optional[Path] = None,
        forward_to_pskreporter: bool = True,
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
        # Tags every row written to the local sink so the wsprdaemon
        # server's gw1-elected pskreporter_forwarder knows whether to
        # POST it to pskreporter.info. Controlled by PSK_DELIVERY_MODE
        # in recorder.py — True for "server", False for "both".
        self._forward_to_pskreporter = bool(forward_to_pskreporter)

        # WSJT-X compound-callsign hash table.  Per-radiod (shared
        # across modes — same compound calls show up on FT8 and FT4).
        # When callhash_path is provided, the table is persisted across
        # daemon restarts so the cumulative resolution grows over
        # time.  Lazy-imported so the tailer remains importable on
        # hosts that don't have sigmond installed.
        self._callhash_path = callhash_path
        self._callhash = self._make_callhash_table(callhash_path)
        self._last_callhash_save = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_pos = 0
        self._last_flush = 0.0

    # ----- lifecycle -----

    def start(self) -> None:
        """Build the writer and start the polling thread.

        Returns immediately. If the writer resolves to a no-op (sink
        path unwritable) we still start the thread, so health stays
        observable via `is_active`.  Failure to import the writer
        package is logged and the thread exits.
        """
        try:
            self._writer = self._writer_factory(self._batch_rows)
        except Exception as e:
            logger.warning("ch_tailer disabled (%s): %s", self._mode, e)
            return
        if self._writer.is_noop:
            logger.debug("ch_tailer %s: sink writer is a no-op "
                         "(sink path unwritable)", self._mode)
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
        # Final callhash persistence so any observations since the
        # last periodic save aren't lost.
        if self._callhash is not None and self._callhash_path is not None:
            try:
                self._callhash.save()
            except Exception as exc:
                logger.warning("ch_tailer %s: final callhash save failed: %s",
                               self._mode, exc)

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
        # Feed the whole chunk to the callhash table first so any
        # `<call>` announcements (compound or resolved) are captured
        # in our cumulative cache before per-line parsing.  This is
        # cheap (a single regex scan) and makes the table grow without
        # caring about line boundaries.
        if self._callhash is not None:
            try:
                self._callhash.observe(text)
            except Exception as exc:
                logger.warning("ch_tailer %s: callhash observe failed: %s",
                               self._mode, exc)

        rows: list[dict] = []
        for line in text.splitlines():
            row = parse_decoder_line(line, mode=self._mode)
            if row is None:
                continue
            row["host_call"] = self._host_call
            row["host_grid"] = self._host_grid
            row["radiod_id"] = self._radiod_id
            row["instance"] = self._radiod_id
            row["processing_version"] = self._processing_version
            row["forward_to_pskreporter"] = self._forward_to_pskreporter
            rows.append(row)
        if rows:
            try:
                self._writer.insert(rows)
            except Exception as e:
                logger.warning("ch_tailer %s: insert failed (%d rows): %s",
                               self._mode, len(rows), e)

        # Periodic callhash persistence.  CALLHASH_SAVE_INTERVAL_SEC is
        # generous (5 min) to amortise the JSON write across many
        # observations.  No-op when nothing changed since the last save.
        if (
            self._callhash is not None
            and self._callhash_path is not None
            and (time.monotonic() - self._last_callhash_save)
                > self.CALLHASH_SAVE_INTERVAL_SEC
        ):
            try:
                self._callhash.save()
            except Exception as exc:
                logger.warning("ch_tailer %s: callhash save failed: %s",
                               self._mode, exc)
            self._last_callhash_save = time.monotonic()

    def _make_callhash_table(self, path: Optional[Path]):
        """Construct (or load) the per-radiod CallHashTable.

        Returns None when ``callhash`` isn't importable — keeps
        psk-recorder runnable on hosts without the callhash library.
        """
        try:
            from callhash import CallHashTable  # type: ignore[import-not-found]
        except ImportError as exc:
            logger.debug(
                "ch_tailer %s: callhash library unavailable (%s); "
                "compound-callsign hash resolution disabled",
                self._mode, exc,
            )
            return None
        if path is None:
            return CallHashTable()
        return CallHashTable.load_or_new(path)


def _default_writer_factory(batch_rows: int):
    """Lazy-import `sigmond.hamsci_sink.Writer` for `psk.spots`.

    Sigmond core stays stdlib-only; this import only happens when a
    tailer actually starts.  `Writer.from_env()` resolves the backend
    (sigmond's SQLite sink by default); the writer is itself a no-op
    when the sink path is unwritable.

    `schema_version=2` is the tag every staged row carries.  The
    `hs-uploader` reader (`SqliteSource.accepted_schema_versions=[2]`)
    filters on it — so the producer must tag rows at the matching
    version or the source silently treats them as stale-schema and
    yields nothing.
    """
    from sigmond.hamsci_sink import Writer
    return Writer.from_env(
        table="spots", mode="psk",
        schema_version=2, batch_rows=batch_rows,
    )
