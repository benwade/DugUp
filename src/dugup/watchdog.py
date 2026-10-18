"""The freeze recorder: when a DugUp window stops answering, write down where it was stuck.

A window answers by running its event loop. The watchdog asks that loop to tick every second (root.after) and
watches from a thread of its own. If no tick comes for `FROZEN_SECONDS`, it writes the stack of every thread
to logs/freeze-<window>-<time>.txt, once per freeze, and notes in the log when the window answers again.
The report window (report.py) attaches the latest of these files.

What is written is program locations only (file, line, function), never the values in them: no document
text and no file names from your folders.
"""

from __future__ import annotations

import datetime
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

FROZEN_SECONDS = 15.0
TICK_MS = 1000
KEEP = 10                    # freeze notes kept in the logs folder; older ones are removed
PREFIX = "freeze-"


def thread_stacks(own: Optional[int] = None) -> str:
    """Every thread's stack, as program locations only."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        if ident == own:
            continue
        lines = traceback.format_list(traceback.extract_stack(frame))   # file, line, function and source line
        parts.append(f"Thread {names.get(ident, ident)}:\n" + "".join(lines))
    return "\n".join(parts)


class Watchdog:
    """Watch one window's event loop. `clock` and `start` are seams for tests."""

    def __init__(self, root, logs: Path, name: str, *, clock: Callable[[], float] = time.monotonic,
                 frozen_after: float = FROZEN_SECONDS, start: bool = True) -> None:
        self.root, self.logs, self.name = root, Path(logs), name
        self.clock, self.frozen_after = clock, frozen_after
        self.last_tick = clock()
        self.frozen_since: Optional[float] = None
        self.written: Optional[Path] = None
        self._stop = threading.Event()
        self._tick_id = None
        self._tick()
        self.thread = threading.Thread(target=self._watch, name=f"dugup-watchdog-{name}", daemon=True)
        if start:
            self.thread.start()

    def _tick(self) -> None:
        self.last_tick = self.clock()
        try:
            self._tick_id = self.root.after(TICK_MS, self._tick)
        except Exception:
            self._stop.set()            # the window is gone

    def check(self) -> Optional[Path]:
        """One look: write a note when frozen (once per freeze). Returns the note written now, if any."""
        quiet = self.clock() - self.last_tick
        if quiet < self.frozen_after:
            if self.frozen_since is not None:
                self._append(f"{_now()} answered again after about {self.clock() - self.frozen_since:.0f} s\n")
                self.frozen_since = None
            return None
        if self.frozen_since is not None:
            return None
        self.frozen_since = self.last_tick
        return self._write(quiet)

    def _write(self, quiet: float) -> Optional[Path]:
        try:
            self.logs.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.logs / f"{PREFIX}{self.name}-{stamp}.txt"
            from . import __version__
            path.write_text(f"DugUp {__version__}: the {self.name} stopped answering for {quiet:.0f} s at {_now()}.\n"
                            f"Python {sys.version.split()[0]} on {sys.platform}\n\n"
                            + thread_stacks(own=self.thread.ident), encoding="utf-8")
            self.written = path
            prune(self.logs)
            return path
        except OSError:
            return None

    def _append(self, line: str) -> None:
        if self.written is None:
            return
        try:
            with open(self.written, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    def _watch(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.check()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._tick_id is not None:
                self.root.after_cancel(self._tick_id)
        except Exception:
            pass


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def prune(logs: Path, keep: int = KEEP) -> None:
    notes = sorted(Path(logs).glob(PREFIX + "*.txt"), key=lambda p: p.stat().st_mtime)
    for old in notes[:-keep] if keep else notes:
        try:
            os.remove(old)
        except OSError:
            pass


def latest_notes(logs: Path, days: float = 7.0, most: int = 3):
    """The most recent freeze notes, newest first, from the last `days`."""
    cutoff = time.time() - days * 86400
    try:
        notes = [p for p in Path(logs).glob(PREFIX + "*.txt") if p.stat().st_mtime >= cutoff]
    except OSError:
        return []
    return sorted(notes, key=lambda p: p.stat().st_mtime, reverse=True)[:most]
