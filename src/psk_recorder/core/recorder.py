"""PskRecorder: orchestrates one radiod's FT4/FT8 channels.

One PskRecorder per radiod instance (= one systemd unit).
Creates ChannelStream objects for each frequency, manages log
file descriptors, and supervises HsPskReporterUploaders.
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
from psk_recorder.core.hs_uploader_shim import HsPskReporterUploader

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float, *, scale: float = 1.0) -> float:
    """Parse a positive float env var.  `scale` converts the env-var
    unit to the constant's unit (e.g. 1e-6 for µs→s) and is applied
    consistently to BOTH the env value and the default so the caller
    states `default` in the env-var's natural unit.  Invalid or
    non-positive values fall back to `default * scale` with a warning."""
    raw = os.environ.get(name)
    if raw is None:
        return default * scale
    try:
        v = float(raw) * scale
        if v <= 0:
            raise ValueError("must be > 0")
        return v
    except (ValueError, TypeError):
        logger.warning(
            "psk-recorder: ignoring invalid %s=%r (using default %g)",
            name, raw, default,
        )
        return default * scale


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
        if v < 1:
            raise ValueError("must be >= 1")
        return v
    except (ValueError, TypeError):
        logger.warning(
            "psk-recorder: ignoring invalid %s=%r (using default %d)",
            name, raw, default,
        )
        return default


# PSK_DELIVERY_MODE — how spots reach PSKReporter.
#
#   server (default): client only ships to wsprdaemon servers; the
#                     gw1-elected pskreporter_forwarder service POSTs
#                     to pskreporter.info on the client's behalf.
#                     Spots are tagged forward_to_pskreporter=True in
#                     the local sink → tar → psk.spots.
#   direct          : client POSTs directly to pskreporter.info (today's
#                     behavior). No wsprdaemon-server path: the
#                     ch_tailer that feeds the local SQLite sink is
#                     not started, so nothing ships to wsprdaemon.
#   both            : client POSTs directly AND ships to wsprdaemon.
#                     Spots are tagged forward_to_pskreporter=False so
#                     the server does NOT re-post (avoids duplicates).
_VALID_DELIVERY_MODES = ("server", "direct", "both")


def _resolve_delivery_mode() -> str:
    """Read + validate PSK_DELIVERY_MODE; fall back to 'server' on
    anything bogus. We log the choice once at startup so an operator
    typo doesn't silently disable direct delivery."""
    raw = (os.environ.get("PSK_DELIVERY_MODE") or "server").strip().lower()
    if raw not in _VALID_DELIVERY_MODES:
        logger.warning(
            "PSK_DELIVERY_MODE=%r is not one of %s; defaulting to 'server'",
            raw, _VALID_DELIVERY_MODES,
        )
        return "server"
    return raw


def _sqlite_sink_available() -> bool:
    """True when the SQLite sink the hs-uploader reads from is in play.

    Mirrors `hs_uploader.sources.sqlite._ConnectionConfig.from_env`:
    an explicit `SIGMOND_SQLITE_PATH`, or the default sink file already
    on disk.  When the sink is in play the hs-uploader shim selects
    `SqliteSource`, so the per-slot `.spots.txt` spool files would
    never be consumed — the recorder skips writing them.
    """
    if (os.environ.get("SIGMOND_SQLITE_PATH") or "").strip():
        return True
    return Path("/var/lib/sigmond/sink.db").exists()


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
        # (MultiStream, ssrc) pairs — populated as channels are provisioned,
        # used by the keep-alive thread to refresh radiod's LIFETIME timer.
        self._lifetime_entries: list[tuple[object, int]] = []
        self._uploaders: list[HsPskReporterUploader] = []
        self._ch_tailers: list[ChTailer] = []
        self._log_fds: dict[str, object] = {}
        self._running = False

        # radiod LIFETIME tag (ka9q-python ≥3.13.0).  0 = no LIFETIME tag
        # sent + no keep-alive; >0 = self-destruct after N frames, refreshed
        # at frames/4 cadence while we're alive.  See DEFAULTS in config.py.
        proc = config.get("processing", {})
        self._radiod_lifetime_frames: int = int(
            proc.get("radiod_lifetime_frames", 0)
        )
        self._lifetime_thread: Optional[threading.Thread] = None

    # Settled-capture gate (V1 fix per
    # docs/TIMING-PIPELINE-WIRING.md §6.6 / §10.3).  Block on
    # ensure_channel() until chrony has reported a settled state
    # for SETTLE_REQUIRED_CYCLES consecutive readings, so the
    # per-channel ChannelInfo anchors captured by ka9q-python
    # inherit an ε_0 ≈ 0 system_time.  Without this gate, channels
    # whose SSRCs were created before chrony settled (or before a
    # radiod restart) carry stale anchors and produce slot
    # timestamps wrong by minutes to hours — corrupting psk.spots'
    # UTC field silently.  Verified 2026-05-11.
    #
    # Defaults assume bare-metal hosts with hardware GPS PPS where
    # chrony tracks within tens of µs.  On VMs and hosts with looser
    # discipline, chrony's Last offset may stably sit at 200-500 µs
    # — the 100 µs default would always time out.  Each constant
    # below is overridable via the matching `PSK_SETTLE_*` env var:
    #
    #   PSK_SETTLE_MAX_OFFSET_US     ceiling on |Last offset| (µs).
    #                                Set to e.g. 1000 on a VM.
    #   PSK_SETTLE_REQUIRED_CYCLES   consecutive settled polls before
    #                                we consider chrony stable.
    #   PSK_SETTLE_POLL_SEC          poll interval (s).
    #   PSK_SETTLE_TIMEOUT_SEC       overall wait cap (s) before
    #                                proceeding with degraded anchors.
    #
    # All env reads happen at class-load time (process start), so a
    # restart picks up the new value.  Invalid values fall back to
    # the conservative default and log a warning at gate time.
    # Resolved at module-load time; env overrides apply per process.
    SETTLE_MAX_OFFSET_S = _env_float(
        "PSK_SETTLE_MAX_OFFSET_US", 100.0, scale=1e-6,
    )
    SETTLE_REQUIRED_CYCLES = _env_int(
        "PSK_SETTLE_REQUIRED_CYCLES", 3,
    )
    SETTLE_POLL_SEC = _env_float(
        "PSK_SETTLE_POLL_SEC", 5.0,
    )
    SETTLE_TIMEOUT_SEC = _env_float(
        "PSK_SETTLE_TIMEOUT_SEC", 60.0,
    )

    def run(self) -> None:
        """Main entry: provision channels, start streams, block until signal."""
        self._running = True
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        try:
            # V1 fix layer 1: gate ensure_channel() on chrony being
            # settled.  See docs/TIMING-PIPELINE-WIRING.md §6.6.
            self._wait_for_chrony_settled()
            self._provision_channels()
            self._start_streams()
            self._start_uploaders()
            self._start_ch_tailers()
            self._start_stats_thread()
            self._start_lifetime_keepalive()
            self._notify_ready()
            self._main_loop()
        except Exception:
            logger.exception("Fatal error in recorder")
        finally:
            self._shutdown()

    def _wait_for_chrony_settled(self) -> bool:
        """Block until chrony's Last offset has been below
        ``SETTLE_MAX_OFFSET_S`` for ``SETTLE_REQUIRED_CYCLES``
        consecutive readings.  Returns True if chrony settled within
        the timeout, False if we timed out (degraded mode, logged
        loudly).

        Capturing per-channel anchors when chrony is settled means
        the ChannelInfo's (gps_time, rtp_timesnap) pair inherits an
        ε_0 ≈ 0 system_time.  Sample-clock arithmetic in
        ka9q.rtp_to_wallclock then projects slot start times to
        true UTC ± ε_now (chrony's current discipline error), not
        ε_now − ε_0 with ε_0 frozen at the wrong value.

        Silent no-op when chronyc is unavailable.  See
        docs/TIMING-PIPELINE-WIRING.md §6.6 for the empirical
        evidence and §10.3 for the architectural pattern.
        """
        import subprocess as _sub
        try:
            _sub.run(['chronyc', '-h'], capture_output=True, timeout=2.0)
        except (FileNotFoundError, OSError, _sub.TimeoutExpired):
            logger.warning(
                "psk-recorder settled-capture gate: chronyc unavailable — "
                "channel anchors will be captured without verification "
                "(ε_0 may be non-zero, V1 not prevented; "
                "slot timestamps may be silently wrong)"
            )
            return False

        consecutive = 0
        wait_start = time.monotonic()
        deadline = wait_start + self.SETTLE_TIMEOUT_SEC
        logger.info(
            "psk-recorder settled-capture gate: waiting for chrony "
            "(threshold |Last offset| <= %.0f µs, need %d consecutive readings, "
            "timeout %.0fs)",
            self.SETTLE_MAX_OFFSET_S * 1e6,
            self.SETTLE_REQUIRED_CYCLES,
            self.SETTLE_TIMEOUT_SEC,
        )
        while time.monotonic() < deadline:
            try:
                proc = _sub.run(
                    ['chronyc', '-n', 'tracking'],
                    capture_output=True, text=True, timeout=5.0,
                )
            except (_sub.TimeoutExpired, OSError) as exc:
                logger.debug("psk-recorder settled-capture: chronyc failed: %s", exc)
                time.sleep(self.SETTLE_POLL_SEC)
                consecutive = 0
                continue
            if proc.returncode != 0:
                time.sleep(self.SETTLE_POLL_SEC)
                consecutive = 0
                continue

            last_offset = self._parse_chronyc_last_offset(proc.stdout)
            if last_offset is None:
                logger.debug(
                    "psk-recorder settled-capture: could not parse "
                    "Last offset from chronyc tracking output"
                )
                time.sleep(self.SETTLE_POLL_SEC)
                consecutive = 0
                continue

            if abs(last_offset) <= self.SETTLE_MAX_OFFSET_S:
                consecutive += 1
                logger.info(
                    "psk-recorder settled-capture: chrony Last offset "
                    "%+.1f µs OK (%d/%d)",
                    last_offset * 1e6,
                    consecutive,
                    self.SETTLE_REQUIRED_CYCLES,
                )
                if consecutive >= self.SETTLE_REQUIRED_CYCLES:
                    elapsed = time.monotonic() - wait_start
                    logger.info(
                        "psk-recorder settled-capture: chrony settled after "
                        "%.1fs — proceeding to provision channels", elapsed,
                    )
                    return True
            else:
                if consecutive > 0:
                    logger.info(
                        "psk-recorder settled-capture: chrony Last offset "
                        "%+.1f µs > threshold; resetting counter",
                        last_offset * 1e6,
                    )
                consecutive = 0
            time.sleep(self.SETTLE_POLL_SEC)

        logger.warning(
            "psk-recorder settled-capture: timeout after %.0fs — "
            "proceeding with degraded anchors (slot timestamps may "
            "be wrong on some channels; visible as future-dated "
            "WAV filenames per docs/TIMING-PIPELINE-WIRING.md §6.6)",
            self.SETTLE_TIMEOUT_SEC,
        )
        return False

    @staticmethod
    def _parse_chronyc_last_offset(text: str) -> Optional[float]:
        """Parse `chronyc tracking`'s ``Last offset`` line.

        Returns the offset in seconds (float), or None if unparseable.
        Matches the parser in hf-timestd's CoreRecorderV2.
        """
        for line in (text or '').splitlines():
            s = line.strip()
            if s.startswith('Last offset'):
                _, _, val = s.partition(':')
                val = val.strip()
                if not val:
                    return None
                token = val.split()[0]
                try:
                    return float(token)
                except ValueError:
                    return None
        return None

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
        # client_id makes ka9q-python derive a per-(client, radiod)
        # multicast destination so this recorder's channels never share
        # a multicast group with peer clients on the same radiod
        # (wspr-recorder, hfdl-recorder, hf-timestd, etc.).  CONTRACT
        # v0.3 §7 / ka9q-python ≥ 3.14.0.
        self._control = RadiodControl(status, client_id="psk-recorder")

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
        # CONTRACT v0.6 — `decoder_kind` selects the decoder backend.
        # Only ka9q/ft8_lib's `decode_ft8` is supported.  The path
        # falls back from `paths.decoder_decode_ft8` to `paths.decoder`
        # for older configs.  See SlotWorker for output-format details.
        decoder_kind = str(self._paths.get("decoder_kind", "decode_ft8")).lower()
        decoder = self._paths.get(
            "decoder_decode_ft8", self._paths.get(
                "decoder", "/usr/local/bin/decode_ft8",
            ),
        )
        decoder_depth = int(self._paths.get("decoder_depth", 3))
        keep_wav = self._paths.get("keep_wav", False)
        # Tee per-slot decoder output into <wav>.spots.txt files only
        # when the hs-uploader is on AND there is no SQLite sink for it
        # to read — that's the file-fallback mode the shim's
        # FileTreeSource picks up.  With a sink present the shim reads
        # SqliteSource and the spool files would only pile up
        # unconsumed; with the legacy uploader in use they're unused
        # either way.  Skipping the write keeps the spool dir clean.
        spool_spots = bool(
            os.environ.get("PSK_USE_HS_UPLOADER")
            and not _sqlite_sink_available()
        )
        logger.info(
            "decoder_kind=%s path=%s depth=%d spool_spots=%s",
            decoder_kind, decoder, decoder_depth, spool_spots,
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
                # Text mode (not binary) — the decoder-output writers
                # feed Python str through this fd.  Opening in "ab"
                # raised TypeError("a bytes-like object is required, not
                # 'str'") and silently dropped every decode on the floor.
                self._log_fds[mode] = open(log_path, "a", encoding="utf-8")

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
                    spool_spots=spool_spots,
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

        # `lifetime=None` when configured to 0 — distinguishes "no LIFETIME
        # tag at all" (radiod template default applies) from "finite N
        # frames" (self-destruct timer enabled).
        lifetime_arg: Optional[int] = (
            self._radiod_lifetime_frames
            if self._radiod_lifetime_frames > 0 else None
        )

        info = self._control.ensure_channel(
            frequency_hz=float(sink.frequency_hz),
            preset=sink.preset,
            sample_rate=sink.sample_rate,
            encoding=sink.encoding,
            lifetime=lifetime_arg,
        )
        key = (info.multicast_address, info.port)
        multi = multi_by_group.get(key)
        if multi is None:
            multi = MultiStream(control=self._control)
            multi_by_group[key] = multi

        ch_info = multi.add_channel(
            frequency_hz=float(sink.frequency_hz),
            preset=sink.preset,
            sample_rate=sink.sample_rate,
            encoding=sink.encoding,
            on_samples=sink.on_samples,
            on_stream_dropped=sink.on_stream_dropped,
            on_stream_restored=sink.on_stream_restored,
            lifetime=lifetime_arg,
        )
        # Hand the freshly-discovered ChannelInfo (with GPS_TIME /
        # RTP_TIMESNAP / chain_delay_correction populated) to the sink
        # so on_samples can use rtp_to_wallclock as the UTC source.
        sink.set_channel_info(ch_info)
        if lifetime_arg is not None:
            self._lifetime_entries.append((multi, ch_info.ssrc))

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
        # Spot uploader: a single thread feeds psk.spots rows upstream
        # to PSKReporter via the hs-uploader-driven `HsPskReporterUploader`
        # (Pipeline + PskReporterTcp transport).  It reads SqliteSource
        # when sigmond's SQLite sink is present, else FileTreeSource over
        # the per-slot spool.
        mode = _resolve_delivery_mode()
        if mode == "server":
            logger.info(
                "PSK_DELIVERY_MODE=server: direct PSKReporter uploader "
                "disabled; spots will reach PSKReporter via wsprdaemon "
                "server forwarding"
            )
            return
        callsign = self._station.get("callsign", "")
        grid = self._station.get("grid_square", "")
        if not callsign:
            logger.warning("No callsign configured — pskreporter will not start")
            return
        antenna = self._station.get("antenna", "")
        # Default to TCP (delivery-confirmed, no silent drops under load).
        # Operators on constrained links can opt out via config.
        use_tcp = bool(self._paths.get("pskreporter_tcp", True))

        spool_dir = Path(self._paths.get(
            "spool_dir", "/var/lib/psk-recorder",
        )) / self._radiod_id
        uploader = HsPskReporterUploader(
            callsign=callsign,
            grid_square=grid,
            antenna=antenna,
            radiod_id=self._radiod_id,
            use_tcp=use_tcp,
            spool_dir=spool_dir,
        )
        logger.info("uploader: %s", type(uploader).__name__)
        uploader.start()
        self._uploaders.append(uploader)

    def _start_ch_tailers(self) -> None:
        """Start one ChTailer per (radiod, mode) — CONTRACT v0.6 §17.

        Each tailer watches the same `<radiod_id>-<mode>.log` file
        pskreporter-sender tails, parses new lines, and inserts rows
        into `psk.spots` via `sigmond.hamsci_sink.Writer.from_env()` —
        sigmond's local SQLite sink by default.  Failure to import /
        start is non-fatal: the existing PSKReporter upload path is
        unaffected.

        ChTailer runs in ALL three PSK_DELIVERY_MODE values.  The mode
        affects only the per-row ``forward_to_pskreporter`` flag, NOT
        whether tailers run:

          * ``server`` → forward=True   (server forwards on our behalf)
          * ``direct`` → forward=False  (we POST directly; server must
                          not double-post if it ever sees the row)
          * ``both``   → forward=False  (we POST directly AND ship to
                          wd as a redundant copy; server won't re-post)

        Originally PR 3 disabled tailers in ``direct`` mode under the
        theory that "direct = no wsprdaemon path".  That broke the
        direct PSKReporter delivery — the HsPskReporterUploader's
        SqliteSource reads from the SAME sink the tailer writes to, so
        with the tailer off the uploader pumped every 30s, found zero
        work, and silently delivered nothing.  Discovered during the
        2026-05-18 B4-100 cutover when ~2h of decodes failed to reach
        PSKReporter.  See feedback_psk_delivery_mode_direct_breaks_uploader.
        """
        mode = _resolve_delivery_mode()
        forward_flag = (mode == "server")  # direct / both → False
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

        for tailer_mode in ("ft8", "ft4"):
            if not get_freqs(self._radiod, tailer_mode):
                continue
            log_path = log_dir / f"{self._radiod_id}-{tailer_mode}.log"
            try:
                tailer = ChTailer(
                    log_path=log_path,
                    mode=tailer_mode,
                    radiod_id=self._radiod_id,
                    host_call=callsign,
                    host_grid=grid,
                    processing_version=proc_version,
                    callhash_path=callhash_path,
                    forward_to_pskreporter=forward_flag,
                )
                tailer.start()
                self._ch_tailers.append(tailer)
            except Exception:
                logger.exception(
                    "ch_tailer %s startup failed; PSKReporter path unaffected",
                    tailer_mode,
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

    def _start_lifetime_keepalive(self) -> None:
        """Refresh radiod's LIFETIME on every active SSRC at frames/4 cadence.

        No-op when radiod_lifetime_frames is 0 or no channels opted in.
        Failure to refresh (network blip, radiod restart) must not crash
        the recorder — log and continue; MultiStream's drop/restore path
        will re-apply the slot's lifetime when reception resumes.
        """
        if not self._lifetime_entries:
            return
        # Refresh every quarter of the lifetime — gives 4× safety margin
        # against radiod self-destruct if a single refresh is missed.
        # Floor at 1 s so absurd configs don't busy-loop.
        interval = max(self._radiod_lifetime_frames / 50.0 / 4.0, 1.0)
        logger.info(
            "lifetime keepalive: %d channels, %d frames, refresh every %.1fs",
            len(self._lifetime_entries),
            self._radiod_lifetime_frames,
            interval,
        )
        self._lifetime_thread = threading.Thread(
            target=self._lifetime_loop,
            args=(interval,),
            daemon=True,
            name="lifetime",
        )
        self._lifetime_thread.start()

    def _lifetime_loop(self, interval_sec: float) -> None:
        while self._running:
            time.sleep(interval_sec)
            if not self._running:
                break
            for multi, ssrc in self._lifetime_entries:
                try:
                    multi.set_channel_lifetime(
                        ssrc, self._radiod_lifetime_frames
                    )
                except Exception as exc:
                    logger.warning(
                        "lifetime keepalive failed (ssrc=%s): %s", ssrc, exc,
                    )

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
