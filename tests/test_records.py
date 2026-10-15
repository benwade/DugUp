"""The record protocol every file-changing operation follows (records.Journal): an intent that cannot be
written stops the operation before the file is touched; an outcome that cannot be written stops it after,
with the change reported. Injected through the very Journal the window and the command line use."""

from __future__ import annotations

import io
import os

import pytest

from dugup import cleanup, naming, records
from dugup.records import Journal, RecordError
from dugup.shapes import FileKind, FileRecord, FileState
from dugup.store import SqliteStore


class Handle(io.StringIO):
    """A record handle whose writes, flushes or syncs can be made to fail from a chosen call on."""

    def __init__(self):
        super().__init__()
        self.fail_write_at = None      # the n-th write (1-based) raises
        self.fail_flush = False
        self.fail_sync = False
        self.writes = 0

    def write(self, text):
        self.writes += 1
        if self.fail_write_at is not None and self.writes >= self.fail_write_at:
            raise OSError(28, "No space left on device")
        return super().write(text)

    def flush(self):
        if self.fail_flush:
            raise OSError(5, "Input/output error")
        super().flush()

    def fileno(self):
        if self.fail_sync:
            raise OSError(5, "sync refused")
        return -1


def journal_over(handle: Handle) -> Journal:
    journal = Journal(handle)
    if not handle.fail_sync:
        journal.synced = False       # a StringIO cannot be fsynced; the sync path is tried on its own below
    return journal


def test_an_intent_that_cannot_be_written_leaves_every_file_untouched_and_stops():
    journal = journal_over(Handle())
    journal.handle.fail_write_at = 1
    with pytest.raises(RecordError):
        journal.intent("renaming\tx\tto\ty")
    assert journal.broken and journal.lines == []
    with pytest.raises(RecordError):
        journal.outcome("left\tx\tanything")        # broken stays broken


def test_a_sync_that_fails_is_a_failed_intent():
    handle = Handle()
    handle.fail_sync = True
    journal = Journal(handle)
    with pytest.raises(RecordError) as caught:
        journal.intent("moving\tx")
    assert "sync refused" in str(caught.value)
    journal2 = Journal(Handle())
    journal2.synced = False
    journal2.outcome("left\tx\twhy")
    assert journal2.lines == ["left\tx\twhy"]


@pytest.fixture
def pictures(tmp_path):
    folder = tmp_path / "pics"
    folder.mkdir()
    store = SqliteStore(tmp_path / "index.sqlite")
    records_ = []
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        (folder / name).write_bytes(b"x")
        st = os.stat(folder / name)
        record = FileRecord(str(folder / name), st.st_size, st.st_mtime, kind=FileKind.IMAGE, state=FileState.DONE)
        store.upsert_file(record)
        records_.append(record)
    yield folder, store, records_
    store.close()


def test_renames_stop_before_the_first_file_when_the_intent_fails_and_after_one_when_an_outcome_fails(pictures):
    folder, store, recs = pictures
    captions = {r.path: f"picture {n}" for n, r in enumerate(recs)}
    plan = naming.plan(recs, "{caption}", captions, lambda m: "d")
    # the first intent fails: nothing is renamed, nothing more is tried
    handle = Handle()
    handle.fail_write_at = 1
    done = naming.apply(plan, store, journal_over(handle))
    assert done.renamed == [] and done.stopped and "No space left" in done.record_problem
    assert sorted(p.name for p in folder.iterdir()) == ["a.jpg", "b.jpg", "c.jpg"], "every file untouched"
    assert len(done.skipped) == 1 and "record could not be written" in done.skipped[0][1]
    # the first outcome fails (intent 1 ok, outcome 1 fails): one file renamed and reported, the second never touched
    handle = Handle()
    handle.fail_write_at = 2
    done = naming.apply(plan, store, journal_over(handle))
    assert [os.path.basename(o) for o, _ in done.renamed] == ["a.jpg"] and done.stopped and done.record_problem
    assert (folder / "Picture 0.jpg").exists() and (folder / "b.jpg").exists() and (folder / "c.jpg").exists()
    assert handle.getvalue().startswith("renaming\t"), "the intent for the renamed file is on the record"
    # undo follows the same rule
    pairs = naming.read_record(handle.getvalue())
    assert len(pairs) == 1, "an intent with no outcome is still a pair for undo"
    handle = Handle()
    handle.fail_write_at = 1
    back = naming.undo(pairs, store, journal_over(handle))
    assert back.renamed == [] and back.stopped and (folder / "Picture 0.jpg").exists()


def test_trash_and_photo_cleanup_stop_the_same_way(pictures, tmp_path):
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    from dugup.settings import Settings
    folder, store, recs = pictures
    store.close()
    layout = DataLayout(tmp_path / "data").ensure()
    moved = []
    mover = lambda path: (moved.append(path), os.remove(path), "")[2]
    with LocalService(layout, Settings()) as svc:
        for r in recs:
            svc.store.upsert_file(r)
        handle = Handle()
        handle.fail_write_at = 1
        done = svc.to_trash([r.path for r in recs], journal=journal_over(handle), mover=mover)
        assert moved == [] and done.stopped and done.record_problem and all((folder / n).exists() for n in ("a.jpg", "b.jpg", "c.jpg"))
        handle = Handle()
        handle.fail_write_at = 2          # the first move's outcome fails: one moved, reported, the rest untouched
        done = svc.to_trash([r.path for r in recs], journal=journal_over(handle), mover=mover)
        assert [os.path.basename(p) for p in moved] == ["a.jpg"] and [os.path.basename(p) for p, _ in done.moved] == ["a.jpg"]
        assert done.stopped and (folder / "b.jpg").exists() and (folder / "c.jpg").exists()
        assert handle.getvalue() == f"moving\t{folder / 'a.jpg'}\n", "the intent is on the record; the outcome could not be"


def test_exact_copy_cleanup_has_its_intent_on_the_record_before_the_mover_runs(tmp_path):
    folder = tmp_path / "d"
    (folder / "Docs").mkdir(parents=True)
    (folder / "Backup").mkdir()
    (folder / "Docs" / "n.txt").write_bytes(b"same")
    (folder / "Backup" / "n.txt").write_bytes(b"same")
    keep = FileRecord(str(folder / "Docs" / "n.txt"), 4, 1.0, kind=FileKind.TEXT, state=FileState.DONE)
    extra = FileRecord(str(folder / "Backup" / "n.txt"), 4, 1.0, kind=FileKind.TEXT, state=FileState.DONE)
    found = cleanup.Report(str(folder), (), [cleanup.Same(cleanup.Copy(keep, 1), [cleanup.Copy(extra, 1)])])
    handle = Handle()
    journal = journal_over(handle)
    seen = []

    def mover(path):
        seen.append(handle.getvalue())          # what the record holds at the moment the mover runs
        os.remove(path)
        return ""

    done = cleanup.remove_extras(found, mover, lambda p: None, journal=journal)
    assert len(done.moved) == 1
    assert seen[0].startswith("moving\t") and "\tkept\t" in seen[0], "the intent, naming the copy kept, was on the record first"
    # and a failing intent keeps the mover from running at all
    (folder / "Backup" / "n.txt").write_bytes(b"same")
    handle = Handle()
    handle.fail_write_at = 1
    seen.clear()
    done = cleanup.remove_extras(found, mover, lambda p: None, journal=journal_over(handle))
    assert seen == [] and done.moved == [] and done.stopped and done.record_problem
