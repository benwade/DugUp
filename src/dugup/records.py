"""Record files: what DugUp moved to the trash or renamed, one line per step, so it can be undone.

A record is created exclusively, with a name that cannot collide (the second, the process and a counter),
so two operations in one second never share a file and a record is never truncated by a later one.

The protocol every file-changing operation follows, through `Journal`:

    journal.intent(line)    before the file is changed: written, flushed and synced to disk. If that fails,
                            the operation must not change the file, and must not go on to the next one:
                            `intent` raises RecordError, and the operation reports it and stops.
    journal.outcome(line)   after the file was changed (or left alone): written and flushed. If that fails
                            the change has already happened; `outcome` raises RecordError too, and the
                            operation reports the change truthfully (it is in its result) and stops there,
                            so nothing more happens without a record. The intent line is on disk already.

A journal that failed once stays failed: `broken` says why, and every later call raises at once.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import IO, Optional, Tuple

_counter = 0


class RecordError(OSError):
    """The record of a file change could not be written. Carries the reason in words."""


def open_new(folder: Path | str, stem: str) -> Tuple[Path, IO[str]]:
    """(path, handle) for a new record under `folder`, created exclusively. Raises OSError when it cannot be."""
    global _counter
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    newline = "\r\n" if os.name == "nt" else "\n"
    for _ in range(1000):
        _counter += 1
        path = folder / f"{stem} {stamp} {os.getpid()}-{_counter}.txt"
        try:
            return path, open(path, "x", encoding="utf-8-sig", newline=newline)
        except FileExistsError:
            continue
    raise OSError(f"no free record name under {folder}")


def sync(handle: IO[str]) -> None:
    """Flush and ask the disk to keep it. Raises OSError when either fails: a promise of recovery that the
    disk did not confirm is no promise."""
    handle.flush()
    os.fsync(handle.fileno())


INTENT_WORDS = ("renaming", "moving")


class Journal:
    """The record of one operation, line by line. `intent` and `outcome` raise RecordError when the record
    cannot be written; after that, `broken` says why and nothing more is accepted."""

    def __init__(self, handle: IO[str], synced: bool = True) -> None:
        self.handle = handle
        self.synced = synced            # intent lines are synced to disk (off only in tests of the protocol)
        self.broken: str = ""
        self.lines: list = []            # what was written, for a caller that reports after a failure

    def _write(self, line: str, durable: bool) -> None:
        if self.broken:
            raise RecordError(self.broken)
        try:
            self.handle.write(line + "\n")
            if durable and self.synced:
                sync(self.handle)
            else:
                self.handle.flush()
        except OSError as exc:
            self.broken = f"{type(exc).__name__}: {exc}"
            raise RecordError(self.broken) from exc
        self.lines.append(line)

    def intent(self, line: str) -> None:
        self._write(line, durable=True)

    def outcome(self, line: str) -> None:
        self._write(line, durable=False)

    def __call__(self, line: str) -> None:
        """As a plain `journal(line)` callable: a line that starts with an intent word is an intent."""
        if line.split("\t", 1)[0] in INTENT_WORDS:
            self.intent(line)
        else:
            self.outcome(line)

    def close(self) -> None:
        try:
            self.handle.close()
        except OSError as exc:
            self.broken = self.broken or f"{type(exc).__name__}: {exc}"


def journal_into(handle: IO[str], problems: Optional[list] = None) -> Journal:
    """A Journal over a record handle. (Kept for callers that pass a list for problems: the journal's `broken`
    is the one place the reason lives now; the list, if given, is appended to as well.)"""
    journal = Journal(handle)
    if problems is not None:
        original = journal._write

        def noting(line: str, durable: bool) -> None:
            try:
                original(line, durable)
            except RecordError as exc:
                if not problems or problems[-1] != str(exc):
                    problems.append(str(exc))
                raise

        journal._write = noting      # type: ignore[method-assign]
    return journal


def stopped_by_record(done, journal: Optional[Journal]) -> None:
    """Mark an operation's result as stopped by its record, with the reason, when the journal broke."""
    if journal is not None and getattr(journal, "broken", ""):
        done.record_problem = journal.broken
        done.stopped = True
