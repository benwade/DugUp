"""A real-folder harness for the FileSource conformance checks: everything happens in a temporary folder."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Set

from dugup.dirscan import DirectoryScan


class DirScanHarness:
    supports_file_ids = True
    supports_offline = False             # a real cloud placeholder cannot be made here; tested with injected entries
    supports_unreadable_folders = True   # simulated by making the injected scandir refuse that folder

    def __init__(self, base: Path, collect_ids: bool = True) -> None:
        self.base = Path(base)
        self.blocked: Set[str] = set()
        self._count = 0
        self._clock = 0
        self.source = DirectoryScan(collect_ids=collect_ids, scandir=self._scandir)

    def _scandir(self, path):
        if os.path.normcase(os.fspath(path)) in self.blocked:
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return os.scandir(path)

    # -- the harness interface
    def new_root(self) -> str:
        self._count += 1
        root = self.base / f"root{self._count}"
        root.mkdir()
        return str(root)

    def nonexistent_root(self) -> str:
        return str(self.base / "never-created")

    def full_path(self, root: str, rel: str) -> str:
        return os.path.join(root, *rel.split("/"))

    def put(self, root: str, rel: str, content: bytes = b"x", offline: bool = False) -> str:
        path = self.full_path(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        self._clock += 1
        stamp = 1_700_000_000 + self._clock   # a rewrite always gets a new time, whatever the clock resolution
        os.utime(path, (stamp, stamp))
        return path

    def rename(self, root: str, old: str, new: str) -> None:
        target = self.full_path(root, new)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.rename(self.full_path(root, old), target)

    def delete(self, root: str, rel: str) -> None:
        os.remove(self.full_path(root, rel))

    def clear(self, root: str) -> None:
        for child in Path(root).iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def make_unreachable(self, root: str) -> None:
        os.rename(root, root + ".away")

    def make_reachable(self, root: str) -> None:
        os.rename(root + ".away", root)

    def make_folder_unreadable(self, root: str, rel: str) -> None:
        self.blocked.add(os.path.normcase(self.full_path(root, rel)))

    def make_folder_readable(self, root: str, rel: str) -> None:
        self.blocked.discard(os.path.normcase(self.full_path(root, rel)))

    def snapshot(self, root: str):
        """Names, kinds, sizes and modified times of everything below root (reading must not change any)."""
        out = []
        for current, folders, files in os.walk(root):
            for name in folders + files:
                path = os.path.join(current, name)
                info = os.lstat(path)
                out.append((os.path.relpath(path, root), os.path.isdir(path), info.st_size, info.st_mtime_ns))
        out.sort()
        return out


class _NoIndex:
    """A directory entry whose listing carries no file index, as on Windows (the real stat call still has it)."""

    def __init__(self, entry):
        self._entry = entry

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def stat(self, follow_symlinks=True):
        return _NoIndexStat(self._entry.stat(follow_symlinks=follow_symlinks))


class _NoIndexStat:
    def __init__(self, info):
        self._info = info
        self.st_ino = 0

    def __getattr__(self, name):
        return getattr(self._info, name)


class _NoIndexListing:
    def __init__(self, listing):
        self._listing = listing

    def __enter__(self):
        self._listing.__enter__()
        return self

    def __exit__(self, *exc):
        return self._listing.__exit__(*exc)

    def __iter__(self):
        for entry in self._listing:
            yield _NoIndex(entry)


class NoIndexHarness(DirScanHarness):
    """Real folders, but directory listings never carry the file index, so ids need the extra lookup."""

    def _scandir(self, path):
        return _NoIndexListing(super()._scandir(path))
