"""Cross-platform file locks and process-tree control (POSIX and Windows).

``flock`` is a drop-in for ``fcntl.flock`` with the same ``LOCK_*`` flags: a busy non-blocking
request raises ``BlockingIOError``. On Windows it uses ``LockFileEx`` on a byte range far past any
real file content, so the locked file can still be read and written normally. Shared locks are
supported on both platforms.

``popen_group_kwargs`` starts a child in its own process group/session, and ``kill_tree`` /
``terminate_tree`` stop that child together with all of its descendants.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import IO, Any

WINDOWS = os.name == "nt"

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

if WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                            ctypes.POINTER(_Overlapped)]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(_Overlapped)]
    _UnlockFileEx.restype = wintypes.BOOL
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_IO_PENDING = 997
    _ERROR_NOT_LOCKED = 158
    # Lock one byte at a fixed offset beyond any realistic file size.
    _OFFSET_HIGH = 0x7FFFFFFF

    def _handle(file: IO[Any] | int) -> int:
        return msvcrt.get_osfhandle(file if isinstance(file, int) else file.fileno())

    def flock(file: IO[Any] | int, operation: int) -> None:
        overlapped = _Overlapped(0, 0, 0, _OFFSET_HIGH, None)
        handle = _handle(file)
        if operation & LOCK_UN:
            if not _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                error = ctypes.get_last_error()
                if error != _ERROR_NOT_LOCKED:
                    raise OSError(error, ctypes.FormatError(error))
            return
        flags = _LOCKFILE_EXCLUSIVE_LOCK if operation & LOCK_EX else 0
        if operation & LOCK_NB:
            flags |= _LOCKFILE_FAIL_IMMEDIATELY
        if not _LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
                raise BlockingIOError(error, "resource temporarily unavailable")
            raise OSError(error, ctypes.FormatError(error))
else:
    import fcntl

    def flock(file: IO[Any] | int, operation: int) -> None:
        native = 0
        if operation & LOCK_UN:
            native = fcntl.LOCK_UN
        elif operation & LOCK_EX:
            native = fcntl.LOCK_EX
        elif operation & LOCK_SH:
            native = fcntl.LOCK_SH
        if operation & LOCK_NB:
            native |= fcntl.LOCK_NB
        fcntl.flock(file, native)


def popen_group_kwargs() -> dict[str, Any]:
    """``subprocess.Popen`` keyword arguments that isolate the child's process tree."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _taskkill(pid: int) -> None:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)


def kill_tree(pid: int) -> None:
    """Forcefully stop ``pid`` and its descendants (``SIGKILL`` to its process group on POSIX)."""
    if WINDOWS:
        _taskkill(pid)
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def terminate_tree(pid: int, *, grace: float = 0.0) -> None:
    """Ask ``pid``'s tree to stop (``SIGTERM``); on Windows, or after ``grace`` seconds, force it."""
    if WINDOWS:
        _taskkill(pid)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    if grace > 0:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return
            time.sleep(0.1)
        kill_tree(pid)


def pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` currently exists."""
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes as _ctypes
        process = _ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return False
        code = _ctypes.c_ulong()
        _ctypes.windll.kernel32.GetExitCodeProcess(process, _ctypes.byref(code))
        _ctypes.windll.kernel32.CloseHandle(process)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
