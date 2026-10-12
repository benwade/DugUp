"""End to end on the generated tree: real folders, the real scanner, the real readers, the real store."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("pypdfium2", reason="the PDF reader pypdfium2 is not installed (pip install -r requirements.txt)")

from corpus.tree import build_tree
from dugup.datadir import DataLayout
from dugup.service import LocalService
from dugup.shapes import FileState, Root

SRC = str(Path(__file__).resolve().parents[1] / "src")
TESTS = str(Path(__file__).resolve().parent)


def make(tmp_path, name="data"):
    data = tmp_path / name
    data.mkdir()
    return LocalService(DataLayout(data).ensure())


def full(tree, relative):
    return str(tree / Path(*relative.split("/")))


def picture(service, tree):
    """What the index says about every file, by path below the tree."""
    out = {}
    for record in service.store.list_files():
        out[os.path.relpath(record.path, tree).replace(os.sep, "/")] = (record.state.value, record.error, record.missing_since is not None)
    return out


def found(service, word):
    return {r.path for r in service.search(word, limit=1000).rows}


@pytest.fixture
def world(tmp_path):
    tree = tmp_path / "tree"
    rows = build_tree(tree)
    with make(tmp_path) as service:
        assert isinstance(service.set_root(Root(str(tree))), Root)
        yield service, tree, rows


def test_every_generated_file_ends_in_its_expected_state_and_is_found_by_its_words(world):
    service, tree, rows = world
    report = service.run_indexing()
    assert not report.stopped and report.roots[0].added == len(rows)
    seen = picture(service, tree)
    assert set(seen) == {r.relative for r in rows}, "every file is listed exactly once"
    for row in rows:
        state, note, _ = seen[row.relative]
        assert state == row.state, f"{row.relative}: {state} ({note})"
        assert row.note in note, f"{row.relative}: note was {note!r}"
        assert state != "pending"
        if state in ("failed", "partial", "skipped"):
            assert note.strip(), f"{row.relative}: a file that was not fully read must say why"
        for word in row.words:
            assert full(tree, row.relative) in found(service, word), f"{row.relative}: {word!r} not found"
        for word in row.absent:
            assert found(service, word) == set(), f"{row.relative}: {word!r} must not be in the index"


def test_duplicates_are_all_found_and_each_name_is_searchable(world):
    service, tree, rows = world
    service.run_indexing()
    assert len(found(service, "duplicatedword")) == 4
    for row in rows:
        stem = Path(row.relative).name
        assert full(tree, row.relative) in {r.path for r in service.search(f'"{stem}"', limit=1000).rows} or \
            full(tree, row.relative) in found(service, stem), f"{row.relative}: not found by its own name"


def test_indexing_twice_changes_nothing(world):
    service, tree, rows = world
    service.run_indexing()
    before = (picture(service, tree), service.indexing_status()["segments"])
    again = service.run_indexing()
    assert again.read == 0 and (again.roots[0].added, again.roots[0].changed, again.roots[0].moved, again.roots[0].missing) == (0, 0, 0, 0)
    assert (picture(service, tree), service.indexing_status()["segments"]) == before


def test_edit_rename_delete_and_restore_on_a_real_disk(world):
    service, tree, rows = world
    service.run_indexing()
    plain = tree / "notes" / "plain.txt"
    # edit (the time is pushed forward by hand: two writes can land within one clock tick on some file systems)
    plain.write_text("zorblat was replaced by freshword", encoding="utf-8")
    info = os.stat(plain)
    os.utime(plain, (info.st_atime, info.st_mtime + 5))
    report = service.run_indexing()
    assert report.roots[0].changed == 1 and report.read == 1 and found(service, "freshword") == {str(plain)}

    # rename: the text follows the file without a re-read
    moved = tree / "notes" / "renamed note.txt"
    os.rename(plain, moved)
    report = service.run_indexing()
    assert (report.roots[0].moved, report.roots[0].added, report.roots[0].missing, report.read) == (1, 0, 0, 0)
    assert found(service, "freshword") == {str(moved)} and found(service, "renamed") == {str(moved)}

    # delete: kept, flagged, still found
    saved = moved.read_bytes()
    saved_times = (os.stat(moved).st_atime, os.stat(moved).st_mtime)
    moved.unlink()
    report = service.run_indexing()
    assert report.roots[0].missing == 1
    row = service.search("freshword").rows[0]
    assert row.missing_since is not None and row.state == "done"

    # restore the same bytes with the same time: back without a re-read
    moved.write_bytes(saved)
    os.utime(moved, saved_times)
    report = service.run_indexing()
    assert (report.roots[0].restored, report.read) == (1, 0)
    assert service.search("freshword").rows[0].missing_since is None


CRASHER = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from dugup.datadir import DataLayout
    from dugup.extractors import default_extractors
    from dugup.service import LocalService

    class DiesAfter:
        # reads normally, then ends the whole process without any cleanup, like a crash or a power cut
        def __init__(self, inner, files):
            self.inner, self.left = inner, files
        def extract(self, record, stop=None):
            if self.left == 0:
                os._exit(77)
            self.left -= 1
            return self.inner.extract(record, stop)

    service = LocalService(DataLayout(sys.argv[2]), extractor=DiesAfter(default_extractors(), int(sys.argv[3])))
    service.run_indexing()
    os._exit(0)
""")


@pytest.mark.parametrize("files_before_the_crash", [0, 1, 13, 35])
def test_a_process_killed_mid_run_leaves_an_index_the_next_run_completes(tmp_path, files_before_the_crash):
    tree = tmp_path / "tree"
    rows = build_tree(tree)
    with make(tmp_path, "straight") as straight:
        straight.set_root(Root(str(tree)))
        straight.run_indexing()
        wanted = (picture(straight, tree), straight.indexing_status()["segments"])

    with make(tmp_path, "crashed") as service:
        service.set_root(Root(str(tree)))
        data = str(service.layout.root)
    child = subprocess.run([sys.executable, "-c", CRASHER, SRC, data, str(files_before_the_crash)], timeout=120)
    assert child.returncode == 77, "the child was meant to die mid-run"

    with LocalService(DataLayout(Path(data))) as service:
        counts = service.indexing_status()["counts"]
        assert counts.get("pending", 0) == len(rows) - files_before_the_crash, "exactly the finished files were kept"
        report = service.run_indexing()      # the lock of the dead process must not be in the way
        assert not report.stopped and report.read == len(rows) - files_before_the_crash
        assert (picture(service, tree), service.indexing_status()["segments"]) == wanted
        assert service.indexing_status()["files"] == len(rows), "no duplicates"
