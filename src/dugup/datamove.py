"""Moving an existing data folder into the folder beside the program, or keeping it where it is.

Used when DugUp finds an index in the older per-user folder and none beside the program (see datadir).
The move never leaves a half-made index behind and never loses the old one:

1. It refuses while a DugUp tray uses the old folder, or an indexing run holds it.
2. It copies into a temporary folder beside the destination: the index through SQLite's own backup (a
   consistent copy, even while something reads it), everything else file by file.
3. It checks the copy: every file at its size, and the index passes SQLite's quick check.
4. Only then does the copy take the destination's name, the start-at-login entry is pointed at it, and the
   old folder is removed. If removing the old folder fails, the data is already safe in its new place and
   the result says what was left behind.

Stopping at any point before step 4 removes the temporary folder and leaves everything as it was.
"""

from __future__ import annotations

import datetime
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .datadir import KEPT_ELSEWHERE
from .lockfile import RunLock

INDEX = "index.sqlite"
# Files that belong to a running DugUp, not to the data: never carried over.
LEFT_BEHIND = frozenset({"index.lock", "index.sqlite-wal", "index.sqlite-shm", "tray.json"})
LEFT_BEHIND_FOLDERS = frozenset({"tmp"})


class MoveError(Exception):
    """The move did not happen. `reason` says why, in plain words; nothing was changed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Moved:
    old: str
    new: str
    files: int = 0
    bytes: int = 0
    startup_updated: bool = False
    left_behind: str = ""          # the old folder could not be removed: why (the data is safe in `new`)
    notes: List[str] = field(default_factory=list)   # anything else worth saying (a leftover from an earlier try)


@dataclass
class _Plan:
    files: List[Tuple[Path, int]] = field(default_factory=list)    # (relative path, size), the index excluded
    total: int = 0


def keep_where_it_is(new_data: Path, old: Path) -> Path:
    """Record that the older data folder stays in use. Returns the note written. Moving later still works."""
    new_data = Path(new_data)
    new_data.mkdir(parents=True, exist_ok=True)
    note = new_data / KEPT_ELSEWHERE
    stamp = datetime.date.today().isoformat()
    note.write_text(f"# DugUp uses the data folder on the next line instead of this one (chosen {stamp}).\n"
                    f"# To bring it here: dugup data move. To start afresh here: delete this file.\n{old}\n",
                    encoding="utf-8")
    return note


def _plan(old: Path) -> _Plan:
    plan = _Plan()
    for folder, dirs, files in os.walk(old):
        rel_folder = Path(folder).relative_to(old)
        if rel_folder == Path("."):
            dirs[:] = [d for d in dirs if d not in LEFT_BEHIND_FOLDERS]
        for name in files:
            rel = rel_folder / name
            if rel_folder == Path(".") and (name in LEFT_BEHIND or name == INDEX):
                continue
            size = (Path(folder) / name).stat().st_size
            plan.files.append((rel, size))
            plan.total += size
    return plan


def _destination_is_free(new: Path) -> None:
    if not new.exists():
        return
    if not new.is_dir():
        raise MoveError(f"there is a file, not a folder, at {new}")
    leftovers = [p.name for p in new.iterdir() if p.name != KEPT_ELSEWHERE]
    if leftovers:
        raise MoveError(f"{new} already holds other files ({', '.join(sorted(leftovers)[:3])}); "
                        "DugUp will not mix two data folders. Move or delete them first")


def _copy_index(old: Path, staging: Path, stop: Callable[[], bool], tell: Callable[[int], None]) -> int:
    source = old / INDEX
    if not source.is_file():
        return 0
    size = source.stat().st_size
    src = sqlite3.connect(str(source))      # read through the backup only; a read-only open of a WAL index
                                            # can fail when its side files are not there
    try:
        dst = sqlite3.connect(str(staging / INDEX))
        try:
            def progress(status, remaining, total):
                if stop():
                    raise MoveError("stopped; nothing was moved")
                done = total - remaining
                tell(int(size * done / total) if total else 0)

            src.backup(dst, pages=256, progress=progress)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dst.close()
    finally:
        src.close()
    return size


def _check_copy(staging: Path, plan: _Plan, had_index: bool) -> None:
    for rel, size in plan.files:
        copied = staging / rel
        if not copied.is_file() or copied.stat().st_size != size:
            raise MoveError(f"the copy of {rel} did not come out whole; nothing was moved")
    if had_index:
        conn = sqlite3.connect(str(staging / INDEX))
        try:
            answer = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        if not answer or answer[0] != "ok":
            raise MoveError("the copied index did not pass its check; nothing was moved")


def update_startup(old: Path, new: Path, entry: Optional[Path] = None) -> bool:
    """Point a start-at-login entry that names the old folder at the new one. True when one was changed."""
    if entry is None:
        from .tray import startup_entry
        entry = startup_entry()
    try:
        with open(entry, encoding="utf-8", newline="") as handle:      # line endings kept exactly as they were
            text = handle.read()
    except OSError:
        return False
    before, after = f'--data-dir "{old}"', f'--data-dir "{new}"'
    if before not in text:
        return False
    with open(entry, "w", encoding="utf-8", newline="") as handle:
        handle.write(text.replace(before, after))
    return True


def tray_running(old: Path) -> bool:
    from . import summons
    return summons.summon(old, "ping") == "pong"


def _staging(new: Path) -> "tuple[Path, Optional[Path]]":
    """The temporary folder to copy into, beside `new`, and a leftover from an earlier try that could not be
    removed (then a fresh name is used, so a stuck leftover never blocks a move)."""
    first = new.with_name(new.name + ".moving")
    if not first.exists():
        return first, None
    shutil.rmtree(first, ignore_errors=True)       # ours, from a move that was cut off
    if not first.exists():
        return first, None
    for number in range(2, 100):
        other = new.with_name(f"{new.name}.moving-{number}")
        if other.exists():
            shutil.rmtree(other, ignore_errors=True)
        if not other.exists():
            return other, first
    raise MoveError(f"earlier attempts left folders beside {new} that cannot be removed; restart the computer, "
                    "delete them, and try again")


def _drop(staging: Path) -> str:
    """Remove the temporary copy. "" when it went; otherwise words saying what was left and where."""
    shutil.rmtree(staging, ignore_errors=True)
    if staging.exists():
        return (f". The temporary folder {staging} could not be removed yet (a sync app or a scanner may hold it); "
                "it holds no data that is in use and can be deleted later")
    return ""


def move(old: Path | str, new: Path | str, tell: Optional[Callable[[int, int], None]] = None,
         stop: Optional[Callable[[], bool]] = None, startup_entry: Optional[Path] = None,
         is_tray_running: Callable[[Path], bool] = tray_running) -> Moved:
    """Move the data folder `old` to `new` (see the module notes). `tell(done_bytes, total_bytes)` is told of
    progress; `stop()` asks to stop. Raises MoveError, with nothing changed, when it cannot be done."""
    old, new = Path(os.path.abspath(old)), Path(os.path.abspath(new))
    stop = stop or (lambda: False)
    tell = tell or (lambda done, total: None)
    if not old.is_dir():
        raise MoveError(f"there is no data folder at {old}")
    if old == new:
        raise MoveError("the data folder is already there")
    _destination_is_free(new)
    from .datadir import DataDirError, check_allowed
    try:
        check_allowed(new)
    except DataDirError as exc:
        raise MoveError(f"the new place is not safe for the index: {exc.reason}") from exc
    if is_tray_running(old):
        raise MoveError("DugUp is running in the tray. Quit it first (right-click its icon, Quit), then move")
    lock = RunLock(old / "index.lock")
    if not lock.acquire():
        raise MoveError("DugUp is indexing right now. Let it finish or stop it, then move")
    done = Moved(str(old), str(new))
    staging, stuck = _staging(new)
    if stuck is not None:
        done.notes.append(f"an earlier attempt left {stuck} behind and Windows would not let it be removed yet "
                          "(a sync app or a scanner may hold it); it holds no data of yours and can be deleted later")
    try:
        try:
            staging.mkdir(parents=True)
            plan = _plan(old)
            had_index = (old / INDEX).is_file()
            index_size = (old / INDEX).stat().st_size if had_index else 0
            total = plan.total + index_size
            done.bytes = _copy_index(old, staging, stop, lambda b: tell(b, total))
            for rel, size in plan.files:
                if stop():
                    raise MoveError("stopped; nothing was moved")
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old / rel, target)
                done.bytes += size
                done.files += 1
                tell(done.bytes, total)
            done.files += 1 if had_index else 0
            _check_copy(staging, plan, had_index)
            if new.exists():
                note = new / KEPT_ELSEWHERE
                if note.exists():
                    note.unlink()
                new.rmdir()
            os.replace(staging, new)
        except MoveError as exc:
            exc.reason += _drop(staging)
            raise
        except (OSError, sqlite3.Error) as exc:
            raise MoveError(f"could not copy the data folder ({exc}); nothing was moved, and your index is still "
                            f"in use in {old}" + _drop(staging)) from exc
        except BaseException:                    # Ctrl+C: the copy is dropped, the old folder is untouched
            _drop(staging)
            raise
        done.startup_updated = update_startup(old, new, startup_entry)
    finally:
        lock.release()
    try:
        shutil.rmtree(old)
    except OSError as exc:
        done.left_behind = f"{exc.strerror or exc}. Your data is safe in {new}; the old folder can be deleted"
    return done
