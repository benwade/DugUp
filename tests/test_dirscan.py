"""DirectoryScan behaviors that the shared checks do not cover.

Most use injected directory entries, so they run the same on Windows and Linux and need no special rights.
"""

import os
import shutil
import subprocess
import threading

import pytest

import helpers
from dugup import dirscan
from dugup.dirscan import DirectoryScan, long_path
from dugup.protocols import ChangeSet, Listing, ScanRules
from dugup.shapes import Failure, FailureReason

NO_RULES = ScanRules()


# ---- injected entries ----------------------------------------------------------------------------------

class Info:
    def __init__(self, size=10, mtime=100.0, ino=1, dev=7, attributes=0):
        self.st_size, self.st_mtime, self.st_ino, self.st_dev = size, mtime, ino, dev
        if attributes is not None:
            self.st_file_attributes = attributes


class Entry:
    def __init__(self, name, kind="file", info=None, stat_error=None):
        self.name, self.kind, self.info, self.stat_error = name, kind, info or Info(), stat_error

    def is_symlink(self):
        return self.kind == "symlink"

    def is_junction(self):
        return self.kind == "junction"

    def is_dir(self, follow_symlinks=True):
        return self.kind == "dir"

    def is_file(self, follow_symlinks=True):
        return self.kind == "file"

    def stat(self, follow_symlinks=True):
        if self.stat_error is not None:
            raise self.stat_error
        return self.info


class Listed:
    def __init__(self, items):
        self.items = items

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for item in self.items:
            if isinstance(item, BaseException):
                raise item
            yield item


class Tree:
    """A fake file system: path -> list of Entry (an exception in the list is raised when reached)."""

    def __init__(self, folders):
        self.folders = {os.path.normcase(k): v for k, v in folders.items()}
        self.opened = []

    def scandir(self, path):
        key = os.path.normcase(os.fspath(path))
        self.opened.append(os.fspath(path))   # as asked for, so tests compare it with the path they built
        content = self.folders.get(key, FileNotFoundError(2, "No such file or directory", key))
        if isinstance(content, BaseException):
            raise content
        return Listed(content)


def scanner(tree, **kwargs):
    return DirectoryScan(scandir=tree.scandir, stat=kwargs.pop("stat", lambda *a, **k: Info()), **kwargs)


def p(*parts):
    return os.path.join("R", *parts)


def names(listing):
    return sorted(os.path.relpath(e.path, "R").replace(os.sep, "/") for e in listing.entries)


def test_links_and_junctions_are_counted_but_never_followed():
    tree = Tree({"R": [Entry("real", "dir"), Entry("loop", "symlink"), Entry("jct", "junction"), Entry("f.txt")],
                 p("real"): [Entry("inside.txt")]})
    source = scanner(tree)
    listing = source.scan("R", NO_RULES)
    assert names(listing) == ["f.txt", "real/inside.txt"]
    assert source.last_stats["links_skipped"] == 2
    assert p("loop") not in tree.opened and p("jct") not in tree.opened


def test_an_excluded_folder_is_never_entered_but_include_patterns_do_not_prune():
    tree = Tree({"R": [Entry("node_modules", "dir"), Entry("docs", "dir")],
                 p("node_modules"): [Entry("x.js")], p("docs"): [Entry("a.pdf"), Entry("b.txt")]})
    listing = scanner(tree).scan("R", ScanRules(exclude=("node_modules",)))
    assert names(listing) == ["docs/a.pdf", "docs/b.txt"]
    assert p("node_modules") not in tree.opened
    tree.opened.clear()
    only_pdf = scanner(tree).scan("R", ScanRules(include=("*.pdf",)))
    assert names(only_pdf) == ["docs/a.pdf"]
    assert p("node_modules") in tree.opened   # an include pattern cannot rule a folder out


def test_a_folder_that_cannot_be_read_is_named_and_its_neighbours_are_still_scanned():
    tree = Tree({"R": [Entry("locked", "dir"), Entry("open", "dir")],
                 p("locked"): PermissionError(13, "Access is denied"), p("open"): [Entry("b.txt")]})
    source = scanner(tree)
    listing = source.scan("R", NO_RULES)
    assert names(listing) == ["open/b.txt"]
    assert [s.path for s in listing.skipped] == [p("locked")]
    assert "PermissionError" in listing.skipped[0].reason and "denied" in listing.skipped[0].reason.lower()
    assert source.last_stats["skipped"] == 1


def test_a_listing_that_breaks_part_way_keeps_what_was_read_and_names_the_folder():
    tree = Tree({"R": [Entry("a.txt"), OSError(5, "Input/output error"), Entry("never-seen.txt")]})
    listing = scanner(tree).scan("R", NO_RULES)
    assert names(listing) == ["a.txt"]
    assert [s.path for s in listing.skipped] == ["R"]


def test_an_unreadable_root_is_unreachable_not_empty():
    tree = Tree({"R": PermissionError(13, "Access is denied")})
    result = scanner(tree).scan("R", NO_RULES)
    assert isinstance(result, Failure) and result.reason is FailureReason.UNREACHABLE and result.detail
    assert not scanner(tree).reachable("R")


def test_a_file_that_vanishes_between_listing_and_stat_is_not_reported_as_a_problem():
    tree = Tree({"R": [Entry("gone.txt", stat_error=FileNotFoundError(2, "gone")), Entry("here.txt")]})
    source = scanner(tree)
    listing = source.scan("R", NO_RULES)
    assert names(listing) == ["here.txt"] and listing.skipped == ()
    assert source.last_stats["vanished"] == 1


def test_a_file_that_cannot_be_examined_is_named():
    tree = Tree({"R": [Entry("odd.txt", stat_error=PermissionError(13, "Access is denied")), Entry("ok.txt")]})
    listing = scanner(tree).scan("R", NO_RULES)
    assert names(listing) == ["ok.txt"] and [s.path for s in listing.skipped] == [p("odd.txt")]


# ---- file ids and offline flags ------------------------------------------------------------------------

def test_the_id_comes_from_the_listing_when_it_has_one_and_no_extra_call_is_made():
    def boom(*a, **k):
        raise AssertionError("an extra stat call was made")
    tree = Tree({"R": [Entry("a.txt", info=Info(ino=42, dev=9))]})
    assert scanner(tree, stat=boom).scan("R", NO_RULES).entries[0].file_id == "9:42"


def test_a_listing_without_an_id_asks_the_system_once_per_file():
    calls = []

    def ask(path, follow_symlinks=True):
        calls.append(path)
        return Info(ino=77, dev=3)
    tree = Tree({"R": [Entry("a.txt", info=Info(ino=0)), Entry("b.txt", info=Info(ino=0))]})
    listing = scanner(tree, stat=ask).scan("R", NO_RULES)
    assert [e.file_id for e in listing.entries] == ["3:77", "3:77"] and len(calls) == 2


def test_a_zero_id_from_the_system_means_no_id():
    tree = Tree({"R": [Entry("a.txt", info=Info(ino=0))]})
    assert scanner(tree, stat=lambda *a, **k: Info(ino=0)).scan("R", NO_RULES).entries[0].file_id is None


def test_switching_ids_off_makes_no_extra_calls_and_gives_none():
    def boom(*a, **k):
        raise AssertionError("stat was called")
    tree = Tree({"R": [Entry("a.txt", info=Info(ino=0))]})
    assert scanner(tree, stat=boom, collect_ids=False).scan("R", NO_RULES).entries[0].file_id is None


@pytest.mark.parametrize("bit", [dirscan.FILE_ATTRIBUTE_OFFLINE, dirscan.FILE_ATTRIBUTE_RECALL_ON_OPEN,
                                 dirscan.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS])
def test_each_cloud_placeholder_bit_marks_a_file_offline(bit):
    tree = Tree({"R": [Entry("cloud.doc", info=Info(attributes=bit | 0x20)), Entry("local.doc", info=Info(attributes=0x20))]})
    flags = {os.path.basename(e.path): e.offline for e in scanner(tree).scan("R", NO_RULES).entries}
    assert flags == {"cloud.doc": True, "local.doc": False}


def test_a_system_without_file_attributes_never_marks_anything_offline():
    tree = Tree({"R": [Entry("a.doc", info=Info(attributes=None))]})
    assert scanner(tree).scan("R", NO_RULES).entries[0].offline is False


def test_size_and_time_come_from_the_listing():
    tree = Tree({"R": [Entry("a.txt", info=Info(size=123, mtime=456.5))]})
    e = scanner(tree).scan("R", NO_RULES).entries[0]
    assert (e.size, e.modified) == (123, 456.5)


def test_stats_are_counts_only():
    tree = Tree({"R": [Entry("d", "dir"), Entry("a.txt"), Entry("l", "symlink")], p("d"): [Entry("b.txt")]})
    source = scanner(tree)
    source.scan("R", NO_RULES)
    assert source.last_stats == {"folders": 2, "files": 2, "links_skipped": 1, "skipped": 0, "vanished": 0, "id_lookups": 0, "tool_skipped": 0}


def test_stop_is_looked_at_between_entries():
    tree = Tree({"R": [Entry(f"f{i}.txt") for i in range(100)]})
    looks = {"n": 0}

    class Stop:
        def is_set(self):
            looks["n"] += 1
            return looks["n"] > 10

        def wait(self, timeout=None):
            return self.is_set()
    result = scanner(tree).scan("R", NO_RULES, Stop())
    assert isinstance(result, Failure) and result.reason is FailureReason.STOPPED and looks["n"] <= 12


# ---- ids in changes(): only for paths that are new or different -----------------------------------------

class Lookups:
    """A stand-in for os.stat that answers by path and remembers what it was asked."""

    def __init__(self, ids=None, errors=None):
        self.ids, self.errors, self.asked = ids or {}, errors or {}, []

    def __call__(self, path, follow_symlinks=True):
        self.asked.append(os.path.basename(path))
        name = os.path.basename(path)
        if name in self.errors:
            raise self.errors[name]
        return Info(ino=self.ids.get(name, 0), dev=5)


def zero_id_tree(*entries):
    return Tree({"R": [Entry(name, info=Info(size=size, mtime=mtime, ino=0)) for name, size, mtime in entries]})


def previous_of(listing):
    return {e.path: e for e in listing.entries}


def test_a_repeat_scan_with_nothing_new_asks_for_no_ids_at_all():
    tree = zero_id_tree(("a.txt", 1, 1.0), ("b.txt", 2, 2.0))
    first = scanner(tree, stat=lambda *a, **k: Info(ino=9)).scan("R", NO_RULES)
    assert all(e.file_id for e in first.entries)
    lookups = Lookups()
    source = scanner(tree, stat=lookups)
    assert source.changes("R", previous_of(first), NO_RULES) == ChangeSet()
    assert lookups.asked == [] and source.last_stats["id_lookups"] == 0


def test_a_new_file_gets_an_id_and_so_does_a_changed_one_but_unchanged_files_do_not():
    old = zero_id_tree(("keep.txt", 1, 1.0), ("edit.txt", 1, 1.0))
    first = scanner(old, stat=lambda *a, **k: Info(ino=9)).scan("R", NO_RULES)
    now = zero_id_tree(("keep.txt", 1, 1.0), ("edit.txt", 5, 1.0), ("new.txt", 1, 1.0))
    lookups = Lookups(ids={"edit.txt": 41, "new.txt": 42})
    source = scanner(now, stat=lookups)
    result = source.changes("R", previous_of(first), NO_RULES)
    assert sorted(lookups.asked) == ["edit.txt", "new.txt"] and source.last_stats["id_lookups"] == 2
    assert [e.file_id for e in result.added] == ["5:42"] and [e.file_id for e in result.changed] == ["5:41"]


def test_a_move_is_still_recognised_when_only_the_new_path_is_asked_for_its_id():
    old = Tree({"R": [Entry("sub", "dir"), Entry("keep.txt", info=Info(ino=0))], p("sub"): [Entry("old.txt", info=Info(ino=0))]})
    first = scanner(old, stat=Lookups(ids={"old.txt": 77, "keep.txt": 78})).scan("R", NO_RULES)
    now = Tree({"R": [Entry("sub", "dir"), Entry("keep.txt", info=Info(ino=0))], p("sub"): [Entry("moved.txt", info=Info(ino=0))]})
    lookups = Lookups(ids={"moved.txt": 77})
    result = scanner(now, stat=lookups).changes("R", previous_of(first), NO_RULES)
    assert lookups.asked == ["moved.txt"]
    assert [(os.path.basename(o), os.path.basename(n.path)) for o, n in result.moved] == [("old.txt", "moved.txt")]
    assert result.added == () and result.removed == ()


def test_ids_that_the_listing_already_has_are_kept_without_any_lookup():
    def boom(*a, **k):
        raise AssertionError("an extra stat call was made")
    now = Tree({"R": [Entry("new.txt", info=Info(ino=42, dev=9))]})
    result = scanner(now, stat=boom).changes("R", {}, NO_RULES)
    assert [e.file_id for e in result.added] == ["9:42"]


def test_a_failed_id_lookup_leaves_the_id_missing_and_reports_nothing():
    now = zero_id_tree(("gone.txt", 1, 1.0), ("locked.txt", 1, 1.0), ("fine.txt", 1, 1.0))
    lookups = Lookups(ids={"fine.txt": 3}, errors={"gone.txt": FileNotFoundError(2, "gone"),
                                                    "locked.txt": PermissionError(13, "denied")})
    result = scanner(now, stat=lookups).changes("R", {}, NO_RULES)
    assert sorted(os.path.basename(e.path) for e in result.added) == ["fine.txt", "gone.txt", "locked.txt"]
    assert {os.path.basename(e.path): e.file_id for e in result.added} == {"fine.txt": "5:3", "gone.txt": None, "locked.txt": None}
    assert result.skipped == ()


def test_with_ids_switched_off_changes_asks_for_none():
    def boom(*a, **k):
        raise AssertionError("stat was called")
    now = zero_id_tree(("new.txt", 1, 1.0))
    result = scanner(now, stat=boom, collect_ids=False).changes("R", {}, NO_RULES)
    assert [e.file_id for e in result.added] == [None]


def test_a_full_scan_still_asks_for_every_id_once():
    now = zero_id_tree(("a.txt", 1, 1.0), ("b.txt", 1, 1.0))
    lookups = Lookups(ids={"a.txt": 1, "b.txt": 2})
    source = scanner(now, stat=lookups)
    assert [e.file_id for e in source.scan("R", NO_RULES).entries] == ["5:1", "5:2"]
    assert source.last_stats["id_lookups"] == 2


# ---- long paths ----------------------------------------------------------------------------------------

def test_short_paths_and_non_windows_paths_are_left_alone():
    assert long_path("short") == "short"
    if os.name != "nt":
        assert long_path("/a" * 200) == "/a" * 200


@pytest.mark.windows_only
def test_a_long_windows_path_gets_the_extended_prefix_without_changing_the_name():
    long = "C:\\" + "\\".join(["d" * 20] * 15)
    assert long_path(long).startswith("\\\\?\\C:\\") and long_path(long).endswith("d" * 20)
    assert long_path(long) == long_path(long_path(long))
    unc = "\\\\server\\share\\" + "\\".join(["d" * 20] * 15)
    assert long_path(unc).startswith("\\\\?\\UNC\\server\\share\\")


# ---- real folders --------------------------------------------------------------------------------------

def test_awkward_real_names_are_listed_exactly(tmp_path):
    folder = tmp_path / helpers.AWKWARD_FOLDER
    folder.mkdir()
    for name in (f"{helpers.CAFE} {helpers.CJK}.txt", "with  two  spaces.txt", f"{helpers.EM_DASH}dash.txt"):
        (folder / name).write_bytes(b"x")
    listing = DirectoryScan().scan(str(tmp_path), NO_RULES)
    assert sorted(os.path.basename(e.path) for e in listing.entries) == sorted(
        [f"{helpers.CAFE} {helpers.CJK}.txt", "with  two  spaces.txt", f"{helpers.EM_DASH}dash.txt"])


def test_a_file_given_as_the_root_is_unreachable(tmp_path):
    f = tmp_path / "file.txt"
    f.write_bytes(b"x")
    source = DirectoryScan()
    assert not source.reachable(str(f))
    result = source.scan(str(f), NO_RULES)
    assert isinstance(result, Failure) and result.reason is FailureReason.UNREACHABLE


def test_a_wide_folder_and_a_deep_tree_are_both_listed_without_trouble(tmp_path):
    wide = tmp_path / "wide"
    wide.mkdir()
    for i in range(2000):
        (wide / f"f{i}.txt").write_bytes(b"")
    deep = tmp_path / "deep"
    top = str(deep)
    path = top
    for _ in range(150):
        path = os.path.join(path, "d")
    os.makedirs(long_path(path))
    with open(long_path(os.path.join(path, "bottom.txt")), "wb") as handle:
        handle.write(b"x")
    try:
        listing = DirectoryScan().scan(str(tmp_path), NO_RULES)
        assert len(listing.entries) == 2001 and listing.skipped == ()
        assert any(e.path.endswith("bottom.txt") and len(e.path) > 300 for e in listing.entries)
        assert not any(e.path.startswith("\\\\?\\") for e in listing.entries)   # callers see normal paths
    finally:
        shutil.rmtree(long_path(top), ignore_errors=True)


@pytest.mark.skipif(os.name == "nt", reason="needs symbolic links, which Windows only allows with special rights")
def test_real_symlinks_are_not_followed_even_when_they_loop(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.txt").write_bytes(b"x")
    os.symlink(tmp_path, tmp_path / "real" / "loop")
    os.symlink(tmp_path / "real" / "a.txt", tmp_path / "link.txt")
    source = DirectoryScan()
    listing = source.scan(str(tmp_path), NO_RULES)
    assert [os.path.basename(e.path) for e in listing.entries] == ["a.txt"]
    assert source.last_stats["links_skipped"] == 2


@pytest.mark.windows_only
def test_a_real_junction_is_not_followed(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "inside.txt").write_bytes(b"x")
    (tmp_path / "scanme").mkdir()
    (tmp_path / "scanme" / "own.txt").write_bytes(b"x")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "scanme" / "jct"), str(tmp_path / "target")],
                          capture_output=True, text=True)
    assert made.returncode == 0, "could not create a junction for the test"
    source = DirectoryScan()
    listing = source.scan(str(tmp_path / "scanme"), NO_RULES)
    assert [os.path.basename(e.path) for e in listing.entries] == ["own.txt"]
    assert source.last_stats["links_skipped"] == 1


@pytest.mark.windows_only
def test_windows_files_get_a_usable_id(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"x")
    ids = [e.file_id for e in DirectoryScan().scan(str(tmp_path), NO_RULES).entries]
    assert all(ids) and len(set(ids)) == 2


def test_scanning_is_read_only(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x")
    before = sorted((e.name, e.stat().st_mtime_ns) for e in os.scandir(tmp_path))
    DirectoryScan().scan(str(tmp_path), NO_RULES)
    assert sorted((e.name, e.stat().st_mtime_ns) for e in os.scandir(tmp_path)) == before


def test_scan_can_be_stopped_from_another_thread(tmp_path):
    for i in range(300):
        (tmp_path / f"f{i}.txt").write_bytes(b"")
    stop = threading.Event()
    stop.set()
    result = DirectoryScan().scan(str(tmp_path), NO_RULES, stop)
    assert isinstance(result, Failure) and result.reason is FailureReason.STOPPED
