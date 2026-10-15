"""A report of the same file kept in several places on one drive, with the copy to keep marked.

Nothing here changes a file. DugUp never deletes. The report is for a person to act on, and it says for
every copy where it is.

  report()   the sets of exact copies on a drive, the keeper of each chosen, and the totals
  Keeper rule (Ben's): a copy in a folder the person named as the proper home first; then the copy that is
  part of the larger set of files, that is, the one whose folder
  holds more files; then the copy higher up in the folders; then the one seen first. The date a file was made
  is not kept by the index (and a copy gets a new one when it is made), so it does not decide.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .records import RecordError
from .shapes import FileRecord


@dataclass(frozen=True)
class Copy:
    record: FileRecord
    folder_files: int          # files lying in the same folder: the size of the set this copy is part of
    protected: bool = False    # in a folder that is never proposed (a live backup of this computer)


@dataclass
class Same:
    """One file, kept in several places."""
    keep: Copy
    extra: List[Copy]          # the other copies, the ones a person might remove
    aside: List[Copy] = field(default_factory=list)   # copies in protected folders: shown, never proposed

    @property
    def size(self) -> int:
        return self.keep.record.size

    @property
    def wasted(self) -> int:
        return self.size * len(self.extra)


@dataclass
class Report:
    drive: str
    protected: Tuple[str, ...]
    sets: List[Same]
    preferred: Tuple[str, ...] = ()
    protected_empty: Tuple[str, ...] = ()   # protected folders with no indexed file below them: likely misspelt

    @property
    def extra_files(self) -> int:
        return sum(len(s.extra) for s in self.sets)

    @property
    def wasted_bytes(self) -> int:
        return sum(s.wasted for s in self.sets)


def _under(path: str, folder: str, fold: Callable[[str], str]) -> bool:
    base = folder.rstrip("/\\")
    p = fold(path)
    return p == fold(base) or p.startswith(fold(base) + "/") or p.startswith(fold(base) + "\\")


def folder_of(path: str) -> str:
    """The folder part of a path, whichever separator it uses (an index made on Windows is read anywhere)."""
    cut = max(path.rfind("/"), path.rfind("\\"))
    return path[:cut] if cut > 0 else ""


def _depth(path: str) -> int:
    return path.count("/") + path.count("\\")


def report(store, drive: str, protected: Sequence[str] = (), case_insensitive: Optional[bool] = None,
           preferred: Sequence[str] = ()) -> Report:
    """The sets of exact copies below `drive` (a drive or any folder). Copies in a protected folder are shown
    beside their set and never proposed; a set with fewer than two copies left is not a set. A copy below a
    preferred folder (a folder the person calls the proper home of such files) wins the choice of keeper."""
    if case_insensitive is None:
        case_insensitive = os.name == "nt"
    fold = (lambda t: t.lower()) if case_insensitive else (lambda t: t)
    sets = store.same_content(drive, case_insensitive)
    empty = tuple(p for p in protected if not store.list_files(prefix=p, limit=1, case_insensitive=case_insensitive))
    folders = sorted({folder_of(r.path) for members in sets for r in members})
    counts = store.files_in_folders(folders)
    found: List[Same] = []
    for members in sets:
        copies = [Copy(r, counts.get(folder_of(r.path), 0), any(_under(r.path, p, fold) for p in protected))
                  for r in members]
        open_ones = [c for c in copies if not c.protected]
        if len(open_ones) < 2:
            continue
        ranked = sorted(open_ones, key=lambda c: (not any(_under(c.record.path, p, fold) for p in preferred),
                                                  -c.folder_files, _depth(c.record.path), len(c.record.path), c.record.row_id or 0))
        found.append(Same(ranked[0], ranked[1:], [c for c in copies if c.protected]))
    found.sort(key=lambda s: (-s.wasted, s.keep.record.path.lower()))
    return Report(drive, tuple(protected), found, tuple(preferred), empty)


# ---- removing the extras ------------------------------------------------------------------------------------

@dataclass
class Removal:
    moved: List[Tuple[str, str]] = field(default_factory=list)      # (extra moved to the trash, the copy kept)
    skipped: List[Tuple[str, str]] = field(default_factory=list)    # (extra left alone, why)
    stopped: bool = False
    record_problem: str = ""        # the record failed: before a file (nothing more touched) or after (the move is in moved)
    record: str = ""                # the record this operation wrote, when the service opened one of its own

    @property
    def bytes_freed(self) -> int:
        return self._bytes

    _bytes: int = 0


def same_bytes(one: str, two: str) -> bool:
    """True when the two files hold the same bytes now, read in full and afresh (the index's marks may be
    stale, and so may a cached comparison: filecmp remembers its answers by size and time, which a file can
    keep while its bytes change)."""
    try:
        if os.path.getsize(one) != os.path.getsize(two):
            return False
        with open(one, "rb") as first, open(two, "rb") as second:
            while True:
                a, b = first.read(1 << 20), second.read(1 << 20)
                if a != b:
                    return False
                if not a:
                    return True
    except OSError:
        return False


def remove_extras(found: Report, mover: Callable[[str], str], note_missing: Callable[[str], None],
                  stop=None, tell: Optional[Callable[[int, int, str], None]] = None,
                  journal: Optional[Callable[[str], None]] = None) -> Removal:
    """Move every "extra" of the report to the trash, one by one, each only after its keeper is found to be
    there and to hold the same bytes at that moment. `journal(line)` is told of each outcome as it happens
    (moved or left, as the record file writes them), so a run cut short still has its record so far.
    `mover(path)` does the moving and answers "" or why
    not; `note_missing(path)` tells the index. A stop request ends it between files; nothing is undone."""
    done = Removal()
    total = found.extra_files
    at = 0
    for same in found.sets:
        keeper = same.keep.record.path
        for extra in same.extra:
            if stop is not None and stop.is_set():
                done.stopped = True
                return done
            at += 1
            path = extra.record.path
            if tell is not None:
                tell(at, total, path)
            why = ""
            if not os.path.isfile(keeper):
                why = f"the copy to keep is not there now: {keeper}"
            elif not os.path.isfile(path):
                why = "it is not there now"
            elif not same_bytes(keeper, path):
                why = "it no longer holds the same bytes as the copy to keep"
            else:
                if journal is not None:      # the intent, naming the copy kept, on disk before the move
                    try:
                        journal(record_line("moving", path, "kept", keeper))
                    except RecordError as exc:
                        done.skipped.append((path, f"the record could not be written ({exc}); nothing more was moved"))
                        done.record_problem, done.stopped = str(exc), True
                        return done
                why = mover(path)
            if why:
                done.skipped.append((path, why))
                if journal is not None:
                    try:
                        journal(record_line("left", path, why))
                    except RecordError as exc:
                        done.record_problem, done.stopped = str(exc), True
                        return done
                continue
            done.moved.append((path, keeper))
            done._bytes += extra.record.size
            note_missing(path)
            if journal is not None:
                try:
                    journal(record_line("moved", path, "kept", keeper))
                except RecordError as exc:     # moved, and in `moved`; nothing more without a record
                    done.record_problem, done.stopped = str(exc), True
                    return done
    return done


def record_line(*parts: str) -> str:
    """One line of the removal record: tab-separated, no line breaks inside a part."""
    return "\t".join(" ".join(str(p).split()) for p in parts)
