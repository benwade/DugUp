"""The one launcher: every outside program and every worker is started here (CONTRACTS.md, "Stopping").

What it promises:
- A program is ended together with everything it started. On Windows the program is placed in a Job
  Object before it runs a single instruction (it is created suspended, assigned, then resumed), so
  nothing it starts can escape, and the job is set to end its members when the last handle to it
  closes: if this program dies, however it dies, the children die with it. On other systems the
  program gets its own session and the whole process group is ended.
- A memory limit, when asked for: the Job Object's limit on Windows, the address-space limit elsewhere.
- No console window pops up on Windows, and a Ctrl+C typed at the console reaches this program only;
  it decides what to tell its children.
- Output comes back as bytes. Decoding is the caller's decision, never a platform default.

Nothing here knows what is being launched.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from .protocols import StopSignal
from .shapes import Failure, FailureReason

IS_WINDOWS = os.name == "nt"
POLL_SECONDS = 0.05   # how often a waiting call looks at the stop signal and the clock


# ---- Windows: the Job Object ----------------------------------------------------------------------------

if IS_WINDOWS:   # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMITS(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC_LIMITS), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _JOB_EXTENDED_LIMIT_INFORMATION = 9
    _KILL_ON_JOB_CLOSE = 0x2000
    _LIMIT_JOB_MEMORY = 0x0200
    CREATE_SUSPENDED = 0x00000004
    CREATE_NO_WINDOW = 0x08000000

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    def _create_job(memory_mb: Optional[int]) -> Optional[int]:
        """A job that ends its members when its last handle closes. None when Windows refuses to make one."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = _EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if memory_mb:
            limits.BasicLimitInformation.LimitFlags |= _LIMIT_JOB_MEMORY
            limits.JobMemoryLimit = int(memory_mb) * 1024 * 1024
        if not _kernel32.SetInformationJobObject(job, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                                                 ctypes.sizeof(limits)):
            _kernel32.CloseHandle(job)
            return None
        return job


@dataclass
class Completed:
    """What a finished program left behind. Output is bytes; decode it deliberately."""

    returncode: int
    stdout: bytes
    stderr: bytes


class Launched:
    """A running program and everything it has started."""

    def __init__(self, process: subprocess.Popen, job: Optional[int] = None, contained: bool = True) -> None:
        self.process = process
        self._job = job
        self.contained = contained   # False when the tree could not be tied together (see `launch`)
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> Optional[int]:
        return self.process.poll()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill_tree(self) -> None:
        """End the program and everything it started, now. Safe to call again, and after it has ended."""
        with self._lock:
            if IS_WINDOWS:   # pragma: no cover
                if self._job:
                    _kernel32.TerminateJobObject(self._job, 1)
                elif self.process.poll() is None:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                                   capture_output=True, creationflags=CREATE_NO_WINDOW)
            else:
                try:
                    os.killpg(self.process.pid, 9)   # the group it leads; children that are still in it go too
                except (ProcessLookupError, PermissionError):
                    pass
            if self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    pass
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        """Let go of the program's pipes and of the job. A program still running is ended first."""
        if self.process.poll() is None:
            self.kill_tree()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        with self._lock:
            if IS_WINDOWS and self._job:   # pragma: no cover
                _kernel32.CloseHandle(self._job)
                self._job = None

    def __enter__(self) -> "Launched":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def launch(args: Sequence[str], *, stdin: Optional[int] = subprocess.DEVNULL, stdout: Optional[int] = subprocess.PIPE,
           stderr: Optional[int] = subprocess.PIPE, env: Optional[Mapping[str, str]] = None,
           cwd: Optional[str] = None, memory_mb: Optional[int] = None) -> "Launched | Failure":
    """Start a program. Failure(NOT_INSTALLED) when it cannot be found, Failure(OTHER) when it cannot start."""
    args = [os.fspath(a) for a in args]
    try:
        if IS_WINDOWS:   # pragma: no cover
            job = _create_job(memory_mb)
            flags = CREATE_NO_WINDOW | (CREATE_SUSPENDED if job else 0)
            process = subprocess.Popen(args, stdin=stdin, stdout=stdout, stderr=stderr, env=env, cwd=cwd,
                                       creationflags=flags)
            if not job:
                return Launched(process, None, contained=False)
            assigned = bool(_kernel32.AssignProcessToJobObject(job, int(process._handle)))
            resumed = _ntdll.NtResumeProcess(int(process._handle)) == 0
            if not resumed:
                process.kill()
                _kernel32.CloseHandle(job)
                return Failure(FailureReason.OTHER, f"{os.path.basename(args[0])} was created but could not be started")
            if not assigned:
                _kernel32.CloseHandle(job)
                return Launched(process, None, contained=False)
            return Launched(process, job)
        process = subprocess.Popen(args, stdin=stdin, stdout=stdout, stderr=stderr, env=env, cwd=cwd,
                                   start_new_session=True)
        _limit_address_space(process.pid, memory_mb)
        return Launched(process)
    except FileNotFoundError:
        return Failure(FailureReason.NOT_INSTALLED, f"the program was not found: {args[0]}")
    except PermissionError as exc:
        return Failure(FailureReason.OTHER, f"the program may not be run: {args[0]} ({exc.strerror or exc})")
    except OSError as exc:
        return Failure(FailureReason.OTHER, f"the program could not be started: {args[0]} ({exc.strerror or exc})")


def _limit_address_space(pid: int, memory_mb: Optional[int]) -> None:
    """For POSIX: cap the program's address space from outside, right after it starts. (Doing it inside the
    child before it runs would need a pre-exec hook, which is not safe in a program that has threads.)
    Where the system cannot do this the limit is not applied; a worker also limits itself at start."""
    if not memory_mb:
        return
    try:
        import resource
        amount = int(memory_mb) * 1024 * 1024
        resource.prlimit(pid, resource.RLIMIT_AS, (amount, amount))
    except (ImportError, AttributeError, OSError, ValueError):
        pass


def run(args: Sequence[str], time_limit: float, stop: Optional[StopSignal] = None, input: Optional[bytes] = None,
        env: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None, memory_mb: Optional[int] = None,
        timer: Callable[[], float] = time.monotonic) -> "Completed | Failure":
    """Run a program to its end and collect its output.

    Failure(TIMED_OUT) after `time_limit` seconds and Failure(STOPPED) on a stop request; in both cases
    the program and everything it started have been ended before this returns.
    """
    started = launch(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, env=env, cwd=cwd,
                     memory_mb=memory_mb)
    if isinstance(started, Failure):
        return started
    with started as program:
        collected: Dict[str, bytes] = {}

        def pump() -> None:   # reading in a thread, so a full pipe can never block the program or this loop
            try:
                out, err = program.process.communicate(input)
            except (OSError, ValueError):
                out, err = b"", b""
            collected["out"], collected["err"] = out or b"", err or b""

        reader = threading.Thread(target=pump, name="dugup-launcher-reader", daemon=True)
        reader.start()
        deadline = timer() + max(0.0, time_limit)
        name = os.path.basename(str(args[0]))
        while True:
            reader.join(POLL_SECONDS)
            if not reader.is_alive():
                break
            if stop is not None and stop.is_set():
                program.kill_tree()
                reader.join(10)
                return Failure(FailureReason.STOPPED, f"{name} was stopped before it finished")
            if timer() >= deadline:
                program.kill_tree()
                reader.join(10)
                return Failure(FailureReason.TIMED_OUT, f"{name} did not finish within {time_limit:g} s")
        code = program.process.poll()
        return Completed(code if code is not None else -1, collected.get("out", b""), collected.get("err", b""))


def alive(pid: int) -> bool:
    """Whether a process with this id exists right now (used by tests and by the health report)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:   # pragma: no cover
        SYNCHRONIZE, STILL_ACTIVE = 0x00100000, 259
        handle = _kernel32.OpenProcess(SYNCHRONIZE | 0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:   # a process that has ended but has not been collected yet is not alive
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b")", 1)[-1].split()[0] != b"Z"
    except OSError:
        return True
