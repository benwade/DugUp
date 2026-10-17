"""Console output: safe printing and the small formatters every command shares."""

from __future__ import annotations

import datetime
import os
import time
from typing import Callable, Optional, TextIO
from ..indexer import Progress
from ..protocols import WARN_MASS_LOSS, WARN_ROOT_EMPTY


MISSING_NOTE = ("Not at this path at the last scan. If this was on a temporarily unavailable disk, reconnect it and "
                "scan again and the file will be restored. Otherwise it will be removed from the index on {date}.")


UNAVAILABLE_NOTE = "Its folder is not available right now; the file is kept in the index."


WARNING_TEXT = {
    WARN_ROOT_EMPTY: "the folder is reachable but empty, and it was not before. Nothing was marked missing.",
    WARN_MASS_LOSS: "at least half of the folder's files disappeared at once. Nothing was marked missing.",
}


def safe_write(text: str, stream: TextIO, end: str = "\n") -> None:
    """Write text to a console that may not be able to show every character.

    A Windows console in a legacy code page raises on characters it cannot show
    (for example a Japanese folder name). Show an escape instead of crashing.
    """
    if stream is None:        # started without a console (the window's launcher): there is nowhere to write
        return
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(text.encode(encoding, "backslashreplace").decode(encoding) + end)
    stream.flush()


# ---- small formatting helpers ----------------------------------------------------------------------------

def format_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def format_date(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "never"
    try:
        return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def one_line(text: str) -> str:
    return " ".join(text.split())


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 3)] + "..."


def progress_text(p: Progress) -> str:
    """One line that says where a run is: phase, counts, percent, rate, time left, current file."""
    if p.phase == "scan":
        return f"[scan] looking through {p.root}"
    if p.phase == "record":
        return f"[scan] recording changes: {p.done:,} of {p.total:,}"
    if p.phase == "estimate":
        left = p.remaining_seconds
        left_text = "" if left is None else f" and take about {format_seconds(left)} more"
        return (f"[estimate] after {p.done:,} of {p.total:,} files: at this rate the index would end near "
                f"{p.projected_bytes / 1048576:,.0f} MB{left_text}. Ctrl+C stops; what is done is kept.")
    if p.phase == "download":
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        return (f"[download] {p.root}: {p.done / 1048576:,.1f} of {p.total / 1048576:,.1f} MB ({p.percent:.0f}%)  "
                f"{p.rate / 1048576:.1f} MB/s  {left_text}")
    if p.phase == "unpack":
        return f"[download] unpacking {p.root}"
    if p.phase == "mark":
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        line = (f"[same files] marking what was read before: {p.done:,} of {p.total:,} ({p.percent:.1f}%)  "
                f"{p.rate:.1f} files/s  {left_text}")
        return line + (f"  | {shorten(os.path.basename(p.current), 40)}" if p.current else "")
    if p.phase == "compare":
        return f"[alike] comparing: {p.done:,} of {p.total:,} pictures"
    if p.phase == "caption":
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        return (f"[rename] writing captions: {p.done:,} of {p.total:,} ({p.percent:.1f}%)  {left_text}"
                + (f"  | {shorten(os.path.basename(p.current), 40)}" if p.current else ""))
    if p.phase == "look":
        if p.root:
            return f"[alike] {p.root} on the {p.current}: {p.done:,} of {p.total:,}"
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        line = f"[alike] looking at pictures: {p.done:,} of {p.total:,} ({p.percent:.1f}%)  {p.rate:.1f} pictures/s  {left_text}  unread {p.failed:,}"
        return line + (f"  | {shorten(os.path.basename(p.current), 40)}" if p.current else "")
    if p.phase == "sketch":
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        line = f"[meaning] sketching: {p.done:,} of {p.total:,} file(s) ({p.percent:.1f}%)  {p.rate:.1f} files/s  {left_text}"
        return line + (f"  | {shorten(os.path.basename(p.current), 40)}" if p.current else "")
    if p.phase == "measure":
        if p.current:
            return f"[meaning] {p.root} on the {p.current}: {p.done:,} of {p.total:,} piece(s)"
        return f"[ocr] looking at the size of the pictures: {p.done:,} of {p.total:,}"
    left = p.remaining_seconds
    left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
    if p.phase == "ocr":
        line = (f"[ocr] {p.done:,} of {p.total:,} page(s) ({p.percent:.1f}%)  {p.rate * 60:.1f} pages/min  "
                f"{left_text}  failed {p.failed:,}")
    else:
        line = (f"[read] {p.done:,} of {p.total:,} ({p.percent:.1f}%)  {p.rate:.1f} files/s  "
                f"{left_text}  failed {p.failed:,}")
    if p.current:
        line += "  | " + shorten(os.path.basename(p.current), 40)
    return line


class ProgressPrinter:
    """Prints progress at most once a second: phase, counts, percent, rate, time left, current file."""

    def __init__(self, stream: TextIO, interval: float = 1.0, timer: Callable[[], float] = time.monotonic) -> None:
        self.stream = stream
        self.interval = interval
        self.timer = timer
        self.live = bool(getattr(stream, "isatty", lambda: False)())
        self._last_time: Optional[float] = None
        self._last_phase = ""
        self._width = 0

    def __call__(self, p: Progress) -> None:
        now = self.timer()
        fresh = p.phase != self._last_phase or p.done >= p.total > 0
        if not fresh and self._last_time is not None and now - self._last_time < self.interval:
            return
        if p.phase == "estimate":       # a line of its own that stays on the screen
            self.finish()
            safe_write(progress_text(p), self.stream)
            return
        self._last_time, self._last_phase = now, p.phase
        line = progress_text(p)
        if self.live:
            pad = " " * max(0, self._width - len(line))
            self._width = len(line)
            safe_write("\r" + line + pad, self.stream, end="")
        else:
            safe_write(line, self.stream)

    def finish(self) -> None:
        if self.live and self._width:
            safe_write("", self.stream)
            self._width = 0


def format_bytes(count: int) -> str:
    size = float(max(count, 0))
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"


def quote(text: str) -> str:
    """A pattern or path as it can be typed in PowerShell and in a Linux shell."""
    if text and all(ch.isalnum() or ch in "._-/\\:*?" for ch in text):
        return text
    return "'" + text.replace("'", "''") + "'"


def _size_text(count: float) -> str:
    """A size a person can compare at a glance: MB below 1,000 MB, GB above."""
    mb = count / 1048576
    if mb >= 1000:
        return f"{mb / 1024:,.1f} GB"
    return f"{mb:,.0f} MB" if mb >= 10 else f"{mb:,.1f} MB"


def print_compacted(result, out: TextIO) -> None:
    if result:
        safe_write(f"The index was compacted: {result['before'] / 1048576:,.0f} MB before, "
                   f"{result['after'] / 1048576:,.0f} MB after.", out)


def megabytes(count: Optional[int]) -> str:
    return "unknown" if count is None else f"{count / (1024 * 1024):,.0f} MB"
