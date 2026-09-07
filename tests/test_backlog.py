"""Tests for the decode-backlog monitor (overdue / saturated) and the
in-flight snapshot it reads.  Ported in spirit from wsprdaemon's
wd-decode-backlog.sh after K6FOD's 42 GB FT8 pile-up went unnoticed."""

import logging

from psk_recorder.core.backlog import BacklogMonitors, DecodeBacklogMonitor
from psk_recorder.core.slot import inflight_from


def test_inflight_from_counts_procs_and_jt9_slots():
    procs = [(None, None, 0.0, 100.0), (None, None, 0.0, 130.0)]   # forked at mono 100, 130
    jt9 = [1000.0]                                                # slot started at utc 1000, cadence 15
    n, oldest = inflight_from(procs, jt9, 15.0, now_mono=150.0, now_utc=1030.0)
    assert n == 3
    assert oldest == 50.0          # the mono-100 fork, 50 s ago; jt9 slot is 15 s past its end


def test_inflight_from_empty():
    assert inflight_from([], [], 15.0, 0.0, 0.0) == (0, 0.0)


def test_overdue_warns_at_once():
    m = DecodeBacklogMonitor("rx:FT8", warn_age_s=45, ratio=1.0, sustain_samples=3)
    a = m.assess(inflight=2, oldest_s=50, channels=10)
    assert a.level == "warn" and "OVERDUE" in a.reason


def test_saturated_needs_sustained_samples():
    m = DecodeBacklogMonitor("rx:FT8", warn_age_s=45, ratio=1.0, sustain_samples=3)
    levels = [m.assess(inflight=12, oldest_s=20, channels=10).level for _ in range(3)]
    assert levels == ["ok", "ok", "warn"]


def test_saturation_streak_resets_when_it_clears():
    m = DecodeBacklogMonitor("rx:FT8", warn_age_s=45, ratio=1.0, sustain_samples=3)
    m.assess(12, 20, 10); m.assess(12, 20, 10)
    assert m.assess(5, 20, 10).level == "ok"
    assert m.assess(12, 20, 10).level == "ok"      # streak restarted at 1


def test_one_decode_per_channel_is_normal():
    m = DecodeBacklogMonitor("rx:FT8", warn_age_s=45, ratio=1.0, sustain_samples=3)
    for _ in range(5):
        assert m.assess(inflight=10, oldest_s=8, channels=10).level == "ok"


def test_registry_logs_transitions(caplog):
    reg = BacklogMonitors()
    with caplog.at_level(logging.INFO, logger="psk_recorder.core.backlog"):
        reg.observe("rx:FT4", 3, 50, 10, now=0)         # overdue → WARNING
        reg.observe("rx:FT4", 3, 55, 10, now=60)        # still, no reminder
        reg.observe("rx:FT4", 0, 0, 10, now=120)        # cleared → INFO
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("OVERDUE" in x for x in msgs) == 1
    assert any("cleared" in x for x in msgs)
