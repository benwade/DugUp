"""Keeping the index current by itself, while the tray runs.

Every `watch_every_minutes` (and once when the tray starts) the folders are scanned again and what changed
is read; then OCR, meaning and looks for what is new, as the window's Update does. Gently: a quarter of the
cores, OCR in its gentle mode, so the machine stays the person's. The scan is cheap; only changed files
cost anything.

One run at a time across programs is the index lock's job; this only adds a schedule. A run started by
hand in the window takes today's speed and, when it finds the tray's run in the way, may ask the tray to
stop through the summons ("stop"); the tray's next run then starts at the next tick.

The work runs on a thread of its own. Everything the tray needs to show comes through `state()`, which is
safe to call from the tray's thread at any time.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, List, Optional

from .shapes import Failure, FailureReason

STEPS = ("index", "ocr", "meaning", "looks")


class Watcher:
    def __init__(self, service, settings, clock: Callable[[], float] = time.monotonic,
                 cores: Optional[int] = None, say: Optional[Callable[[str], None]] = None) -> None:
        self.service, self.settings, self.clock = service, settings, clock
        self.say = say or (lambda text: None)
        self.cores = cores or os.cpu_count() or 2
        self.paused = False
        self.stop_flag: Optional[threading.Event] = None
        self.thread: Optional[threading.Thread] = None
        self.last_started: Optional[float] = None
        self.last_finished: Optional[float] = None
        self.last_outcome = ""
        self.step = ""
        self.asked_now = False
        self.lock = threading.Lock()

    # -- what the tray asks

    @property
    def every_seconds(self) -> float:
        return max(0.0, float(self.settings.watch_every_minutes)) * 60.0

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def due(self, now: Optional[float] = None) -> bool:
        """Whether a run should start now: asked for, or the time has come (the first tick counts)."""
        if self.running or self.paused and not self.asked_now:
            return False
        if self.asked_now:
            return True
        if self.every_seconds <= 0:
            return False
        now = self.clock() if now is None else now
        return self.last_started is None or now - self.last_started >= self.every_seconds

    def tick(self) -> bool:
        """Called often by the tray; starts a run when one is due. True when one was started."""
        if not self.due():
            return False
        self.start()
        return True

    def now(self) -> None:
        """Run at the next tick, paused or not."""
        self.asked_now = True

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def stop(self) -> None:
        if self.stop_flag is not None:
            self.stop_flag.set()

    def state(self) -> str:
        """One line for the menu or the status: what it is doing, or when it last ran."""
        if self.running:
            return f"updating ({self.step})"
        if self.paused:
            return "updates paused"
        if self.every_seconds <= 0:
            return "updates off (watch_every_minutes is 0)"
        if self.last_finished is None:
            return "no update yet"
        ago = int(self.clock() - self.last_finished)
        return f"last update {ago // 60} min ago: {self.last_outcome}"

    # -- the run

    def start(self) -> None:
        self.asked_now = False
        self.stop_flag = threading.Event()
        self.last_started = self.clock()
        self.thread = threading.Thread(target=self._run, name="dugup-watcher", daemon=True)
        self.thread.start()

    def gentle_readers(self) -> int:
        return max(1, self.cores // 4)

    def _run(self) -> None:
        stop = self.stop_flag
        outcomes: List[str] = []
        try:
            for step in STEPS:
                if stop.is_set():
                    outcomes.append("stopped")
                    break
                self.step = step
                report = self._step(step, stop)
                if isinstance(report, Failure):
                    if report.reason is FailureReason.BUSY:
                        outcomes.append("another run was going; waited for the next time")
                    elif report.reason is FailureReason.STOPPED:
                        outcomes.append("stopped")
                    else:
                        outcomes.append(f"{step}: {report.detail}")
                    break
                outcomes.append(self._said(step, report))
        except Exception as exc:           # never die silently in the background
            outcomes.append(f"{self.step}: {type(exc).__name__}: {exc}")
        finally:
            self.step = ""
            self.last_finished = self.clock()
            self.last_outcome = "; ".join(o for o in outcomes if o) or "nothing to do"
            self.say(f"Background update: {self.last_outcome}")

    def _step(self, step: str, stop: threading.Event):
        s = self.service
        if step == "index":
            return s.run_indexing(stop, None, readers=self.gentle_readers())
        if step == "ocr":
            if not self.settings.ocr_after_index:
                return None
            return s.run_ocr(stop, None, fast=False)
        if step == "meaning":
            if not self.settings.meaning_after_index or self._waiting("meaning") == 0:
                return None
            return s.run_meaning(stop, None)
        if step == "looks":
            if not self.settings.looks_after_index or self._waiting("looks") == 0:
                return None
            return s.run_looks(stop, None, readers=self.gentle_readers())
        return None

    def _waiting(self, what: str) -> int:
        """How many files (or pictures) have no sketch yet, read through a connection of this thread's own:
        the service's reading connection belongs to the tray's thread and must not be touched from here."""
        s = self.service
        opener = getattr(s, "_open_store", None)
        if opener is None:                       # a stand-in service in the tests answers directly
            return s.meaning_waiting() if what == "meaning" else s.looks_waiting()
        store = opener()
        try:
            if what == "meaning":
                done, could = store.sketch_counts(s.meaning_model())
            else:
                done, could, _ = store.look_counts(s.looks_model())
            return max(0, could - done)
        finally:
            store.close()

    @staticmethod
    def _said(step: str, report) -> str:
        if report is None:
            return ""
        name = {"index": "read", "ocr": "read", "meaning": "files", "looks": "pictures"}[step]
        value = getattr(report, name, None)
        return f"{step} {value:,}" if isinstance(value, int) else step
