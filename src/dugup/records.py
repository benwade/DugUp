"""Record files: what DugUp moved to the trash or renamed, one line per outcome, so it can be undone.

A record is created exclusively, with a name that cannot collide (the second, the process and a counter),
so two operations in one second never share a file and a record is never truncated by a later one. Lines
are flushed as they are written; the caller may also ask for a sync before promising recovery.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import IO, Tuple

_counter = 0


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
    """Flush and ask the disk to keep it: called before a file is changed when its record must survive a crash."""
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass
