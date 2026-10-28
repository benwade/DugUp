"""Ending `dugup mcp serve` when the app that started it is gone.

A server normally ends when the app closes its standard input. That once failed: a server from a source checkout
outlived the Claude app and kept answering after the app's settings had moved on to another copy. On Windows a
virtual environment's python.exe is only a launcher that starts the real Python as its child, so the server's
parent is the launcher, not the app, and the launcher stays as long as the server does. So the watch looks past
a launcher to the app itself, and when that process is gone the server ends.

Nothing here guesses with timing: whether the app is alive is asked of the system (a process handle on Windows,
the process table elsewhere), and the tests drive `Watch.check` directly.
"""

from __future__ import annotations

import ntpath
import os
import sys
import threading
from typing import Callable, Dict, Optional, Tuple

CHECK_SECONDS = 5.0


class Watch:
    """Calls `on_gone` once, the first time `alive()` says no. `run` checks every `every` seconds until stopped."""

    def __init__(self, alive: Callable[[], bool], on_gone: Callable[[], None], every: float = CHECK_SECONDS,
                 stop: Optional[threading.Event] = None) -> None:
        self.alive = alive
        self.on_gone = on_gone
        self.every = every
        self.stopping = stop or threading.Event()
        self.fired = False
        self._thread: Optional[threading.Thread] = None

    def check(self) -> bool:
        """True while the watched process lives. When it is gone, `on_gone` runs (once) and this is False."""
        try:
            living = bool(self.alive())
        except Exception:
            living = True          # a failed question is not an answer: never end the server on a guess
        if not living and not self.fired:
            self.fired = True
            self.on_gone()
        return living

    def run(self) -> None:
        while not self.stopping.wait(self.every):
            if not self.check():
                return

    def start(self) -> "Watch":
        self._thread = threading.Thread(target=self.run, name="dugup-parent-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()


def watched_pid(me: int, parent_of: Callable[[int], Optional[int]], name_of: Callable[[int], str],
                executable: str, base_executable: str) -> Optional[int]:
    """The process to watch: this one's parent, or, when that parent is a virtual environment's launcher (this
    Python runs from a virtual environment and the parent has the launcher's file name), the launcher's parent.
    None when it cannot be told."""
    parent = parent_of(me)
    if not parent:
        return None
    in_venv = os.path.normcase(os.path.abspath(executable)) != os.path.normcase(os.path.abspath(base_executable))
    if in_venv and name_of(parent).lower() == ntpath.basename(executable).lower():   # \ or /
        return parent_of(parent) or None
    return parent


# ---- Windows ----------------------------------------------------------------------------------------------------

def _windows_table() -> Dict[int, Tuple[int, str]]:
    """Every process: its id -> (its parent's id, its file name), from one snapshot."""
    import ctypes
    from ctypes import wintypes

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ENTRY)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ENTRY)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)     # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    table: Dict[int, Tuple[int, str]] = {}
    try:
        entry = ENTRY()
        entry.dwSize = ctypes.sizeof(ENTRY)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def windows_alive(pid: int) -> Optional[Callable[[], bool]]:
    """A question "is that process still running?", asked of a handle opened now (so a later process given the
    same id is never mistaken for it). None when the process cannot be opened."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x00100000, False, pid)          # SYNCHRONIZE: enough to wait on it
    if not handle:
        return None

    def alive() -> bool:
        # WAIT_OBJECT_0 (0) means it has ended; still running (WAIT_TIMEOUT) and a failed wait both count as alive,
        # so the server never ends on a question that went wrong
        return kernel32.WaitForSingleObject(handle, 0) != 0x00000000

    return alive


# ---- elsewhere --------------------------------------------------------------------------------------------------

def posix_alive(pid: int) -> Callable[[], bool]:
    def alive() -> bool:
        if pid == os.getppid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True            # it exists; it belongs to someone else
        return True

    return alive


# ---- this process -----------------------------------------------------------------------------------------------

def _gone() -> None:
    try:
        sys.stderr.write("DugUp: the app that started this server is gone, so the server ends.\n")
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


def start_for_this_process(on_gone: Callable[[], None] = _gone) -> Optional[Watch]:
    """Watch the app that started this server, and end the server when it is gone. None when the app cannot be
    told or watched; then the server runs as before, ending when its standard input closes."""
    try:
        executable = sys.executable or ""
        base = getattr(sys, "_base_executable", executable) or executable
        if sys.platform.startswith("win"):
            table = _windows_table()
            pid = watched_pid(os.getpid(), lambda p: table.get(p, (0, ""))[0], lambda p: table.get(p, (0, ""))[1],
                              executable, base)
            alive = windows_alive(pid) if pid else None
        else:
            pid = watched_pid(os.getpid(), lambda p: os.getppid() if p == os.getpid() else None, lambda p: "",
                              executable, base)
            alive = posix_alive(pid) if pid and pid > 1 else None
    except Exception:
        return None
    if alive is None:
        return None
    return Watch(alive, on_gone).start()
