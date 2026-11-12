"""Remove jt9_decode shared memory that a dead process left behind.

jt9_decode drives jt9 through a ~46 MB SysV segment per (band, mode).  Builds
before 2026-11-12 removed it only on a clean in-process exit, so every
SIGTERM or SIGKILL left one behind, unattached and owned by psk-recorder's
user.  On AI6VN-1 12.75 GB of them OOM-locked the VM.  The fixed jt9_decode
marks its segment for removal once jt9 attaches, so nothing new is left; this
sweep clears what older builds (or a kill before jt9 attached) left.

A segment is removed only when all hold: it belongs to this process's user,
nothing is attached, and both the process that created it and the last one to
touch it are gone.  A live jt9_decode still waiting for jt9 has itself attached
(nattch 1), so the sweep never touches it.  The per-instance temp folders
`/dev/shm/jt9_decode_<pid>_<ms>` of dead pids go too.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

PROC_SHM = Path("/proc/sysvipc/shm")
SHM_ROOT = Path("/dev/shm")
IPC_RMID = 0
_TEMP_DIR = re.compile(r"^jt9_decode_(\d+)_\d+$")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _rmid(shmid: int) -> bool:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    return libc.shmctl(ctypes.c_int(shmid), ctypes.c_int(IPC_RMID), None) == 0


def orphan_segments(text: str, uid: int,
                    alive: Callable[[int], bool] = _pid_alive) -> List[tuple]:
    """(shmid, size) of this user's unattached segments whose creator and
    last user are both dead, from /proc/sysvipc/shm's text."""
    lines = text.splitlines()
    if not lines:
        return []
    cols = lines[0].split()
    try:
        ix = {k: cols.index(k) for k in ("shmid", "size", "cpid", "lpid", "nattch", "uid")}
    except ValueError:
        return []
    out = []
    for ln in lines[1:]:
        f = ln.split()
        if len(f) < len(cols) - 2:
            continue
        try:
            shmid, size = int(f[ix["shmid"]]), int(f[ix["size"]])
            cpid, lpid = int(f[ix["cpid"]]), int(f[ix["lpid"]])
            nattch, owner = int(f[ix["nattch"]]), int(f[ix["uid"]])
        except (ValueError, IndexError):
            continue
        if owner != uid or nattch != 0:
            continue
        if alive(cpid) or alive(lpid):
            continue
        out.append((shmid, size))
    return out


def sweep(proc_shm: Path = PROC_SHM, shm_root: Path = SHM_ROOT,
          uid: Optional[int] = None,
          alive: Callable[[int], bool] = _pid_alive,
          rmid: Callable[[int], bool] = _rmid) -> int:
    """Remove orphaned segments and temp folders; returns bytes of segments
    removed.  Never raises."""
    uid = os.getuid() if uid is None else uid
    freed = 0
    try:
        text = proc_shm.read_text()
    except OSError:
        text = ""
    removed = 0
    for shmid, size in orphan_segments(text, uid, alive):
        try:
            if rmid(shmid):
                removed += 1
                freed += size
        except OSError:
            pass
    if removed:
        logger.warning("removed %d orphaned jt9_decode shared-memory segment(s), "
                       "%.0f MB, left by dead decoders", removed, freed / 1e6)
    try:
        entries = list(shm_root.iterdir())
    except OSError:
        entries = []
    for d in entries:
        m = _TEMP_DIR.match(d.name)
        if not m or alive(int(m.group(1))):
            continue
        try:
            if d.is_dir() and not d.is_symlink() and d.stat().st_uid == uid:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
    return freed
