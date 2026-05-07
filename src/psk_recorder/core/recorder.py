"""PskRecorder: orchestrates one radiod's FT4/FT8 channels.

One PskRecorder per radiod instance (= one systemd unit).
Creates ChannelStream objects for each frequency, manages log
file descriptors, and supervises PskReporterUploaders.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Optional

from psk_recorder.config import (
    FT4_CADENCE_SEC,
    FT8_CADENCE_SEC,
    get_freqs,
    get_mode_params,
    resolve_radiod_status,
)
from psk_recorder.core.stream import ChannelSink
from psk_recorder.core.ch_tailer import ChTailer
from psk_recorder.core.uploader import PskReporterUploader

logger = logging.getLogger(__name__)


class PskRecorder:
    """Manages all FT4/FT8 channels for a single radiod."""

    def __init__(self, config: dict, radiod_block: dict):
        self._config = config
        self._radiod = radiod_block
        self._radiod_id = radiod_block.get("id", "default")
        self._paths = config.get("paths", {})
        self._station = config.get("station", {})

        self._sinks: list[ChannelSink] = []
        self._multi_streams: list = []
        self._uploaders: list[PskReporterUploader] = []
        self._ch_tailers: list[ChTailer] = []
        self._log_fds: dict[str, object] = {}
        self._running = False

    def run(self) -> None:
        """Main entry: provision channels, start streams, block until signal."""
        self._running = True
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        try:
            self._provision_channels()
            self._start_streams()
            self._start_uploaders()
            self._start_ch_tailers()
            self._start_stats_thread()
            self._notify_ready()
            self._main_loop()
        except Exception:
            logger.exception("Fatal error in recorder")
        finally:
            self._shutdown()

    def _provision_channels(self) -> None:
        """Create ChannelSinks and register them with MultiStream(s).

        One MultiStream per unique multicast group, keyed on the
        (mcast_addr, port) returned by ensure_channel(). In the common
        case (FT8+FT4 share preset/sample_rate/encoding) all channels
        land on one group and we end up with a single MultiStream.
        """
        from ka9q import MultiStream, RadiodControl

        status = resolve_radiod_status(self._radiod)
        logger.info("Connecting to radiod at %s", status)
        self._control = RadiodControl(status)

        # Surface the Fusion governor identity at startup so the journal
        # record makes multi-radiod attribution clear. See
        # hf-timestd/docs/METROLOGY.md §4.5.1: when Fusion's governor
        # radiod differs from the one this recorder subscribes to, the
        # per-host clock-skew between the two radiods' hosts adds to
        # uncertainty. With a single-radiod station the two will match.
        try:
            from psk_recorder.core.authority_reader import AuthorityReader
            snap = AuthorityReader().read()
            governor = snap.governor_radiod if snap is not None else None
        except Exception as e:
            logger.debug("authority.json read at startup failed: %s", e)
            governor = None
        if governor and governor != status:
            logger.info(
                "Timing attribution: client_radiod=%s, fusion_governor_radiod=%s "
                "(differ — per-host clock-skew uncertainty applies to all spots)",
                status, governor,
            )
        elif governor:
            logger.info(
                "Timing attribution: client_radiod=fusion_governor_radiod=%s",
                status,
            )
        else:
            logger.info(
                "Timing attribution: client_radiod=%s, fusion_governor_radiod=<none> "
                "(hf-timestd authority not published — recorder will anchor via "
                "wall clock at stream start)",
                status,
            )

        spool_root = Path(self._paths.get(
            "spool_dir", "/var/lib/psk-recorder"
        )) / self._radiod_id
        log_dir = Path(self._paths.get(
            "log_dir", "/var/log/psk-recorder"
        ))
        # CONTRACT v0.6 — `decoder_kind` selects between WSJT-X jt9
        # (default, calibrated dB SNR + spectral width) and ka9q/
        # ft8_lib's decode_ft8 (fallback, internal "score" only).  The
        # paths are looked up per-kind: `paths.decoder_jt9` falls back
        # to `paths.decoder` for old configs that pre-date the swap.
        # See SlotWorker for output-format details.
        decoder_kind = str(self._paths.get("decoder_kind", "jt9")).lower()
        if decoder_kind == "jt9":
            decoder = self._paths.get(
                "decoder_jt9", self._paths.get(
                    "decoder", "/usr/local/bin/jt9",
                ),
            )
        else:
            decoder = self._paths.get(
                "decoder_decode_ft8", self._paths.get(
                    "decoder", "/usr/local/bin/decode_ft8",
                ),
            )
        decoder_depth = int(self._paths.get("decoder_depth", 3))
        keep_wav = self._paths.get("keep_wav", False)
        logger.info(
            "decoder_kind=%s path=%s depth=%d",
            decoder_kind, decoder, decoder_depth,
        )

        multi_by_group: dict[tuple, object] = {}

        for mode in ("ft8", "ft4"):
            freqs = get_freqs(self._radiod, mode)
            if not freqs:
                continue

            params = get_mode_params(self._radiod, mode)
            sample_rate = params["sample_rate"]
            preset = params["preset"]
            encoding_str = params.get("encoding", "s16be")
            encoding_int = _resolve_encoding(encoding_str)

            log_path = log_dir / f"{self._radiod_id}-{mode}.log"
            if mode not in self._log_fds:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_fds[mode] = open(log_path, "ab")

            for freq_hz in freqs:
                logger.info(
                    "Provisioning %s %d Hz (sr=%d, preset=%s, enc=%s)",
                    mode.upper(), freq_hz, sample_rate, preset, encoding_str,
                )
                sink = ChannelSink(
                    mode=mode,
                    frequency_hz=freq_hz,
                    sample_rate=sample_rate,
                    preset=preset,
                    encoding=encoding_int,
                    spool_dir=spool_root,
                    log_fd=self._log_fds[mode],
                    decoder_path=decoder,
                    decoder_kind=decoder_kind,
                    decoder_depth=decoder_depth,
                    keep_wav=keep_wav,
                )
                self._add_sink_to_multi(sink, multi_by_group)
                self._sinks.append(sink)

        self._multi_streams = list(multi_by_group.values())
        logger.info(
            "Provisioned %d channels across %d multicast group(s) on radiod %s",
            len(self._sinks), len(self._multi_streams), self._radiod_id,
        )

    def _add_sink_to_multi(
        self, sink: ChannelSink, multi_by_group: dict,
    ) -> None:
        """Attach sink to the MultiStream for its multicast group.

        Resolves the multicast group up-front via ensure_channel() so
        we can pick the right MultiStream (or create one) by
        (mcast_addr, port). MultiStream.add_channel() calls ensure_channel
        again internally — idempotent, one extra cheap status probe —
        but this keeps the grouping deterministic instead of relying on
        ValueError as control flow.
        """
        from ka9q import MultiStream

        info = self._control.ensure_channel(
            frequency_hz=float(sink.frequency_hz),
            preset=sink.preset,
            sample_rate=sink.sample_rate,
            encoding=sink.encoding,
        )
        key = (info.multicast_address, info.port)
        multi = multi_by_group.get(key)
        if multi is None:
            multi = MultiStream(control=self._control)
            multi_by_group[key] = multi

        multi.add_channel(
            frequency_hz=float(sink.frequency_hz),
            preset=sink.preset,
            sample_rate=sink.sample_rate,
            encoding=sink.encoding,
            on_samples=sink.on_samples,
            on_stream_dropped=sink.on_stream_dropped,
            on_stream_restored=sink.on_stream_restored,
        )

    def _start_streams(self) -> None:
        for sink in self._sinks:
            try:
                sink.start()
            except Exception:
                logger.exception(
                    "Failed to start sink %s %d Hz",
                    sink.mode, sink.frequency_hz,
                )
        for multi in self._multi_streams:
            try:
                multi.start()
            except Exception:
                logger.exception("Failed to start MultiStream")

    def _start_uploaders(self) -> None:
        pskreporter = self._paths.get(
            "pskreporter", "/usr/local/bin/pskreporter-sender"
        )
        callsign = self._station.get("callsign", "")
        grid = self._station.get("grid_square", "")

        if not callsign:
            logger.warning("No callsign configured — pskreporter will not start")
            return

        log_dir = Path(self._paths.get("log_dir", "/var/log/psk-recorder"))
        use_tcp = bool(self._paths.get("pskreporter_tcp", False))

        for mode in ("ft8", "ft4"):
            if not get_freqs(self._radiod, mode):
                continue
            log_path = log_dir / f"{self._radiod_id}-{mode}.log"
            uploader = PskReporterUploader(
                pskreporter_path=pskreporter,
                log_path=log_path,
                callsign=callsign,
                grid_square=grid,
                mode=mode,
                use_tcp=use_tcp,
            )
            uploader.start()
            self._uploaders.append(uploader)

    def _start_ch_tailers(self) -> None:
        """Start one ChTailer per (radiod, mode) — CONTRACT v0.6 §17.

        Each tailer watches the same `<radiod_id>-<mode>.log` file
        pskreporter-sender tails, parses new lines, and inserts rows
        into `psk.spots`.  No-op when SIGMOND_CLICKHOUSE_URL is unset.
        Failure to import / start is non-fatal: the existing PSKReporter
        upload path is unaffected.
        """
        log_dir = Path(self._paths.get("log_dir", "/var/log/psk-recorder"))
        callsign = self._station.get("callsign", "")
        grid = self._station.get("grid_square", "")

        try:
            from psk_recorder.version import GIT_INFO
            short = (GIT_INFO or {}).get("short", "")
        except Exception:
            short = ""
        try:
            from importlib.metadata import version as pkg_version
            ver = pkg_version("psk-recorder")
        except Exception:
            ver = "0.1.0"
        proc_version = f"{ver}+{short}" if short else ver

        # Per-radiod compound-callsign hash table.  Lives under the
        # spool dir (state, not log) and is shared across both modes
        # because the same compound calls show up on FT8 and FT4.
        spool_root = Path(self._paths.get(
            "spool_dir", "/var/lib/psk-recorder",
        )) / self._radiod_id
        callhash_path = spool_root / "callhash.json"

        for mode in ("ft8", "ft4"):
            if not get_freqs(self._radiod, mode):
                continue
            log_path = log_dir / f"{self._radiod_id}-{mode}.log"
            try:
                tailer = ChTailer(
                    log_path=log_path,
                    mode=mode,
                    radiod_id=self._radiod_id,
                    host_call=callsign,
                    host_grid=grid,
                    processing_version=proc_version,
                    callhash_path=callhash_path,
                )
                tailer.start()
                self._ch_tailers.append(tailer)
            except Exception:
                logger.exception(
                    "ch_tailer %s startup failed; PSKReporter path unaffected",
                    mode,
                )

    def _notify_ready(self) -> None:
        """Send sd_notify READY=1 if running under systemd."""
        try:
            addr = os.environ.get("NOTIFY_SOCKET")
            if addr:
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    if addr.startswith("@"):
                        addr = "\0" + addr[1:]
                    sock.connect(addr)
                    sock.sendall(b"READY=1")
                finally:
                    sock.close()
                logger.info("sd_notify READY=1 sent")
        except Exception:
            logger.debug("sd_notify failed (not running under systemd?)")

    def _start_stats_thread(self) -> None:
        self._stats_thread = threading.Thread(
            target=self._stats_loop, daemon=True, name="stats",
        )
        self._stats_thread.start()

    def _stats_loop(self) -> None:
        """Every 60 s, log a summary of decode + spot activity per mode.

        Spot count comes from counting lines added to each mode-log file
        (the file that decode_ft8 writes into and that pskreporter-sender
        tails). Decode count comes from each SlotWorker's own counters.
        """
        log_dir = Path(self._paths.get("log_dir", "/var/log/psk-recorder"))
        prev_ok: dict[str, int] = {}
        prev_fail: dict[str, int] = {}
        prev_empty: dict[str, int] = {}
        prev_spot_lines: dict[str, int] = {}

        def count_lines(p: Path) -> int:
            try:
                with open(p, "rb") as f:
                    return sum(1 for _ in f)
            except OSError:
                return 0

        # Align first report to the minute boundary + 60 s so the first
        # window isn't a partial-minute artifact.
        time.sleep(60.0)

        while self._running:
            snapshots = [s.stats_snapshot() for s in self._sinks]
            by_mode: dict[str, dict] = {}
            for snap in snapshots:
                m = snap["mode"]
                agg = by_mode.setdefault(m, {
                    "freqs": 0, "decodes_ok": 0, "decodes_fail": 0,
                    "slots_empty": 0,
                })
                agg["freqs"] += 1
                agg["decodes_ok"] += snap["decodes_ok"]
                agg["decodes_fail"] += snap["decodes_fail"]
                agg["slots_empty"] += snap["slots_empty"]

            for mode, agg in by_mode.items():
                spot_log = log_dir / f"{self._radiod_id}-{mode}.log"
                spot_lines_total = count_lines(spot_log)
                spots_delta = spot_lines_total - prev_spot_lines.get(mode, spot_lines_total)
                ok_delta = agg["decodes_ok"] - prev_ok.get(mode, 0)
                fail_delta = agg["decodes_fail"] - prev_fail.get(mode, 0)
                empty_delta = agg["slots_empty"] - prev_empty.get(mode, 0)

                logger.info(
                    "stats %s: spots=%d decodes=%d/%d slots_empty=%d freqs=%d (60s window)",
                    mode.upper(), spots_delta, ok_delta, ok_delta + fail_delta,
                    empty_delta, agg["freqs"],
                )

                prev_ok[mode] = agg["decodes_ok"]
                prev_fail[mode] = agg["decodes_fail"]
                prev_empty[mode] = agg["slots_empty"]
                prev_spot_lines[mode] = spot_lines_total

            time.sleep(60.0)

    def _main_loop(self) -> None:
        """Block until signalled, petting the watchdog periodically."""
        watchdog_usec = os.environ.get("WATCHDOG_USEC")
        pet_interval = (
            int(watchdog_usec) / 1_000_000 / 2
            if watchdog_usec else 30.0
        )

        while self._running:
            time.sleep(min(pet_interval, 5.0))
            self._pet_watchdog()

    def _pet_watchdog(self) -> None:
        try:
            addr = os.environ.get("NOTIFY_SOCKET")
            if addr:
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    if addr.startswith("@"):
                        addr = "\0" + addr[1:]
                    sock.connect(addr)
                    sock.sendall(b"WATCHDOG=1")
                finally:
                    sock.close()
        except Exception:
            pass

    def _on_signal(self, signum, frame) -> None:
        logger.info("Received signal %d, shutting down", signum)
        self._running = False

    def _shutdown(self) -> None:
        logger.info("Shutting down...")
        for uploader in self._uploaders:
            uploader.stop()
        for tailer in self._ch_tailers:
            try:
                tailer.stop()
            except Exception:
                logger.exception("Error stopping ch_tailer")
        for multi in self._multi_streams:
            try:
                multi.stop()
            except Exception:
                logger.exception("Error stopping MultiStream")
        for sink in self._sinks:
            sink.stop()
        for fd in self._log_fds.values():
            try:
                fd.close()
            except Exception:
                pass
        if hasattr(self, "_control"):
            try:
                self._control.close()
            except Exception:
                pass
        logger.info("Shutdown complete")


def _resolve_encoding(enc_str: str) -> int:
    """Map config encoding string to ka9q.Encoding integer."""
    mapping = {
        "s16be": 2,
        "s16le": 1,
        "f32": 4,
        "f32le": 4,
        "f32be": 8,
    }
    return mapping.get(enc_str.lower(), 2)
