"""psk_recorder.core.shm_sweep: orphaned jt9_decode segments go, live ones stay."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from psk_recorder.core import shm_sweep  # noqa: E402

HEADER = ("       key      shmid perms                  size  cpid  lpid nattch   uid   gid  cuid  cgid"
          "      atime      dtime      ctime                   rss                  swap")
UID = 997
DEAD = {111, 112, 222, 333}


def row(shmid, cpid, lpid, nattch, uid=UID, size=48275376):
    return (f"         0 {shmid:10d}   600 {size:21d} {cpid:5d} {lpid:5d} {nattch:6d} {uid:5d} {uid:5d}"
            f" {uid:5d} {uid:5d} 1 2 3 {size} 0")


def alive(pid):
    return pid not in DEAD


def test_only_unattached_segments_of_dead_processes_qualify():
    text = "\n".join([
        HEADER,
        row(1, 111, 112, 0),            # orphan: both dead, nothing attached
        row(2, 111, 500, 0),            # last user alive
        row(3, 500, 112, 0),            # creator alive
        row(4, 222, 333, 1),            # still attached
        row(5, 222, 333, 0, uid=0),     # another user's
        row(6, 222, 333, 0),            # orphan
    ])
    assert [s for s, _ in shm_sweep.orphan_segments(text, UID, alive)] == [1, 6]


def test_a_live_decoder_waiting_for_jt9_is_never_touched():
    # jt9_decode has attached its own segment before jt9 starts: nattch 1.
    text = "\n".join([HEADER, row(7, 500, 500, 1)])
    assert shm_sweep.orphan_segments(text, UID, alive) == []


def test_sweep_removes_orphans_and_dead_temp_dirs(tmp_path):
    proc = tmp_path / "shm"
    proc.write_text("\n".join([HEADER, row(1, 111, 112, 0), row(2, 500, 500, 1)]) + "\n")
    root = tmp_path / "devshm"
    (root / "jt9_decode_111_1700000000000").mkdir(parents=True)
    (root / "jt9_decode_111_1700000000000" / "timer.out").write_text("x")
    (root / f"jt9_decode_{os.getpid()}_1700000000001").mkdir()
    (root / "timestd").mkdir()
    removed = []
    freed = shm_sweep.sweep(proc, root, uid=UID, alive=lambda p: p == os.getpid() or alive(p),
                            rmid=lambda i: removed.append(i) or True)
    # temp dirs are removed only when owned by this user; the test owns them as itself
    assert removed == [1] and freed == 48275376
    assert not (root / "jt9_decode_111_1700000000000").exists() or os.getuid() != UID


def test_temp_dirs_of_dead_pids_go(tmp_path):
    root = tmp_path / "devshm"
    dead = root / "jt9_decode_111_1700000000000"
    live = root / f"jt9_decode_{os.getpid()}_1700000000001"
    other = root / "timestd"
    for d in (dead, live, other):
        d.mkdir(parents=True)
    shm_sweep.sweep(tmp_path / "missing", root, uid=os.getuid(),
                    alive=lambda p: p == os.getpid(), rmid=lambda i: True)
    assert not dead.exists() and live.exists() and other.exists()


def test_an_unreadable_table_removes_nothing(tmp_path):
    assert shm_sweep.sweep(tmp_path / "missing", tmp_path, uid=UID,
                           rmid=lambda i: (_ for _ in ()).throw(AssertionError)) == 0


def mrow(shmid, perms, nattch, uid=UID):
    return (f"         0 {shmid:10d}  {perms} {48275376:21d}   500   500 {nattch:6d} {uid:5d} {uid:5d}"
            f" {uid:5d} {uid:5d} 1 2 3 0 0")


def test_an_old_jt9_decode_build_is_noticed():
    text = "\n".join([HEADER,
                      mrow(1, "1600", 2),      # fixed build: marked once jt9 attached
                      mrow(2, " 600", 2),      # old build: attached, never marked
                      mrow(3, " 600", 1),      # jt9 not attached yet
                      mrow(4, " 600", 2, uid=0)])
    assert shm_sweep.unmarked_attached(text, UID) == 1
