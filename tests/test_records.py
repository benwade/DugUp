"""The record protocol every file-changing operation follows (records.Journal): an intent that cannot be
written stops the operation before the file is touched; an outcome that cannot be written stops it after,
with the change reported. Injected through the very Journal the window and the command line use."""

from __future__ import annotations

import io
import os

import pytest

from dugup import cleanup, naming, records
from dugup.records import Journal, RecordError
from dugup.shapes import Failure, FileKind, FileRecord, FileState
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


def test_undo_through_the_service_honours_current_protections_and_writes_its_own_record(tmp_path, pictures):
    """Solace's acceptance test: rename, protect the folder, undo through the public entry point: the undo is
    refused, and the undo has a record of its own under renames/ (the original record stays intact)."""
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    from dugup.settings import Settings
    folder, store, recs = pictures
    store.close()
    layout = DataLayout(tmp_path / "data").ensure()
    with LocalService(layout, Settings()) as svc:
        for r in recs:
            svc.store.upsert_file(r)
        plan = naming.Plan("{caption}", naming.plan(recs[:1], "{caption}", {recs[0].path: "apple pie"}, lambda m: "d"))
        done = svc.apply_names(plan)                  # no journal given: the service opens a record of its own
        assert [os.path.basename(o) for o, _ in done.renamed] == ["a.jpg"] and done.record and os.path.exists(done.record)
        text = open(done.record, encoding="utf-8-sig").read()
        assert text.startswith("pattern\t") and "renaming\t" in text and "renamed\t" in text
        assert not isinstance(svc.apply_settings({"cleanup_protect": str(folder)}), Failure)
        back = svc.undo_names(text)
        assert back.renamed == [] and back.skipped[0][1] == "it is in a protected folder now"
        assert back.record and os.path.exists(back.record) and os.path.basename(back.record).startswith("undo ")
        assert open(done.record, encoding="utf-8-sig").read() == text, "the original record is left as it was"
        assert (folder / "Apple pie.jpg").exists()
        svc.apply_settings({"cleanup_protect": ""})
        back = svc.undo_names(text)
        assert [os.path.basename(n) for n, _ in back.renamed] == ["Apple pie.jpg"] and (folder / "a.jpg").exists()
        assert "renaming\t" in open(back.record, encoding="utf-8-sig").read()


def test_undo_through_the_service_stops_on_a_failed_intent_and_after_a_failed_outcome(tmp_path, pictures, monkeypatch):
    from dugup import records
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    from dugup.settings import Settings
    folder, store, recs = pictures
    store.close()
    layout = DataLayout(tmp_path / "data").ensure()
    with LocalService(layout, Settings()) as svc:
        for r in recs:
            svc.store.upsert_file(r)
        plan = naming.Plan("{caption}", naming.plan(recs[:2], "{caption}", {recs[0].path: "apple pie", recs[1].path: "blue boat"}, lambda m: "d"))
        done = svc.apply_names(plan)
        assert len(done.renamed) == 2
        text = open(done.record, encoding="utf-8-sig").read()
        # the service's own record cannot be synced: no file is put back
        real_sync = records.sync
        monkeypatch.setattr(records, "sync", lambda handle: (_ for _ in ()).throw(OSError(5, "sync refused")))
        back = svc.undo_names(text)
        assert back.renamed == [] and back.stopped and "sync refused" in back.record_problem
        assert (folder / "Apple pie.jpg").exists() and (folder / "Blue boat.jpg").exists()
        monkeypatch.setattr(records, "sync", real_sync)
        # the first outcome fails: one put back and reported, the second untouched
        calls = {"n": 0}
        real_open = records.open_new

        def opening(folder_, stem):
            path, handle = real_open(folder_, stem)
            if stem == "undo":
                class Breaking:
                    def __init__(self, inner):
                        self.inner = inner
                    def write(self, text_):
                        calls["n"] += 1
                        if calls["n"] == 2:           # intent 1 ok, outcome 1 fails
                            raise OSError(28, "No space left on device")
                        return self.inner.write(text_)
                    def __getattr__(self, name):
                        return getattr(self.inner, name)
                return path, Breaking(handle)
            return path, handle

        monkeypatch.setattr(records, "open_new", opening)
        back = svc.undo_names(text)
        assert len(back.renamed) == 1 and back.stopped and "No space left" in back.record_problem
        assert (folder / "b.jpg").exists() and (folder / "Apple pie.jpg").exists(), "the last rename was undone first; the other left"


def test_a_record_from_the_previous_format_still_undoes_an_unchanged_file(tmp_path, pictures):
    """The a94d25b format wrote the time as seconds to three places. Compared at that precision, an unchanged
    file with a sub-millisecond time undoes; a new-format record still rejects a same-size, same-time replacement."""
    folder, store, recs = pictures
    a = folder / "a.jpg"
    os.utime(a, ns=(1600000000123456700, 1600000000123456700))
    os.rename(a, folder / "A renamed.jpg")
    old_line = f"renamed\t{a}\tto\t{folder / 'A renamed.jpg'}\t1\t{1600000000123456700 / 1e9:.3f}"
    pairs = naming.read_record(old_line)
    assert pairs[0][5] == 1_000_000, "a millisecond of tolerance, the precision it was written with"
    back = naming.undo(pairs, store)
    assert [os.path.basename(n) for n, _ in back.renamed] == ["A renamed.jpg"] and a.exists()

