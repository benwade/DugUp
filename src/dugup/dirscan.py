"""DirectoryScan: the default FileSource (guide decision D2). Walks a folder with os.scandir.

What it promises, beyond the FileSource contract:
- It reads names, sizes and times from the directory listing itself, with no extra system call per file
  on Linux. On Windows the directory listing carries no file id, so a full `scan` with `collect_ids` on asks
  the system once per file. That is paid once: `changes` asks only for paths that are new or different,
  because only those can be part of a move (measured by tests/test_dirscan_private.py: about 0.02 ms per
  file on local disks and 0.35 ms on a Google Drive folder).
- Symbolic links and junctions are not followed, so a loop cannot happen. They are counted in `last_stats`.
- A folder that cannot be listed is reported in `Listing.skipped`, never dropped silently; a file that
  disappears between the listing and the stat call is simply gone and is not reported.
- An excluded folder is never entered.
- Cloud placeholders (Windows) are listed with `offline=True`; stat does not download them.
- It never writes anything.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

from .filesource import compute_changes, differs, excluded, matches_rules
from .protocols import ChangeSet, FileEntry, Listing, ScanRules, Skipped, StopSignal
from .shapes import Failure, FailureReason

# Windows file attribute bits that mark a cloud placeholder (content not on this machine).
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
OFFLINE_BITS = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

LONG_PATH_THRESHOLD = 240   # Windows: from this length on, paths get the \\?\ prefix for the system calls


def long_path(path: str) -> str:
    """The form of `path` to hand to the operating system. Only Windows changes it, and only when it is long."""
    if os.name != "nt" or len(path) < LONG_PATH_THRESHOLD or path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _describe(error: OSError) -> str:
    return f"{type(error).__name__}: {error.strerror or error}"


def _is_link(entry) -> bool:
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None:
        return bool(is_junction())
    # Python before 3.12 cannot tell a junction from other reparse points; treat reparse folders as links.
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT) and entry.is_dir(follow_symlinks=False)


class DirectoryScan:
    def __init__(self, collect_ids: bool = True, scandir: Callable = os.scandir, stat: Callable = os.stat) -> None:
        self.collect_ids = collect_ids
        self._scandir = scandir
        self._stat = stat
        self.last_stats: Dict[str, int] = {}   # counts only; "id_lookups" is the extra calls made for file ids

    # -- the contract

    def reachable(self, root: str) -> bool:
        try:
            with self._scandir(long_path(root)):
                return True
        except OSError:
            return False

    def scan(self, root: str, rules: ScanRules, stop: Optional[StopSignal] = None) -> "Listing | Failure":
        """The full listing. With `collect_ids` on, every file gets an id (the one-time cost on Windows)."""
        return self._walk(root, rules, stop, ids_for_all=self.collect_ids)

    def changes(self, root: str, previous, rules: ScanRules, stop: Optional[StopSignal] = None
                ) -> "ChangeSet | Failure":
        """Compared with `previous`. Ids are looked up only for paths that are new or different, so a repeat scan
        costs the same as a scan without ids. Unchanged files in `previous` keep whatever id they already had."""
        listing = self._walk(root, rules, stop, ids_for_all=False)
        if isinstance(listing, Failure):
            return listing
        entries = listing.entries
        if self.collect_ids:
            entries = tuple(self._with_id(e) if e.file_id is None and self._needs_id(e, previous) else e
                            for e in entries)
        return compute_changes(previous, entries, listing.skipped)

    def _needs_id(self, entry: FileEntry, previous) -> bool:
        old = previous.get(entry.path)
        return old is None or differs(old, entry)

    def _with_id(self, entry: FileEntry) -> FileEntry:
        """The entry with its id asked from the system. If that fails the id stays missing; nothing is reported."""
        self.last_stats["id_lookups"] = self.last_stats.get("id_lookups", 0) + 1
        try:
            info = self._stat(long_path(entry.path), follow_symlinks=False)
        except OSError:
            return entry
        file_id = f"{info.st_dev}:{info.st_ino}" if info.st_ino else None
        return replace(entry, file_id=file_id) if file_id else entry

    def _walk(self, root: str, rules: ScanRules, stop, ids_for_all: bool) -> "Listing | Failure":
        stats = {"folders": 0, "files": 0, "links_skipped": 0, "skipped": 0, "vanished": 0, "id_lookups": 0}
        self.last_stats = stats
        entries: List[FileEntry] = []
        skipped: List[Skipped] = []
        stack: List[Tuple[str, str]] = [(root, "")]   # (folder as the caller names it, relative path with "/")
        is_root = True
        while stack:
            if stop is not None and stop.is_set():
                return Failure(FailureReason.STOPPED, "stopped while scanning")
            folder, rel_folder = stack.pop()
            try:
                iterator = self._scandir(long_path(folder))
            except OSError as error:
                if is_root:
                    return Failure(FailureReason.UNREACHABLE, f"cannot read {root} ({_describe(error)})")
                skipped.append(Skipped(folder, _describe(error)))
                stats["skipped"] += 1
                continue
            is_root = False
            stats["folders"] += 1
            with iterator:
                try:
                    for entry in iterator:
                        if stop is not None and stop.is_set():
                            return Failure(FailureReason.STOPPED, "stopped while scanning")
                        self._take(entry, folder, rel_folder, rules, stack, entries, skipped, stats, ids_for_all)
                except OSError as error:   # the listing broke part way: say so, keep what was read
                    skipped.append(Skipped(folder, _describe(error)))
                    stats["skipped"] += 1
        return Listing(tuple(entries), tuple(skipped))

    # -- one directory entry

    def _take(self, entry, folder, rel_folder, rules, stack, entries, skipped, stats, ids_for_all) -> None:
        name = entry.name
        rel = f"{rel_folder}/{name}" if rel_folder else name
        path = os.path.join(folder, name)
        try:
            if _is_link(entry):
                stats["links_skipped"] += 1
                return
            if entry.is_dir(follow_symlinks=False):
                if not excluded(rel, rules):
                    stack.append((path, rel))
                return
            if not entry.is_file(follow_symlinks=False) or not matches_rules(rel, rules):
                return
            info = entry.stat(follow_symlinks=False)
            file_id = self._file_id(info, path, stats, ask=ids_for_all) if self.collect_ids else None
        except FileNotFoundError:
            stats["vanished"] += 1   # gone between the listing and now: it is simply not there
            return
        except OSError as error:
            skipped.append(Skipped(path, _describe(error)))
            stats["skipped"] += 1
            return
        offline = bool(getattr(info, "st_file_attributes", 0) & OFFLINE_BITS)
        entries.append(FileEntry(path, info.st_size, info.st_mtime, file_id, offline))
        stats["files"] += 1

    def _file_id(self, info, path: str, stats, ask: bool) -> Optional[str]:
        """"device:index" as text. Windows directory listings carry no index; with `ask` on, ask the system once."""
        if not info.st_ino:
            if not ask:
                return None
            stats["id_lookups"] += 1
            info = self._stat(long_path(path), follow_symlinks=False)
        if not info.st_ino:
            return None   # this file system gives no usable id
        return f"{info.st_dev}:{info.st_ino}"
