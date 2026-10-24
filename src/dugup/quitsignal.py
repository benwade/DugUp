"""Quit all of DugUp: one signal that every DugUp program sees.

The tray, the main window and DugUp Remote are programs of their own. "Quit DugUp" in any of them leaves a
small file in the data folder saying when; each program looks at it about once a second and closes itself
if the signal is newer than the program. A program started afterwards is not affected, and nothing needs
the tray to be running.

Only programs that share a data folder see each other's signal (a source checkout and an installed copy
with separate data folders do not).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

FILE = "quit-all"
EVERY = 10              # look every this many polls (about once a second at the usual 100 ms)


def send(root: Path | str, now: Callable[[], float] = time.time) -> None:
    """Ask every DugUp program on this data folder to close."""
    try:
        path = Path(root) / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{now():.3f}\n", encoding="utf-8")
    except OSError:
        pass                # the program asking still closes itself; the others stay, as before


def asked_since(root: Path | str, started: float) -> bool:
    """True when a quit was asked for after `started` (a time.time() value)."""
    try:
        asked = float((Path(root) / FILE).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return False
    return asked > started


class Listener:
    """For a program's poll: `due()` is True once a quit is asked for after the program started."""

    def __init__(self, root: Path | str, now: Callable[[], float] = time.time) -> None:
        self.root, self.started, self.count = root, now(), 0

    def due(self) -> bool:
        self.count += 1
        if self.count % EVERY:
            return False
        return asked_since(self.root, self.started)
