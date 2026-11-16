"""Band-grouped resident jt9: one jt9_decode serves several channels of one mode.

Michael, 2026-11-16: the recording keeps its precise timing; when it decodes
can follow resource allocation.  A group's slots go to ONE resident process in
order, so decodes serialize (no boundary burst) and the station holds a few
shared-memory segments rather than one per band.  Fixed band->group affinity
keeps the compound-call hash table learning the same bands (docs/jt9-decoder.md
section 2).
"""
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from psk_recorder.core.jt9_group import Jt9Decoder, assign_groups

FT8_SAMPLES = 180000

# Stands in for jt9_decode -T.  Like the real wrapper (AudioReaderThread:
# fread of 4096 int16 samples at a time), it takes stdin in whole 8192-byte
# blocks, so a slot's tail stays unread until more audio arrives: one slot is
# 43 blocks plus 3872 samples.  It answers each complete slot in order with one
# decode line (echoing the slot's first sample, so a test can tell slots apart)
# and one terminal <DecodeStats>.  FAKE_EXIT_AFTER makes it exit after that
# many slots.
FAKE = r'''#!{py}
import os, struct, sys
n = {n}
exit_after = int(os.environ.get("FAKE_EXIT_AFTER", "0"))
k = 0
ring = b""
while True:
    block = sys.stdin.buffer.read(8192)      # blocks for a full block, as fread does
    if not block:
        sys.exit(0)
    ring += block
    while len(ring) >= 2 * n:
        slot, ring = ring[:2 * n], ring[2 * n:]
        first = struct.unpack("<h", slot[:2])[0]
        sys.stdout.write("000000 -10  0.1 1500 ~  CQ K{{}} FN20\n".format(first))
        sys.stdout.write("<DecodeStats> cycle_num={{}} watchdog=0 </DecodeStats>\n".format(k))
        sys.stdout.flush()
        k += 1
        if exit_after and k >= exit_after:
            sys.exit(0)
'''


@pytest.fixture
def fake_wrapper(tmp_path):
    path = tmp_path / "jt9_decode"
    path.write_text(FAKE.format(py=sys.executable, n=FT8_SAMPLES))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class Client:
    """The three callbacks a SlotWorker gives the decoder."""

    def __init__(self, freq):
        self.frequency_hz = freq
        self.lines = []
        self.done = []
        self.dropped = 0
        self.got = threading.Event()

    def on_jt9_line(self, line, slot_utc):
        self.lines.append((line, slot_utc))

    def on_jt9_done(self, ok):
        self.done.append(ok)
        self.got.set()

    def on_jt9_dropped(self):
        self.dropped += 1


def pcm(marker):
    """One FT8 slot of int16 PCM whose first sample carries `marker`."""
    import numpy as np
    a = np.zeros(FT8_SAMPLES, dtype="<i2")
    a[0] = marker
    return a.tobytes()


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_assign_groups_round_robin_balanced():
    freqs = [1840000, 3573000, 5357000, 7074000, 10136000,
             14074000, 18100000, 21074000, 24915000, 28074000]
    groups = assign_groups(freqs, 3)
    assert [len(g) for g in groups] == [3, 3, 2, 2]
    assert sorted(f for g in groups for f in g) == sorted(freqs)
    # Round-robin over sorted frequencies: neighbours land in different groups.
    assert groups[0] == [1840000, 10136000, 24915000]


def test_assign_groups_edges():
    assert assign_groups([7074000, 14074000], 1) == [[7074000], [14074000]]
    assert assign_groups([7074000, 14074000], 5) == [[7074000, 14074000]]
    assert assign_groups([], 3) == []
    assert assign_groups([14074000, 7074000], 0) == [[7074000], [14074000]]


def test_two_channels_share_one_process_and_results_route_back(fake_wrapper):
    dec = Jt9Decoder(fake_wrapper, "ft8", depth=3, cadence_sec=15.0,
                     name="ft8-g0", sweep=lambda: None, now=lambda: 1020.0)
    a, b = Client(7074000), Client(14074000)
    dec.start()
    assert dec.submit(a, pcm(11), 1000.0)
    assert dec.submit(b, pcm(22), 1000.0)
    assert dec.submit(a, pcm(33), 1015.0)
    dec.stop()          # closes stdin: the last slot's tail arrives, all decode
    assert [(l.split()[-2], s) for l, s in a.lines] == [("K11", 1000.0), ("K33", 1015.0)]
    assert [(l.split()[-2], s) for l, s in b.lines] == [("K22", 1000.0)]
    assert a.done == [True, True] and b.done == [True]
    assert dec.pid_count == 1


def test_last_slot_waits_for_the_next_one(fake_wrapper):
    """jt9_decode reads stdin in 4096-sample blocks, so the tail of the last
    slot fed reaches it only with the next slot's audio (or EOF).  Feeding must
    therefore never stop; the newest slot is answered one feed later."""
    dec = Jt9Decoder(fake_wrapper, "ft8", depth=3, cadence_sec=15.0,
                     name="ft8-g0", sweep=lambda: None, now=lambda: 1020.0)
    a = Client(7074000)
    dec.start()
    try:
        assert dec.submit(a, pcm(1), 1000.0)
        assert dec.submit(a, pcm(2), 1000.0)
        assert wait_for(lambda: len(a.done) == 1)
        time.sleep(0.3)
        assert len(a.done) == 1                 # slot 2's tail still unread
        assert dec.submit(a, pcm(3), 1015.0)
        assert wait_for(lambda: len(a.done) == 2)
    finally:
        dec.stop()
    assert [l.split()[-2] for l, _ in a.lines] == ["K1", "K2", "K3"]


def test_pending_for_reports_each_clients_unanswered_slots(fake_wrapper):
    dec = Jt9Decoder(fake_wrapper, "ft8", depth=3, cadence_sec=15.0,
                     name="ft8-g0", sweep=lambda: None, now=lambda: 1020.0)
    a, b = Client(7074000), Client(14074000)
    # Not started: nothing answers, so what was queued stays pending.
    dec._pending.extend([(a, 1000.0), (b, 1000.0), (a, 1015.0)])
    assert dec.pending_for(a) == [1000.0, 1015.0]
    assert dec.pending_for(b) == [1000.0]


class _FakeProc:
    """A running process whose stdin records what it was fed."""

    class _In:
        def __init__(self):
            self.fed = []

        def write(self, b):
            self.fed.append(len(b))

        def flush(self):
            pass

    def __init__(self):
        self.stdin = self._In()

    def poll(self):
        return None


def test_behind_but_not_stalled_still_feeds():
    """Falling behind by more than a cycle must never stop the feed: the
    previous slot's tail only arrives with new audio (fread blocks), so
    withholding it would stall the group for good (B4, 2026-11-16)."""
    now = [5000.0]
    dec = Jt9Decoder("/bin/false", "ft8", depth=3, cadence_sec=15.0,
                     name="ft8-g0", sweep=lambda: None, now=lambda: now[0])
    a = Client(7074000)
    dec._running = True
    dec._proc = proc = _FakeProc()
    dec._pending.append((a, 4960.0))     # ended 4975: 25 s ago, under the stall limit
    assert dec.submit(a, pcm(1), 4990.0) is True
    assert proc.stdin.fed == [2 * FT8_SAMPLES]
    assert dec.restarts == 0 and a.dropped == 0
    assert dec.pending_for(a) == [4960.0, 4990.0]


def test_stalled_group_restarts_and_keeps_feeding(fake_wrapper):
    """An oldest unanswered slot past the stall limit means the process is
    stuck: restart it (its unanswered slots drop, logged) and feed the new
    slot to the fresh process."""
    now = [5000.0]
    dec = Jt9Decoder(fake_wrapper, "ft8", depth=3, cadence_sec=15.0,
                     name="ft8-g0", sweep=lambda: None, now=lambda: now[0])
    a = Client(7074000)
    dec.start()
    try:
        stuck = dec._proc
        dec._pending.append((a, 4900.0))     # ended 4915: 85 s ago, past the limit
        assert dec.submit(a, pcm(7), 4985.0) is True
        assert dec.restarts == 1 and dec._proc is not stuck
        assert a.dropped == 1
        assert dec.pending_for(a) == [4985.0]
    finally:
        dec.stop()
    assert [l.split()[-2] for l, _ in a.lines] == ["K7"]


def test_dead_process_restarts_and_drops_unanswered_slots(fake_wrapper, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT_AFTER", "1")
    dec = Jt9Decoder(fake_wrapper, "ft8", depth=3, cadence_sec=15.0,
                     name="ft8-g0", sweep=lambda: None, now=lambda: 1020.0)
    a = Client(7074000)
    dec.start()
    try:
        assert dec.submit(a, pcm(1), 1000.0)
        assert dec.submit(a, pcm(2), 1000.0)    # completes slot 1; then it exits
        assert wait_for(lambda: len(a.done) == 1)
        first = dec._proc
        assert wait_for(lambda: first.poll() is not None)
        monkeypatch.delenv("FAKE_EXIT_AFTER")
        assert dec.submit(a, pcm(3), 1015.0)    # restarts (slot 2 lost), feeds 3
        assert dec._proc is not first
        assert dec.restarts == 1 and a.dropped == 1
    finally:
        dec.stop()
    assert [l.split()[-2] for l, _ in a.lines] == ["K1", "K3"]
    assert a.done == [True, True]


def test_stop_drains_and_is_idempotent(fake_wrapper):
    dec = Jt9Decoder(fake_wrapper, "ft8", depth=3, cadence_sec=15.0,
                     name="ft8-g0", sweep=lambda: None, now=lambda: 1020.0)
    a = Client(7074000)
    dec.start()
    dec.start()                     # second start: no second process
    assert dec.pid_count == 1
    assert dec.submit(a, pcm(5), 1000.0)
    dec.stop()                      # closes stdin; the fed slot still decodes
    dec.stop()
    assert a.done == [True]
