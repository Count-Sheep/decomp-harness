"""Portable, read-only process listing for worker telemetry (Windows, Linux, macOS).

Rows use the shape the harness's m2c report expects from ``process_snapshot()``:
``{"pid", "parent", "elapsed_seconds", "state", "argv"}``. ``snapshot()`` returns ``None`` when
processes cannot be observed on this host, so callers can tell "no runner" from "unknown".

Order of preference: ``psutil`` when it happens to be installed (optional), then the platform's
own interface: ``/proc`` on Linux (including WSL), the Win32 API through ``ctypes`` on Windows,
and ``ps`` elsewhere (macOS/BSD, where it always exists). Nothing here signals or changes a process.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

WINDOWS = os.name == "nt"
Row = dict[str, Any]


def _psutil() -> list[Row] | None:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    rows, now = [], time.time()
    for process in psutil.process_iter(["pid", "ppid", "cmdline", "create_time", "status"]):
        info = process.info
        status = str(info.get("status") or "")
        state = "Z" if status == "zombie" else "T" if status == "stopped" else "R"
        rows.append({"pid": int(info["pid"]), "parent": int(info.get("ppid") or 0),
                     "elapsed_seconds": max(0, int(now - float(info.get("create_time") or now))),
                     "state": state, "argv": list(info.get("cmdline") or [])})
    return rows


def _linux(proc: Path = Path("/proc"), ticks: int | None = None) -> list[Row] | None:
    try:
        uptime = float((proc / "uptime").read_text().split()[0])
        ticks = ticks or os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    rows = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            stat = (entry / "stat").read_text()
        except OSError:
            continue  # exited while listing, or not ours to read
        if not raw:
            continue  # kernel thread
        fields = stat[stat.rfind(")") + 2:].split()
        try:
            state, parent, started = fields[0], int(fields[1]), int(fields[19])
        except (IndexError, ValueError):
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.rstrip(b"\0").split(b"\0")]
        rows.append({"pid": int(entry.name), "parent": parent,
                     "elapsed_seconds": max(0, int(uptime - started / ticks)), "state": state, "argv": argv})
    return rows


def _ps() -> list[Row] | None:
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,stat=,command="],
                                capture_output=True, text=True, check=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in result.stdout.splitlines():
        try:
            pid, parent, elapsed, state, command = line.split(None, 4)
            parts = [int(part) for part in elapsed.replace("-", ":").split(":")]
            seconds = sum(value * weight for value, weight in zip(reversed(parts), (1, 60, 3600, 86400)))
            rows.append({"pid": int(pid), "parent": int(parent), "elapsed_seconds": seconds,
                         "state": state, "argv": shlex.split(command)})
        except ValueError:
            continue
    return rows


if WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    class _UnicodeString(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                    ("Buffer", ctypes.c_void_p)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _shell32 = ctypes.WinDLL("shell32")
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                                 wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    _ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    _shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    _shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _QUERY_LIMITED = 0x1000
    _COMMAND_LINE_CLASS = 60  # ProcessCommandLineInformation, Windows 8.1 and later

    def _filetime(value: wintypes.FILETIME) -> int:
        return (value.dwHighDateTime << 32) | value.dwLowDateTime

    def _split(command: str) -> list[str]:
        count = ctypes.c_int(0)
        pointer = _shell32.CommandLineToArgvW(command, ctypes.byref(count))
        if not pointer:
            return [command]
        try:
            return [pointer[index] for index in range(count.value)]
        finally:
            _kernel32.LocalFree(pointer)

    def _details(pid: int, now: int) -> tuple[list[str] | None, int | None]:
        handle = _kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return None, None
        try:
            elapsed = None
            times = [wintypes.FILETIME() for _ in range(4)]
            if _kernel32.GetProcessTimes(handle, *[ctypes.byref(item) for item in times]):
                elapsed = max(0, (now - _filetime(times[0])) // 10_000_000)
            size = wintypes.ULONG(0)
            _ntdll.NtQueryInformationProcess(handle, _COMMAND_LINE_CLASS, None, 0, ctypes.byref(size))
            if not size.value:
                return None, elapsed
            buffer = ctypes.create_string_buffer(size.value)
            if _ntdll.NtQueryInformationProcess(handle, _COMMAND_LINE_CLASS, buffer, size, ctypes.byref(size)) != 0:
                return None, elapsed
            text = _UnicodeString.from_buffer(buffer)
            if not text.Buffer or not text.Length:
                return None, elapsed
            return _split(ctypes.wstring_at(text.Buffer, text.Length // 2)), elapsed
        finally:
            _kernel32.CloseHandle(handle)

    def _windows() -> list[Row] | None:
        snapshot = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == _INVALID_HANDLE:
            return None
        entries = []
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(_ProcessEntry)
            more = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                entries.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
                more = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        now_time = wintypes.FILETIME()
        _kernel32.GetSystemTimeAsFileTime(ctypes.byref(now_time))
        now = _filetime(now_time)
        rows = []
        for pid, parent, exe in entries:
            argv, elapsed = None, None
            # Command lines are only needed for interpreters: the harness's runners and m2c are Python.
            if exe.lower().startswith(("python", "py.exe", "pyw")):
                argv, elapsed = _details(pid, now)
            rows.append({"pid": pid, "parent": parent, "elapsed_seconds": elapsed or 0,
                         "state": "R", "argv": argv or [exe]})
        return rows


def list_processes() -> list[Row] | None:
    """Every observable process now, or ``None`` when this host offers no way to list them."""
    try:
        rows = _psutil()
        if rows is not None:
            return rows
        if WINDOWS:
            return _windows()
        if sys.platform.startswith("linux") and Path("/proc/self").is_dir():
            return _linux()
        return _ps()
    except Exception:  # noqa: BLE001 - observation must never take the dashboard down
        return None


class SharedSnapshot:
    """One process scan shared by every reader within ``max_age`` seconds."""

    def __init__(self, lister=list_processes, max_age: float = 1.0, clock=time.monotonic):
        self.lister, self.max_age, self.clock = lister, max_age, clock
        self.lock = threading.Lock()
        self.value: list[Row] | None = None
        self.taken: float | None = None

    def __call__(self) -> list[Row] | None:
        with self.lock:
            now = self.clock()
            if self.taken is None or now - self.taken >= self.max_age:
                self.value, self.taken = self.lister(), now
            return None if self.value is None else [dict(row, argv=list(row["argv"])) for row in self.value]


shared_snapshot = SharedSnapshot()


def option(argv: list[str], name: str) -> str | None:
    """The value of ``--name VALUE`` or ``--name=VALUE`` in an argument vector (last one wins)."""
    value = None
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith(name + "="):
            value = arg[len(name) + 1:]
    return value


def script(argv: list[str], names: set[str]) -> tuple[str, int] | None:
    """The first argument naming one of ``names`` (a ``.py`` file), with its index, for Python argv only."""
    if not argv or not Path(argv[0]).name.lower().startswith(("python", "py")):
        return None
    for index, arg in enumerate(argv[1:], 1):
        name = Path(arg).name
        if name in names:
            return name, index
    return None
