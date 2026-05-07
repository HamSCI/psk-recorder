"""ChannelSink: per-channel Ring + SlotWorker driven by MultiStream callbacks.

One ChannelSink per (mode, frequency). The sink owns no socket and no
thread of its own for RTP reception — it receives sample batches via
the `on_samples` callback that a shared `MultiStream` dispatches after
demultiplexing by SSRC.

Timing anchoring.  Under the RTP-reference labeling invariant
(hf-timestd/docs/METROLOGY.md §4.5.1), psk-recorder anchors the UTC of
each sample using radiod's RTP counter plus an optional
rtp_to_utc_offset_ns from /run/hf-timestd/authority.json.  When
authority.json is unavailable (standalone mode, no hf-timestd), we
fall back to a one-time `time.time()` snapshot — the recorder still
works, but the anchor inherits whatever error the system clock carries
at that instant (the Saturday 2026-04-20 failure mode).  We refresh
the authority-driven correction opportunistically (every ~60 s of
samples) so large offset transitions (e.g., hf-timestd bootstrap
completing, T3 Fusion locking) are picked up mid-run rather than
requiring a recorder restart.
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

# Opportunistic authority refresh interval, measured in samples. Every
# ~60 seconds of delivered samples we re-read authority.json and
# rebase the stream epoch if the offset has drifted meaningfully.
AUTHORITY_REFRESH_EVERY_SAMPLES = 60 * 12_000  # 60 s at 12 kHz

# Re-anchor only when the authority-reported offset has drifted by
# more than this from the offset used at the last anchor. Smaller
# drifts are absorbed by the FT8/FT4 decoder tolerance and not worth
# the log noise of a rebase.
AUTHORITY_REANCHOR_THRESHOLD_SEC = 0.050  # 50 ms


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

        self._stream_start_epoch: Optional[float] = None
        self._total_delivered: int = 0

        # Authority-driven UTC anchoring (§4.5.1). A None reader disables
        # the mechanism entirely and the sink reverts to the legacy
        # wall-clock anchor — useful only for tests and the explicit
        # "force standalone" operator toggle.
        self._reader = authority_reader if authority_reader is not None else AuthorityReader()
        self._anchor_source: Optional[str] = None       # "authority" | "wall_clock"
        self._anchor_offset_sec: Optional[float] = None  # applied rtp_to_utc offset, in seconds
        self._samples_since_refresh: int = 0

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

    def on_samples(self, samples: np.ndarray, quality) -> None:
        """MultiStream callback — push samples into the ring.

        First call anchors `_stream_start_epoch`; subsequent calls
        extrapolate UTC from the delivered-sample counter. Every
        AUTHORITY_REFRESH_EVERY_SAMPLES we re-read authority.json so a
        large offset transition (e.g., hf-timestd bootstrap completing)
        can be absorbed mid-run.
        """
        n = len(samples)
        if n == 0:
            return

        if self._stream_start_epoch is None:
            self._anchor_initial(quality)
        else:
            self._samples_since_refresh += n
            if self._samples_since_refresh >= AUTHORITY_REFRESH_EVERY_SAMPLES:
                self._samples_since_refresh = 0
                self._refresh_authority_offset()

        batch_start_sample = quality.total_samples_delivered - n
        utc_of_first = self._stream_start_epoch + (
            batch_start_sample / self._sample_rate
        )

        self._ring.push(samples, utc_of_first)
        self._total_delivered = quality.total_samples_delivered

    # ---- authority anchoring -----------------------------------------

    def _anchor_initial(self, quality) -> None:
        """Compute `_stream_start_epoch` from the best available source.

        Preference order:
          1. authority.json with a usable offset → system_clock + offset
             is treated as UTC, so the anchor is UTC-accurate up to
             host-clock skew between radiod and the authority host.
          2. system clock alone → the legacy path; recorder still works
             but inherits whatever error the local clock carries.
        """
        now = time.time()
        samples_so_far = quality.total_samples_delivered
        offset_sec = self._read_current_offset()
        corrected_now = now + (offset_sec or 0.0)
        self._stream_start_epoch = corrected_now - (samples_so_far / self._sample_rate)

        if offset_sec is not None:
            self._anchor_source = "authority"
            self._anchor_offset_sec = offset_sec
            logger.info(
                "%s %d Hz: first samples received (samples_so_far=%d); anchored "
                "via hf-timestd authority (offset=%+.3f s)",
                self._mode.upper(), self._frequency_hz, samples_so_far, offset_sec,
            )
        else:
            self._anchor_source = "wall_clock"
            self._anchor_offset_sec = None
            logger.warning(
                "%s %d Hz: first samples received (samples_so_far=%d); anchored "
                "via wall clock (hf-timestd authority unavailable — standalone "
                "mode; WAV timestamps rely on this host's clock discipline)",
                self._mode.upper(), self._frequency_hz, samples_so_far,
            )

    def _read_current_offset(self) -> Optional[float]:
        """Read authority.json, return offset in seconds if usable."""
        try:
            snap = self._reader.read()
        except Exception as e:
            logger.debug("authority reader raised: %s", e)
            return None
        if snap is None or not snap.offset_usable:
            return None
        return snap.offset_seconds

    def _refresh_authority_offset(self) -> None:
        """Re-read authority.json and rebase the stream epoch if the
        authority offset has drifted enough to matter. No-op when the
        reader is disabled, authority.json is still unavailable, or the
        drift is below the re-anchor threshold."""
        offset_sec = self._read_current_offset()
        if offset_sec is None:
            # Authority disappeared or became unusable. Keep the existing
            # anchor — losing hf-timestd mid-run does not invalidate the
            # RTP counter we've been tracking; it just means future drift
            # correction is unavailable.
            return

        if self._anchor_source != "authority" or self._anchor_offset_sec is None:
            # We were anchored via wall clock (or uninitialized); authority
            # is now available. Rebase the epoch by the full offset.
            assert self._stream_start_epoch is not None
            self._stream_start_epoch += offset_sec
            logger.info(
                "%s %d Hz: authority became available mid-run; rebased epoch "
                "by %+.3f s",
                self._mode.upper(), self._frequency_hz, offset_sec,
            )
            self._anchor_source = "authority"
            self._anchor_offset_sec = offset_sec
            return

        drift = offset_sec - self._anchor_offset_sec
        if abs(drift) < AUTHORITY_REANCHOR_THRESHOLD_SEC:
            return

        assert self._stream_start_epoch is not None
        self._stream_start_epoch += drift
        logger.info(
            "%s %d Hz: authority offset drifted by %+.3f s; rebased epoch",
            self._mode.upper(), self._frequency_hz, drift,
        )
        self._anchor_offset_sec = offset_sec

    @property
    def anchor_source(self) -> Optional[str]:
        """'authority' | 'wall_clock' | None (not yet anchored). For
        operator diagnostics and tests."""
        return self._anchor_source

    @property
    def anchor_offset_sec(self) -> Optional[float]:
        """Applied rtp_to_utc offset in seconds if anchored via authority."""
        return self._anchor_offset_sec

    def on_stream_dropped(self, reason: str) -> None:
        logger.warning(
            "%s %d Hz: stream dropped — %s",
            self._mode.upper(), self._frequency_hz, reason,
        )
        self._stream_start_epoch = None

    def on_stream_restored(self, channel_info) -> None:
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
