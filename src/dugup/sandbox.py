"""SubprocessSandbox: the Sandbox contract with real worker processes (CONTRACTS.md part 7).

A job runs in a worker (worker.py), never in this process. The worker is started through the launcher,
so stopping it ends everything it started, and on Windows it dies with this program. A worker stays up
between jobs (starting Python and loading a reader costs more than most jobs); one that times out,
crashes or runs out of memory is ended and a fresh one is started for the next job. That is the whole
point: a bad file can hurt one worker, not the index.

The answers:
  the job's result            when it finished
  Failure(TIMED_OUT)          it ran past its time limit and was ended
  Failure(OUT_OF_MEMORY)      it needed more memory than allowed
  Failure(CRASHED)            the worker died, or the job raised
  Failure(STOPPED)            a stop request arrived; the worker was ended at once
  Failure(NOT_INSTALLED/OTHER) the worker could not be started
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import launcher
from .protocols import Limits, StopSignal
from .shapes import Failure, FailureReason

READY_SECONDS = 60.0   # how long a new worker may take to say it is ready (a cold disk, an antivirus scan)
UNCONTAINED = ("files were not read: the reading program could not be placed under the limits that end it and "
               "everything it starts (a Windows Job Object), and DugUp does not read files without them")
_END = object()


class _Worker:
    def __init__(self, program: launcher.Launched, hello: Dict[str, Any]) -> None:
        self.program = program
        self.pid = int(hello.get("pid") or program.pid)
        self.network = str(hello.get("network", "unknown"))
        self.lines: "queue.Queue[object]" = queue.Queue()
        self.errors: List[bytes] = []

    def close(self) -> None:
        self.program.close()


def _read_lines(stream, lines: "queue.Queue[object]") -> None:
    try:
        for line in iter(stream.readline, b""):
            lines.put(line)
    except (OSError, ValueError):
        pass
    lines.put(_END)


def _drain(stream, into: List[bytes], keep: int = 50) -> None:
    try:
        for line in iter(stream.readline, b""):
            into.append(line)
            del into[:-keep]
    except (OSError, ValueError):
        pass


class SubprocessSandbox:
    def __init__(self, python: Optional[str] = None, extra_paths: Sequence[str] = (),
                 timer: Callable[[], float] = time.monotonic, poll: float = launcher.POLL_SECONDS) -> None:
        self.python = python or sys.executable
        self.extra_paths = [os.fspath(p) for p in extra_paths]   # where jobs outside this package are found
        self.timer = timer
        self.poll = poll
        self._workers: Dict[Optional[int], _Worker] = {}   # one kept worker per memory limit
        self._lock = threading.Lock()
        self.started = 0     # workers started so far (a restart after a failure counts)

    # -- workers

    def _environment(self) -> Dict[str, str]:
        env = dict(os.environ)
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = [package_root] + self.extra_paths + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _start(self, memory_mb: Optional[int], stop: Optional[StopSignal]) -> "_Worker | Failure":
        args = [self.python, "-m", "dugup.worker"]
        if memory_mb:
            args.append(f"--memory-mb={int(memory_mb)}")
        program = launcher.launch(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=self._environment(), memory_mb=memory_mb if launcher.IS_WINDOWS else None)
        if isinstance(program, Failure):
            return program
        if not program.contained:
            # files are read here, and a file can be made to attack the program that reads it: never without the
            # limits that end the worker and everything it starts (Windows Job Object; its own session elsewhere)
            program.close()
            return Failure(FailureReason.OTHER, UNCONTAINED)
        self.started += 1
        worker = _Worker(program, {})
        threading.Thread(target=_read_lines, args=(program.process.stdout, worker.lines), daemon=True).start()
        threading.Thread(target=_drain, args=(program.process.stderr, worker.errors), daemon=True).start()
        message = self._next(worker, self.timer() + READY_SECONDS, stop)
        if isinstance(message, Failure):
            worker.close()
            if message.reason is FailureReason.STOPPED:
                return message
            return Failure(FailureReason.CRASHED, f"a worker could not start ({message.detail}){self._last_words(worker)}")
        worker.pid = int(message.get("pid") or worker.pid)
        worker.network = str(message.get("network", "unknown"))
        return worker

    @staticmethod
    def _last_words(worker: _Worker) -> str:
        text = b"".join(worker.errors[-3:]).decode("utf-8", "replace").strip()
        return f"; it said: {text[-300:]}" if text else ""

    def _next(self, worker: _Worker, deadline: float, stop: Optional[StopSignal]) -> "Dict[str, Any] | Failure":
        """The worker's next answer, or why there is none. Looks at the stop signal and the clock while it waits."""
        while True:
            try:
                line = worker.lines.get(timeout=self.poll)
            except queue.Empty:
                if stop is not None and stop.is_set():
                    return Failure(FailureReason.STOPPED, "stopped while a worker was busy")
                if self.timer() >= deadline:
                    return Failure(FailureReason.TIMED_OUT, "no answer within the time limit")
                continue
            if line is _END:
                code = worker.program.wait(5)
                return Failure(FailureReason.CRASHED, f"the worker ended without an answer (exit code {code})")
            try:
                return json.loads(line.decode("utf-8"))   # type: ignore[union-attr]
            except (ValueError, UnicodeDecodeError):
                continue   # not ours: ignore it and keep waiting

    def _discard(self, key: Optional[int]) -> None:
        worker = self._workers.pop(key, None)
        if worker is not None:
            worker.close()

    # -- the contract

    def run(self, job: str, payload: Mapping[str, object], limits: Limits,
            stop: Optional[StopSignal] = None) -> "object | Failure":
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped before the job began")
        key = limits.memory_mb or None
        with self._lock:
            worker = self._workers.get(key)
            if worker is not None and worker.program.poll() is not None:
                self._discard(key)
                worker = None
            if worker is None:
                started = self._start(key, stop)
                if isinstance(started, Failure):
                    return started
                worker = self._workers[key] = started
            try:
                request = json.dumps({"job": job, "payload": dict(payload)}).encode("ascii") + b"\n"
            except (TypeError, ValueError) as exc:
                raise ValueError(f"a job's payload must be plain JSON values: {exc}") from exc
            try:
                worker.program.process.stdin.write(request)
                worker.program.process.stdin.flush()
            except (OSError, ValueError):
                self._discard(key)
                return Failure(FailureReason.CRASHED, "the worker was gone before the job could be handed over")
            message = self._next(worker, self.timer() + max(0.0, limits.seconds), stop)
            if isinstance(message, Failure):
                words = self._last_words(worker) if message.reason is FailureReason.CRASHED else ""
                self._discard(key)    # whatever it was doing, it and everything it started are ended now
                if message.reason is FailureReason.TIMED_OUT:
                    return Failure(FailureReason.TIMED_OUT, f"the job did not finish within {limits.seconds:g} s and was ended")
                return Failure(message.reason, message.detail + words)
            if message.get("ok"):
                return message.get("result")
            kind = message.get("kind")
            detail = str(message.get("detail") or "the job failed without saying why")
            if kind == "out_of_memory":
                self._discard(key)
                return Failure(FailureReason.OUT_OF_MEMORY, detail)
            return Failure(FailureReason.CRASHED, f"the job raised {detail}")

    def describe(self, limits: Optional[Limits] = None) -> "Mapping[str, object] | Failure":
        """What the worker is: its process id and how far it is cut off from the network."""
        return self.run("dugup.worker:hello", {}, limits or Limits(READY_SECONDS))   # type: ignore[return-value]

    def worker_pids(self) -> List[int]:
        return [w.pid for w in self._workers.values()]

    def close(self) -> None:
        """End every worker and everything they started."""
        with self._lock:
            for key in list(self._workers):
                self._discard(key)

    def __enter__(self) -> "SubprocessSandbox":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
