"""Marks a video as being worked on by `python -m clipper run`, so the dashboard
doesn't offer Continue (and start the same paid steps) while the terminal is busy.
A lock whose process has died counts as released."""

import ctypes
import os
import time

from . import db

_STILL_ACTIVE = 259
_QUERY_LIMITED = 0x1000


def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)  # signal 0 only checks; on Windows os.kill would terminate
        except OSError:
            return False
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _key(video_id: str) -> str:
    return f"cli_running:{video_id}"


def acquire(video_id: str) -> None:
    db.kv_set(_key(video_id), {"pid": os.getpid(), "since": time.time()})


def release(video_id: str) -> None:
    db.kv_delete(_key(video_id))


def running(video_id: str) -> bool:
    lock = db.kv_get(_key(video_id))
    return bool(lock) and _alive(int(lock["pid"]))
