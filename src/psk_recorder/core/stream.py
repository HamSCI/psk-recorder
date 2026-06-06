"""ChannelSink: per-channel Ring + SlotWorker driven by MultiStream callbacks.

One ChannelSink per (mode, frequency). The sink owns no socket and no
thread of its own for RTP reception — it receives sample batches via
the `on_samples` callback that a shared `MultiStream` dispatches after
demultiplexing by SSRC.

Timing model (anchor-once, sample-count projection — 2026-05-13).

  1. On the FIRST on_samples batch, we read one wall-clock anchor and
     save the corresponding total_samples_delivered counter.  Preferred
     source: ka9q.rtp_to_wallclock(first_rtp, channel_info), which gives
     us radiod's GPS_TIME / RTP_TIMESNAP snapshot — and hf-timestd's
     chain_delay_correction_ns if hf-timestd is publishing.  Fallback
     when channel_info is unavailable: `time.time()` minus n/sample_rate.

  2. Every subsequent batch's UTC is computed by pure sample-count
     projection from the anchor:
         utc_of_first = anchor_utc + (total_delivered - n - anchor_total) / sr
     No wall-clock re-reads. No channel_info refreshes.  This mirrors
     the wspr-recorder BandRecorder design (grid-propagated minute
     wallclocks via `first_wallclock + 60 * minute_count`) and is what
     guarantees every WAV / slot has exactly the same sample count
     and identical alignment to its predecessors.

Previous design (rtp_to_wallclock per batch, with channel_info refresh
on stream-restored) re-anchored the timing whenever a multicast gap
recovered.  Each refresh adopted radiod's then-current view of UTC,
introducing a wall-clock dependency that occasionally cascaded into
"decodes=N/N but spots=0" states (B4-100, 2026-05-13: PSK silently
fell from 275 spots/min to 0 over a few minutes; stop+start was the
only known cure).  The anchor-once model removes the silent-degradation
surface entirely.

Per METROLOGY.md §4.5 RTP-reference invariant, the recorder does not
diagnose timing health on its own — that is hf-timestd's job.  If the
host clock is badly wrong at anchor time, decode rate goes to zero and
the operator sees the symptom through the standard decode-health
signal (psk decodes_ok/decodes_total + sigmond's wav_snapshot), not
through any client-side wall-clock comparison.
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
        decoder_kind: str = "decode_ft8",
        spool_spots: bool = False,
        radiod_id: str = "",
        fault_reporter=None,
        fault_threshold_sec: float = 0.25,
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
            keep_wav=keep_wav,
            spool_spots=spool_spots,
        )

        self._total_delivered: int = 0
        # ChannelInfo carrying gps_time / rtp_timesnap / chain_delay
        # — used ONCE to derive the initial anchor via rtp_to_wallclock.
        # After the first batch the anchor is frozen; channel_info refreshes
        # from on_stream_restored() are deliberately ignored.
        self._channel_info = None
        # Anchor-once state (see module docstring).  Set on the first
        # on_samples() batch; never re-set afterwards.
        self._anchor_utc: Optional[float] = None
        self._anchor_total_samples: int = 0
        self._anchor_source: str = ""        # diagnostic: "rtp_to_wallclock" | "wallclock_fallback"
        # Authority reader is retained so other consumers (e.g., diags)
        # can still inspect what hf-timestd is publishing.  Not used for
        # anchoring anymore — the one-shot anchor read is the only path.
        self._reader = authority_reader if authority_reader is not None else AuthorityReader()
        # Timing-fault detector state (see _maybe_check_timing).  A
        # StatusListener keeps self._channel_info's (gps_time, rtp_timesnap)
        # fresh, so re-anchoring lands on radiod's current GPS reference.
        self._radiod_id = radiod_id
        self._fault_reporter = fault_reporter
        self._fault_threshold_sec = fault_threshold_sec
        self._last_check_mono = 0.0
        self._last_reanchor_mono = 0.0
        self._reanchor_cooldown_sec = 30.0

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
        """MultiStream callback — anchor once, then sample-count project.

        First batch sets `_anchor_utc` + `_anchor_total_samples` via
        rtp_to_wallclock (preferred) or `time.time()` (fallback).  Every
        subsequent batch's UTC is computed by pure sample-count
        projection — no wall-clock reads, no channel_info refreshes.
        """
        n = len(samples)
        if n == 0:
            return

        if self._anchor_utc is None:
            anchor, source = self._compute_initial_anchor(samples, quality)
            if anchor is None:
                # rtp_to_wallclock failed AND time.time() somehow returned
                # something useless — extremely rare; defer to the next
                # batch rather than push samples with a bogus UTC.
                return
            self._anchor_utc = anchor
            self._anchor_total_samples = quality.total_samples_delivered - n
            self._anchor_source = source
            logger.info(
                "%s %d Hz: anchored via %s at UTC %.3f (sample %d)",
                self._mode.upper(), self._frequency_hz, source,
                self._anchor_utc, self._anchor_total_samples,
            )

        # Pure sample-count projection — the timing invariant the user
        # asked for (and the same model wspr-recorder's BandRecorder uses).
        delta_samples = (
            quality.total_samples_delivered - n - self._anchor_total_samples
        )
        utc_of_first = (
            self._anchor_utc + delta_samples / self._sample_rate
        )

        # Detect → alarm → re-anchor (operator principle 2026-06-05):
        # compare this projection against radiod's fresh GPS reference for
        # the same RTP value; on a gross divergence raise a loud fault AND
        # drop the anchor so the next batch re-anchors off the fresh status
        # snapshot, recovering decode timing instead of drifting silently.
        self._maybe_check_timing(quality, n, utc_of_first)

        self._ring.push(samples, utc_of_first)
        self._total_delivered = quality.total_samples_delivered

    def _maybe_check_timing(self, quality, n, projected_utc) -> None:
        """Compare the sample-count projection against radiod's fresh GPS
        reference (rtp_to_wallclock on the StatusListener-refreshed
        channel_info) for the same RTP value.  Throttled to ~1 Hz.  On a
        divergence past ``_fault_threshold_sec`` raise a loud fault and
        re-anchor; a cooldown stops one fault re-anchoring every batch."""
        if self._fault_reporter is None or self._channel_info is None:
            return
        mono = time.monotonic()
        if mono - self._last_check_mono < 1.0:
            return
        self._last_check_mono = mono
        first_rtp = getattr(quality, "first_rtp_timestamp", 0)
        if not first_rtp:
            return
        try:
            from ka9q import rtp_to_wallclock
            batch_start_sample = quality.total_samples_delivered - n
            rtp_ts = (first_rtp + batch_start_sample) & 0xFFFFFFFF
            reference = rtp_to_wallclock(
                rtp_ts, self._channel_info, wallclock_hint_sec=projected_utc,
            )
        except Exception as e:  # noqa: BLE001 — detection must not crash audio
            logger.debug("%s %d Hz: timing check raised: %s",
                         self._mode.upper(), self._frequency_hz, e)
            return
        if reference is None:
            return
        divergence = projected_utc - reference
        if abs(divergence) <= self._fault_threshold_sec:
            return
        if mono - self._last_reanchor_mono < self._reanchor_cooldown_sec:
            return
        self._last_reanchor_mono = mono
        self._fault_reporter.report(self._mode, self._frequency_hz, divergence)
        # Re-anchor: clear the frozen anchor; the next batch recomputes it
        # from the fresh (StatusListener-refreshed) channel_info.
        self._anchor_utc = None

    def _compute_initial_anchor(self, samples: np.ndarray, quality):
        """Return (anchor_utc, source) for the very first batch.

        Preferred: rtp_to_wallclock — includes hf-timestd's chain delay
        correction when authority is published.  Fallback: time.time()
        adjusted to "UTC of first sample in this batch".  Either way,
        this is the ONLY wall-clock read for the recorder's lifetime.
        """
        n = len(samples)
        if self._channel_info is not None:
            try:
                from ka9q import rtp_to_wallclock
                first_rtp = getattr(quality, "first_rtp_timestamp", 0)
                if first_rtp != 0:
                    batch_start_sample = quality.total_samples_delivered - n
                    rtp_ts = (first_rtp + batch_start_sample) & 0xFFFFFFFF
                    utc = rtp_to_wallclock(rtp_ts, self._channel_info)
                    if utc is not None:
                        return utc, "rtp_to_wallclock"
            except Exception as exc:                # noqa: BLE001
                logger.warning(
                    "%s %d Hz: rtp_to_wallclock raised on anchor: %s",
                    self._mode.upper(), self._frequency_hz, exc,
                )
        # Wall-clock fallback: time.time() *is* the UTC of "right now",
        # so the UTC of this batch's FIRST sample is (now - n/sample_rate).
        return time.time() - n / self._sample_rate, "wallclock_fallback"

    def on_stream_dropped(self, reason: str) -> None:
        logger.warning(
            "%s %d Hz: stream dropped — %s",
            self._mode.upper(), self._frequency_hz, reason,
        )

    def on_stream_restored(self, channel_info) -> None:
        # Re-anchor on stream restoration.  MultiStream only fires this
        # callback after _drop_timeout_sec (default 15s) of silence AND
        # a successful ensure_channel() — i.e. a real radiod restart or
        # comparable outage, never a sub-second multicast hiccup.  On
        # such a restart, MultiStream resets ``slot.quality =
        # StreamQuality()``, so ``quality.total_samples_delivered``
        # restarts at 0.  Holding the pre-restart anchor across that
        # discontinuity produces wildly negative ``delta_samples`` in
        # on_samples(), every projected UTC misses every slot window,
        # and decodes silently fall to 0/0 forever (observed B4-100
        # 2026-05-14: radiod bounced at 20:13/20:14, every band silent
        # for 3 h until manual stop+start).
        #
        # The original comment here cited "mistuning after a multicast
        # hiccup", but MultiStream's 15s threshold makes that scenario
        # impossible to reach via this callback — transient packet loss
        # never fires on_stream_restored.  Re-anchoring is the
        # intended behavior.
        self._channel_info = channel_info
        self._anchor_utc = None
        self._anchor_total_samples = 0
        self._anchor_source = ""
        logger.info(
            "%s %d Hz: stream restored — re-anchoring on next batch",
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
