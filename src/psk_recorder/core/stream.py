"""ChannelSink: per-channel Ring + SlotWorker driven by MultiStream callbacks.

One ChannelSink per (mode, frequency). The sink owns no socket and no
thread of its own for RTP reception — it receives sample batches via
the `on_samples` callback that a shared `MultiStream` dispatches after
demultiplexing by SSRC.

Timing anchoring (rtp_to_wallclock authority — 2026-05-10).
We timestamp each delivered batch via ka9q.rtp_to_wallclock(rtp_ts,
channel_info), which converts the RTP timestamp of the batch's first
sample to UTC using radiod's GPS_TIME / RTP_TIMESNAP snapshot.  This
is the canonical source of truth for sample wall-clock — radiod
captures the GPS-disciplined time once and we extrapolate from RTP
sample positions, completely independent of:

  - this host's system clock (chrony, NTP, GPSDO)
  - hf-timestd's authority offset (now redundant for our anchor)
  - startup-time accumulator state in MultiStream / the resequencer

Previous design anchored via `time.time() - samples_so_far/rate` at
the first on_samples call.  When the channel had been live in radiod
before psk-recorder subscribed (the normal case after a process
restart on a long-running radiod), `samples_so_far` already reflected
hundreds of seconds of MultiStream-side accounting (resequencer
startup batching, gap fills) so the wall-clock anchor was off by
that span.  Effect: every WAV slot's labelled UTC was systematically
EARLY by ~2-3 seconds, decoders reported FT8 DT clustering at +2.79s
mean (B4-100 2026-05-10), and 73.7% of decodes were outside the
nominal ±2.5s slot tolerance — most were not decoded at all.

rtp_to_wallclock applies hf-timestd's chain_delay_correction_ns
(measured by the L6 BPSK PPS calibrator, attached to the channel
info on discovery) automatically, so the recorder still benefits
from the RF→ADC→DSP latency calibration without managing it here.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np

from psk_recorder.config import FT4_CADENCE_SEC, FT8_CADENCE_SEC
from psk_recorder.core.authority_reader import AuthorityReader
from psk_recorder.core.ring import Ring
from psk_recorder.core.slot import SlotWorker

logger = logging.getLogger(__name__)

RING_SECONDS = 60.0


class ChannelSink:
    """Ring + SlotWorker for one channel, fed by MultiStream callbacks."""

    def __init__(
        self,
        mode: str,
        frequency_hz: int,
        sample_rate: int,
        preset: str,
        encoding: int,
        spool_dir: Path,
        log_fd,
        decoder_path: str,
        keep_wav: bool = False,
        authority_reader: Optional[AuthorityReader] = None,
        decoder_kind: str = "jt9",
        decoder_depth: int = 3,
    ):
        self._mode = mode
        self._frequency_hz = frequency_hz
        self._sample_rate = sample_rate
        self._preset = preset
        self._encoding = encoding

        cadence = FT4_CADENCE_SEC if mode == "ft4" else FT8_CADENCE_SEC

        self._ring = Ring(
            max_seconds=RING_SECONDS,
            sample_rate=sample_rate,
        )

        self._slot_worker = SlotWorker(
            ring=self._ring,
            mode=mode,
            frequency_hz=frequency_hz,
            cadence_sec=cadence,
            spool_dir=spool_dir / mode,
            log_fd=log_fd,
            decoder_path=decoder_path,
            decoder_kind=decoder_kind,
            decoder_depth=decoder_depth,
            keep_wav=keep_wav,
        )

        self._total_delivered: int = 0
        # ChannelInfo carrying gps_time / rtp_timesnap / chain_delay
        # — required for ka9q.rtp_to_wallclock(); set by the recorder via
        # set_channel_info() right after multi.add_channel() returns.
        self._channel_info = None
        # Whether we've already logged the "no channel_info, falling back"
        # warning — keeps the log noise to one line per channel even when
        # rtp_to_wallclock can't be used.
        self._fallback_warned: bool = False
        # Authority reader is retained so other consumers (e.g., diags)
        # can still inspect what hf-timestd is publishing.  Not used for
        # anchoring anymore — rtp_to_wallclock is the source of truth.
        self._reader = authority_reader if authority_reader is not None else AuthorityReader()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def frequency_hz(self) -> int:
        return self._frequency_hz

    def stats_snapshot(self) -> dict:
        sw = self._slot_worker
        return {
            "mode": self._mode,
            "freq": self._frequency_hz,
            "decodes_ok": sw.decodes_ok,
            "decodes_fail": sw.decodes_fail,
            "slots_empty": sw.slots_empty,
        }

    def start(self) -> None:
        self._slot_worker.start()
        logger.info(
            "%s %d Hz: sink started (sr=%d)",
            self._mode.upper(), self._frequency_hz, self._sample_rate,
        )

    def stop(self) -> None:
        self._slot_worker.stop()
        logger.info(
            "%s %d Hz: sink stopped (total_delivered=%d)",
            self._mode.upper(), self._frequency_hz, self._total_delivered,
        )

    def set_channel_info(self, channel_info) -> None:
        """Attach the ChannelInfo carrying gps_time/rtp_timesnap/chain_delay.

        Called by the recorder right after multi.add_channel() returns.
        Without it, on_samples falls back to wall-clock anchoring (the
        old broken path) and logs a one-time warning per channel.
        """
        self._channel_info = channel_info

    def on_samples(self, samples: np.ndarray, quality) -> None:
        """MultiStream callback — timestamp via rtp_to_wallclock, push to ring.

        For each batch we compute the RTP timestamp of the first sample
        as `first_rtp_timestamp + batch_start_sample` and convert it to
        UTC via ka9q.rtp_to_wallclock().  This ties every sample to
        radiod's GPS-disciplined RTP_TIMESNAP/GPS_TIME pair, independent
        of the host clock and the MultiStream-side accumulator state.
        """
        n = len(samples)
        if n == 0:
            return

        utc_of_first: Optional[float] = None
        if self._channel_info is not None:
            utc_of_first = self._utc_from_rtp(samples, quality)

        if utc_of_first is None:
            # Fallback path — the channel's GPS_TIME/RTP_TIMESNAP wasn't
            # populated (very old radiod, or discovery hasn't snapped yet).
            # Use the prior wall-clock anchor logic just enough to keep
            # going; warn once.
            utc_of_first = self._fallback_wallclock_utc(quality, n)
            if not self._fallback_warned:
                self._fallback_warned = True
                logger.warning(
                    "%s %d Hz: rtp_to_wallclock unavailable (channel_info=%s, "
                    "gps_time=%s) — using wall-clock fallback; FT8 DT may drift",
                    self._mode.upper(), self._frequency_hz,
                    "set" if self._channel_info is not None else "unset",
                    getattr(self._channel_info, "gps_time", None)
                    if self._channel_info else None,
                )

        self._ring.push(samples, utc_of_first)
        self._total_delivered = quality.total_samples_delivered

    def _utc_from_rtp(self, samples: np.ndarray, quality) -> Optional[float]:
        """Compute UTC of first sample in batch via rtp_to_wallclock.

        RTP timestamps increment by 1 per output sample, so the first
        sample of this batch has RTP TS = first_rtp_timestamp +
        batch_start_sample (mod 2**32).  rtp_to_wallclock then converts
        that to UTC seconds using radiod's GPS_TIME/RTP_TIMESNAP snapshot.
        """
        from ka9q import rtp_to_wallclock

        first_rtp = getattr(quality, "first_rtp_timestamp", 0)
        if first_rtp == 0:
            return None  # not yet populated by MultiStream
        batch_start_sample = quality.total_samples_delivered - len(samples)
        rtp_ts = (first_rtp + batch_start_sample) & 0xFFFFFFFF
        return rtp_to_wallclock(rtp_ts, self._channel_info)

    def _fallback_wallclock_utc(self, quality, n: int) -> float:
        """Best-effort UTC when rtp_to_wallclock can't run.

        Anchors at first call, extrapolates from sample count thereafter.
        Inherits all the host-clock-vs-radiod-counter drift the rtp path
        was meant to eliminate, but at least the WAV files keep flowing.
        """
        if not hasattr(self, "_fb_anchor_utc"):
            self._fb_anchor_utc = time.time()
            self._fb_anchor_total = quality.total_samples_delivered
        delta_samples = quality.total_samples_delivered - n - self._fb_anchor_total
        return self._fb_anchor_utc + delta_samples / self._sample_rate

    def on_stream_dropped(self, reason: str) -> None:
        logger.warning(
            "%s %d Hz: stream dropped — %s",
            self._mode.upper(), self._frequency_hz, reason,
        )

    def on_stream_restored(self, channel_info) -> None:
        # MultiStream re-discovers and hands us a fresh ChannelInfo with
        # an updated GPS_TIME/RTP_TIMESNAP snapshot; replace ours so
        # rtp_to_wallclock keeps using a sane reference even if radiod
        # restarted under us.
        self._channel_info = channel_info
        logger.info(
            "%s %d Hz: stream restored",
            self._mode.upper(), self._frequency_hz,
        )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def frequency_hz(self) -> int:
        return self._frequency_hz

    @property
    def preset(self) -> str:
        return self._preset

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def encoding(self) -> int:
        return self._encoding
