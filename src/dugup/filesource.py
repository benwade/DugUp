"""Pure helpers every FileSource shares: include/exclude rules and the comparison of two listings.

No disk access here. A directory scanner and a watcher both feed their listings into `compute_changes`,
so "what counts as changed or moved" is decided in one place.
"""

from __future__ import annotations

import fnmatch
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from .protocols import (WARN_MASS_LOSS, WARN_ROOT_EMPTY, ChangeSet, FileEntry, ScanRules, Skipped)

# GUESSES, to be tuned on real data: a listing of at least this many files that loses at least
# this fraction in one pass is reported with the "mass_loss" warning (the files are still reported as removed).
MASS_LOSS_MIN_PREVIOUS = 20
MASS_LOSS_FRACTION = 0.5

# A PROPOSAL, not applied by anything: folders that are almost never worth indexing (Phase 0.2 found 75 to 91
# percent of the files on the first real machine in folders like these). The user's own ignore list decides; this is only a starting point.
SUGGESTED_EXCLUDES = (".git", ".venv", "venv", "node_modules", "__pycache__", "$RECYCLE.BIN", "System Volume Information")


def _norm(text: str) -> str:
    return text.replace("\\", "/").strip("/").casefold()


def _hit(pattern: str, relative: str) -> bool:
    anchored = pattern.replace("\\", "/").lstrip().startswith("/")
    pattern = _norm(pattern)
    if not pattern:
        return False
    parts = relative.split("/")
    candidates = [relative]
    candidates += ["/".join(parts[:i]) for i in range(1, len(parts))]   # every folder above the file
    if not anchored:   # a pattern that starts with "/" names one place below the root, not a name found anywhere
        candidates += [parts[-1]] + parts[:-1]                          # the file name, and each folder name alone
    return any(fnmatch.fnmatchcase(c, pattern) for c in candidates)


def excluded(relative_path: str, rules: ScanRules) -> bool:
    """True when a file or folder at `relative_path` is excluded. A scanner uses it to avoid entering a folder."""
    relative = _norm(relative_path)
    return bool(relative) and any(_hit(p, relative) for p in rules.exclude)


def matches_rules(relative_path: str, rules: ScanRules) -> bool:
    """True when a file at `relative_path` (below the root) is wanted under `rules`."""
    relative = _norm(relative_path)
    if not relative:
        return False
    if excluded(relative, rules):
        return False
    if rules.include and not any(_hit(p, relative) for p in rules.include):
        return False
    return True


def differs(a: FileEntry, b: FileEntry) -> bool:
    """True when the same path now has a different size or modified time, or when a cloud placeholder
    has been downloaded (or sent back to the cloud): its content can be read now when it could not before."""
    return a.size != b.size or a.modified != b.modified or a.offline != b.offline


def _under(path: str, folder: str) -> bool:
    base = folder.rstrip("/\\")
    return path == base or path.startswith(base + "/") or path.startswith(base + "\\")


def compute_changes(previous: Mapping[str, FileEntry], current: Iterable[FileEntry],
                    skipped: Sequence[Skipped] = ()) -> ChangeSet:
    """Compare an earlier listing (keyed by path) with a new one.

    - added: a path not in `previous`; removed: a path no longer listed.
    - changed: same path, different size or modified time.
    - moved: a removed path and an added path that carry the same, non-empty file id (only when exactly
      one of each, so an ambiguous id never guesses). A moved file whose size or time also differ is
      listed under `changed` as well, at its new path.
    - skipped: folders the scan could not read. Nothing below one is reported as removed or moved, and
      those files do not count toward the warnings.
    - warnings: "root_empty" when nothing is listed now but something was before; otherwise "mass_loss"
      when a sizeable listing lost at least half its files in one pass.
    """
    now: Dict[str, FileEntry] = {}
    for entry in current:
        now[entry.path] = entry
    if skipped:
        previous = {p: e for p, e in previous.items() if p in now or not any(_under(p, s.path) for s in skipped)}
    added: List[FileEntry] = [e for p, e in now.items() if p not in previous]
    removed: List[str] = [p for p in previous if p not in now]
    changed: List[FileEntry] = [e for p, e in now.items() if p in previous and differs(previous[p], e)]

    removed_by_id: Dict[str, List[str]] = {}
    for path in removed:
        fid = previous[path].file_id
        if fid:
            removed_by_id.setdefault(fid, []).append(path)
    added_by_id: Dict[str, List[FileEntry]] = {}
    for entry in added:
        if entry.file_id:
            added_by_id.setdefault(entry.file_id, []).append(entry)

    moved: List[Tuple[str, FileEntry]] = []
    moved_old: set = set()
    moved_new: set = set()
    for fid, old_paths in removed_by_id.items():
        new_entries = added_by_id.get(fid, [])
        if len(old_paths) == 1 and len(new_entries) == 1:
            old, new = old_paths[0], new_entries[0]
            moved.append((old, new))
            moved_old.add(old)
            moved_new.add(new.path)
            if differs(previous[old], new):
                changed.append(new)
    added = [e for e in added if e.path not in moved_new]
    removed = [p for p in removed if p not in moved_old]

    warnings: List[str] = []
    if not now and previous:
        warnings.append(WARN_ROOT_EMPTY)
    elif len(previous) >= MASS_LOSS_MIN_PREVIOUS and len(removed) >= MASS_LOSS_FRACTION * len(previous):
        warnings.append(WARN_MASS_LOSS)

    return ChangeSet(tuple(sorted(added, key=lambda e: e.path)), tuple(sorted(changed, key=lambda e: e.path)),
                     tuple(sorted(removed)), tuple(sorted(moved, key=lambda m: m[0])), tuple(warnings),
                     tuple(sorted(skipped, key=lambda x: x.path)))
