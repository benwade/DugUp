"""An in-memory FileSource over an in-memory "disk", plus deliberately broken variants.

The disk belongs to the harness (the test); the source only reads it. That is what makes the
"never writes to the folders it reads" check meaningful.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Set, Tuple

from dugup.filesource import compute_changes, matches_rules
from dugup.protocols import ChangeSet, FileEntry, Listing, ScanRules, Skipped, StopSignal
from dugup.shapes import Failure, FailureReason


@dataclass
class FakeFile:
    content: bytes
    modified: float
    file_id: str
    offline: bool = False


class FakeDisk:
    def __init__(self) -> None:
        self.roots: Dict[str, Dict[str, FakeFile]] = {}
        self.unreachable: Set[str] = set()
        self.unreadable: Dict[str, Set[str]] = {}   # root -> folders (relative) that cannot be read
        self.next_id = 1
        self.clock = 0


def full_path(root: str, rel: str) -> str:
    return f"{root}/{rel}"


class FakeFileSource:
    def __init__(self, disk: FakeDisk) -> None:
        self.disk = disk

    def reachable(self, root: str) -> bool:
        return root in self.disk.roots and root not in self.disk.unreachable

    def _unreachable(self, root: str) -> Failure:
        return Failure(FailureReason.UNREACHABLE, f"the folder {root} cannot be reached right now")

    def _listing(self, root: str, rules: ScanRules, stop: Optional[StopSignal]):
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped before the scan began")
        if not self.reachable(root):
            return self._unreachable(root)
        entries = []
        locked = self.disk.unreadable.get(root, set())
        for rel in sorted(self.disk.roots[root]):
            if stop is not None and stop.is_set():
                return Failure(FailureReason.STOPPED, "stopped part way through the scan")
            if not matches_rules(rel, rules):
                continue
            if any(rel.startswith(folder + "/") for folder in locked):
                continue
            f = self.disk.roots[root][rel]
            entries.append(FileEntry(full_path(root, rel), len(f.content), f.modified, f.file_id, f.offline))
        skipped = tuple(Skipped(full_path(root, folder), "permission denied") for folder in sorted(locked))
        return Listing(tuple(entries), skipped)

    def scan(self, root: str, rules: ScanRules, stop: Optional[StopSignal] = None):
        return self._listing(root, rules, stop)

    def changes(self, root: str, previous: Mapping[str, FileEntry], rules: ScanRules,
                stop: Optional[StopSignal] = None):
        listing = self._listing(root, rules, stop)
        if isinstance(listing, Failure):
            return listing
        return compute_changes(previous, listing.entries, listing.skipped)


# ---- deliberately broken variants: each must be caught by the check named in BROKEN_FILESOURCES ---------

class EmptyWhenUnreachable(FakeFileSource):
    """Reports an unreachable folder as an empty one (the worst FileSource failure)."""

    def _listing(self, root, rules, stop):
        if root in self.disk.unreachable or root not in self.disk.roots:
            return Listing()
        return super()._listing(root, rules, stop)


class RemovedWhenUnreachable(FakeFileSource):
    """scan() is honest, but changes() says everything was removed when the folder is unreachable."""

    def changes(self, root, previous, rules, stop=None):
        if not self.reachable(root):
            return ChangeSet(removed=tuple(sorted(previous)))
        return super().changes(root, previous, rules, stop)


class DropsWarnings(FakeFileSource):
    def changes(self, root, previous, rules, stop=None):
        result = super().changes(root, previous, rules, stop)
        if isinstance(result, ChangeSet):
            return ChangeSet(result.added, result.changed, result.removed, result.moved, ())
        return result


class IgnoresFileIds(FakeFileSource):
    """Never recognises a moved file: it is always one removal plus one addition."""

    def _listing(self, root, rules, stop):
        result = super()._listing(root, rules, stop)
        if isinstance(result, Listing):
            return Listing(tuple(FileEntry(e.path, e.size, e.modified, None, e.offline) for e in result.entries), result.skipped)
        return result


class WritesToTree(FakeFileSource):
    """Leaves a marker file behind in the folder it scans."""

    def _listing(self, root, rules, stop):
        result = super()._listing(root, rules, stop)
        if isinstance(result, Listing) and ".dugup-scan" not in self.disk.roots[root]:
            self.disk.roots[root][".dugup-scan"] = FakeFile(b"scanned", 0.0, "marker")
        return result


class IgnoresStop(FakeFileSource):
    def _listing(self, root, rules, stop):
        return super()._listing(root, rules, None)


class StopsOnlyAtStart(FakeFileSource):
    """Looks at the stop request once, before it starts, and never again."""

    def _listing(self, root, rules, stop):
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped before the scan began")
        return super()._listing(root, rules, None)


class IgnoresRules(FakeFileSource):
    def _listing(self, root, rules, stop):
        return super()._listing(root, ScanRules(), stop)


class SilentlySkipsFolders(FakeFileSource):
    """Leaves out folders it cannot read without saying so."""

    def _listing(self, root, rules, stop):
        result = super()._listing(root, rules, stop)
        if isinstance(result, Listing):
            return Listing(result.entries, ())
        return result


class RemovesFilesBelowSkippedFolders(FakeFileSource):
    """Names the folders it could not read, but then reports everything known below them as removed."""

    def changes(self, root, previous, rules, stop=None):
        listing = self._listing(root, rules, stop)
        if isinstance(listing, Failure):
            return listing
        return compute_changes(previous, listing.entries)


class DropsOfflineFlag(FakeFileSource):
    def _listing(self, root, rules, stop):
        result = super()._listing(root, rules, stop)
        if isinstance(result, Listing):
            return Listing(tuple(FileEntry(e.path, e.size, e.modified, e.file_id, False) for e in result.entries), result.skipped)
        return result


class RaisesWhenUnreachable(FakeFileSource):
    def _unreachable(self, root):
        raise OSError(f"cannot reach {root}")


class Harness:
    """What the conformance checks drive. A real harness would use a temporary folder instead."""

    supports_file_ids = True
    supports_offline = True
    supports_unreadable_folders = True

    def __init__(self, source_class=FakeFileSource) -> None:
        self.disk = FakeDisk()
        self.source = source_class(self.disk)
        self._roots = 0

    def new_root(self) -> str:
        self._roots += 1
        root = f"fake:/root{self._roots}"
        self.disk.roots[root] = {}
        return root

    def nonexistent_root(self) -> str:
        return "fake:/nowhere"

    def full_path(self, root: str, rel: str) -> str:
        return full_path(root, rel)

    def put(self, root: str, rel: str, content: bytes = b"x", offline: bool = False) -> str:
        files = self.disk.roots[root]
        self.disk.clock += 1
        stamp = 1_700_000_000.0 + self.disk.clock
        if rel in files:
            files[rel].content, files[rel].modified = content, stamp
        else:
            files[rel] = FakeFile(content, stamp, f"id{self.disk.next_id}", offline)
            self.disk.next_id += 1
        return full_path(root, rel)

    def rename(self, root: str, old: str, new: str) -> None:
        files = self.disk.roots[root]
        files[new] = files.pop(old)

    def delete(self, root: str, rel: str) -> None:
        del self.disk.roots[root][rel]

    def clear(self, root: str) -> None:
        self.disk.roots[root].clear()

    def make_unreachable(self, root: str) -> None:
        self.disk.unreachable.add(root)

    def make_reachable(self, root: str) -> None:
        self.disk.unreachable.discard(root)

    def make_folder_unreadable(self, root: str, rel: str) -> None:
        self.disk.unreadable.setdefault(root, set()).add(rel)

    def make_folder_readable(self, root: str, rel: str) -> None:
        self.disk.unreadable.get(root, set()).discard(rel)

    def snapshot(self, root: str):
        """Everything about the tree that a read could disturb, in a form that compares equal."""
        return copy.deepcopy((self.disk.roots.get(root), root in self.disk.unreachable))
