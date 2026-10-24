"""LocalService on real temporary folders: roots, policy, honest results, one run at a time, background runs."""

import os
import threading

import pytest

import helpers
from dugup.datadir import DataLayout
from dugup.indexer import POLICY_NOTE
from dugup.lockfile import RunLock
from dugup.protocols import Extraction, Service
from dugup.service import LocalService
from dugup.shapes import (POLICY_NAMES_ONLY, POLICY_OPEN, Failure, FailureReason, FileState, Passage, Root, SearchFilters,
                             SegmentIn, SegmentKind)

WAIT = 30.0   # only bounds how long a broken program can hang the tests; a correct one never waits for it


@pytest.fixture
def layout(tmp_path):
    data = tmp_path / "data folder"
    data.mkdir()
    return DataLayout(data).ensure()


@pytest.fixture
def docs(tmp_path):
    folder = tmp_path / helpers.AWKWARD_FOLDER
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("alpha needle text", encoding="utf-8")
    (folder / "sub" / f"{helpers.CAFE}.txt").write_text(f"beta words {helpers.CJK}", encoding="utf-8")
    (folder / "locked.docx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100))
    (folder / "photo.png").write_bytes(b"\x89PNG not really")
    return folder


@pytest.fixture
def service(layout):
    with LocalService(layout) as svc:
        yield svc


def names(page):
    return sorted(r.name for r in page.rows)


def test_local_service_fits_the_service_contract(service):
    assert isinstance(service, Service)


# ---- roots -------------------------------------------------------------------------------------------------

def test_a_root_is_added_listed_and_indexed(service, docs):
    added = service.set_root(Root(str(docs), exclude=("*.png",)))
    assert isinstance(added, Root) and added.path == str(docs) and added.exclude == ("*.png",)
    assert [r.path for r in service.list_roots()] == [str(docs)]
    report = service.run_indexing()
    assert (report.done, report.failed, report.stopped) == (2, 1, False)
    assert names(service.search("needle")) == ["a.txt"] and names(service.search(helpers.CJK)) == [f"{helpers.CAFE}.txt"]
    assert service.search("photo").total == 0, "an excluded file is not even listed"


def test_roots_that_cannot_work_are_refused_with_a_reason(service, docs, layout, tmp_path):
    assert service.set_root(Root(str(tmp_path / "not there"))).reason is FailureReason.UNREACHABLE
    assert service.set_root(Root(str(docs / "a.txt"))).reason is FailureReason.UNREACHABLE, "a file is not a folder"
    assert service.set_root(Root(str(docs), policy="everything")).reason is FailureReason.UNSUPPORTED
    assert service.set_root(Root(str(layout.logs))).reason is FailureReason.UNSUPPORTED, "never index the data folder"
    assert isinstance(service.set_root(Root(str(docs))), Root)
    inside = service.set_root(Root(str(docs / "sub")))
    above = service.set_root(Root(str(tmp_path)))
    assert inside.reason is FailureReason.UNSUPPORTED and above.reason is FailureReason.UNSUPPORTED
    assert "overlaps" in inside.detail and len(service.list_roots()) == 1


def test_a_sibling_folder_with_a_longer_name_is_not_an_overlap(service, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data2").mkdir()
    assert isinstance(service.set_root(Root(str(tmp_path / "data"))), Root)
    assert isinstance(service.set_root(Root(str(tmp_path / "data2"))), Root)


def test_the_data_folder_inside_a_root_is_skipped(tmp_path):
    root = tmp_path / "everything"
    (root / "appdata").mkdir(parents=True)
    (root / "note.txt").write_text("alpha note", encoding="utf-8")
    layout = DataLayout(root / "appdata").ensure()
    (layout.logs / "old.log").write_text("beta in a log", encoding="utf-8")
    with LocalService(layout) as svc:
        assert isinstance(svc.set_root(Root(str(root))), Root)
        svc.run_indexing()
        assert names(svc.search("alpha")) == ["note.txt"] and svc.search("beta").total == 0
        assert svc.indexing_status()["files"] == 1


def test_removing_a_root_purges_it_unless_asked_to_keep(service, docs, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "b.txt").write_text("alpha elsewhere", encoding="utf-8")
    service.set_root(Root(str(docs)))
    service.set_root(Root(str(other)))
    service.run_indexing()
    assert service.remove_root(str(docs)) == 4
    assert names(service.search("alpha")) == ["b.txt"] and [r.path for r in service.list_roots()] == [str(other)]
    assert service.remove_root(str(other), purge=False) == 0 and names(service.search("alpha")) == ["b.txt"]
    assert service.remove_root(str(other)).reason is FailureReason.OTHER, "it is no longer a root"


# ---- policy is enforced here, once ---------------------------------------------------------------------------

def test_tightening_a_policy_purges_the_text_and_loosening_reads_again(service, docs):
    service.set_root(Root(str(docs)))
    service.run_indexing()
    assert service.search("needle").total == 1 and service.indexing_status()["segments"] == 2
    service.set_root(Root(str(docs), POLICY_NAMES_ONLY))
    assert service.search("needle").total == 0 and service.search("beta").total == 0, "the stored text must be gone at once"
    assert service.indexing_status()["segments"] == 0
    assert names(service.search("a.txt")) == ["a.txt"], "names stay searchable"
    assert dict(service.indexing_status()["counts"]) == {"skipped": 4}
    assert all(r.error == POLICY_NOTE for r in service.store.list_files())
    assert service.run_indexing().read == 0, "a names-only folder is never read"
    service.set_root(Root(str(docs), POLICY_OPEN))
    assert service.run_indexing().read == 4 and service.search("needle").total == 1


# ---- honest results ------------------------------------------------------------------------------------------

def test_results_carry_evidence_that_shows_the_words(service, docs):
    service.set_root(Root(str(docs)))
    service.run_indexing()
    row = service.search("needle").rows[0]
    passage = service.get_evidence(row.evidence[0], context=6)
    assert isinstance(passage, Passage) and passage.text[passage.start:passage.end] == "needle"
    assert passage.text == "alpha needle text"
    (docs / "a.txt").write_text("needle moved to a new sentence entirely", encoding="utf-8")
    service.run_indexing()
    stale = service.get_evidence(row.evidence[0])
    assert isinstance(stale, Failure) and "search again" in stale.detail


def test_files_of_an_unreachable_folder_stay_in_results_and_are_flagged(service, docs, tmp_path):
    service.set_root(Root(str(docs)))
    service.run_indexing()
    away = tmp_path / "unplugged"
    os.rename(docs, away)
    report = service.run_indexing()
    assert report.roots[0].reachable is False
    page = service.search("needle")
    assert page.total == 1 and page.rows[0].unavailable is True and page.rows[0].missing_since is None
    os.rename(away, docs)
    service.run_indexing()
    assert service.search("needle").rows[0].unavailable is False


def test_only_files_that_are_here_now(service, docs, tmp_path):
    """present_only leaves out a file gone at the last scan and the files of a folder that cannot be reached;
    without it they stay, flagged, as before. The same narrowing serves every kind of search (one builder)."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "needle.txt").write_text("needle in the other folder", encoding="utf-8")
    for n in range(4):       # company, so losing one file is not "the folder lost most of its files" (nothing marked)
        (other / f"keep{n}.txt").write_text("nothing here", encoding="utf-8")
    service.set_root(Root(str(docs)))
    service.set_root(Root(str(other)))
    service.run_indexing()
    (other / "needle.txt").unlink()            # gone at the next scan
    service.run_indexing()
    away = tmp_path / "unplugged"
    os.rename(docs, away)                      # and the whole first folder cannot be reached
    service.run_indexing()
    page = service.search("needle")
    assert page.total == 2 and {(r.name, r.unavailable, r.missing_since is not None) for r in page.rows} == \
        {("a.txt", True, False), ("needle.txt", False, True)}
    here = service.search("needle", filters=SearchFilters(present_only=True))
    assert here.total == 0 and here.rows == []
    os.rename(away, docs)
    service.run_indexing()
    here = service.search("needle", filters=SearchFilters(present_only=True))
    assert [r.name for r in here.rows] == ["a.txt"] and here.total == 1


def test_a_bad_sort_key_is_a_stated_failure_not_an_exception(service):
    assert service.search("x", sort="nonsense").reason is FailureReason.UNSUPPORTED


def test_status_and_needs_attention(service, docs):
    service.set_root(Root(str(docs)))
    assert service.indexing_status()["counts"] == {} and service.indexing_status()["running"] is False
    service.run_indexing()
    status = service.indexing_status()
    assert status["files"] == 4 and status["counts"] == {"done": 2, "failed": 1, "partial": 1}
    assert status["roots"][0]["counts"] == status["counts"] and status["roots"][0]["reachable"] is True
    hopeless = service.unreadable_files()
    assert [(os.path.basename(r.path), r.state) for r in hopeless] == [("locked.docx", FileState.FAILED)], \
        "a picture that only waits for OCR is queued work, not trouble"
    assert all(r.error for r in hopeless) and service.needs_attention() == [], "a password is nothing to act on here"
    assert dict(service.attention_counts()) == {"attention": 0, "unreadable": 1, "waiting_for_ocr": 1}


def test_trouble_is_listed_failed_first_and_a_page_that_could_not_be_read_counts(service, docs):
    from corpus.pdfwriter import PdfPage, build_pdf
    (docs / "a broken page.pdf").write_bytes(build_pdf([PdfPage("none", ink=True), PdfPage("unreadable")]))
    service.set_root(Root(str(docs)))
    service.run_indexing()
    listed = [(os.path.basename(r.path), r.state) for r in service.unreadable_files()] + \
        [(os.path.basename(r.path), r.state) for r in service.needs_attention()]
    assert sorted(listed) == sorted([("locked.docx", FileState.FAILED), ("a broken page.pdf", FileState.PARTIAL)])
    assert len(service.unreadable_files(limit=1)) == 1 and service.unreadable_files(offset=5) == []
    tally = dict(service.attention_counts())
    assert tally["attention"] + tally["unreadable"] == 2 and tally["waiting_for_ocr"] == 1


def test_parts_not_built_yet_say_so(service):
    assert service.page_crop(1, 1).reason is FailureReason.UNSUPPORTED
    assert service.find_moved(1).reason is FailureReason.UNSUPPORTED
    assert service.record_usage({"kind": "open"}) is None
    assert "checks" in service.doctor()


# ---- one run at a time ---------------------------------------------------------------------------------------

def test_a_second_program_indexing_the_same_data_folder_is_refused(service, docs, layout):
    service.set_root(Root(str(docs)))
    other = RunLock(layout.root / "index.lock")
    assert other.acquire()
    try:
        refused = service.run_indexing()
        assert isinstance(refused, Failure) and refused.reason is FailureReason.BUSY
        assert service.search("needle").total == 0, "a refused run does nothing"
    finally:
        other.release()
    assert service.run_indexing().done == 2


class HoldingReader:
    """Reads the first file, then holds on the next until it is released or told to stop. No sleeping."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.count = 0

    def extract(self, record, stop=None):
        self.count += 1
        if self.count >= 2 and not self.release.is_set():
            self.entered.set()
            while not self.release.is_set():
                if stop is not None and stop.wait(0.01):
                    return Failure(FailureReason.STOPPED, "stopped while reading")
        return Extraction((SegmentIn(0, 0, SegmentKind.TEXT, "heldword"),))


def test_a_background_run_answers_status_and_stops_while_it_is_blocked(layout, docs):
    reader = HoldingReader()
    with LocalService(layout, extractor=reader) as service:
        service.set_root(Root(str(docs)))
        assert service.start_indexing() is None
        assert reader.entered.wait(WAIT), "the run never reached the second file"
        status = service.indexing_status()          # asked from this thread while the run is blocked in the other
        assert status["running"] is True and status["progress"]["phase"] == "read"
        assert status["counts"].get("done") == 1, "the first file was committed before the second began"
        assert service.search("heldword").total == 1, "what is finished is searchable while the run goes on"
        assert service.start_indexing().reason is FailureReason.BUSY
        assert service.run_indexing().reason is FailureReason.BUSY
        service.stop_indexing()                     # the reader is still holding: this must not hang
        assert service.is_indexing() is False and service.last_report.stopped is True
        assert service.indexing_status()["counts"].get("pending") == 3, "the unread files are still waiting"
        reader.release.set()
        assert service.scan_now() is None and service.wait_for_indexing(WAIT)
        assert service.last_report.stopped is False and service.indexing_status()["counts"] == {"done": 4}


def test_a_crash_in_a_background_run_is_reported_not_lost(layout, docs):
    class Broken:
        def changes(self, *a, **k):
            raise RuntimeError("source bug")

        def reachable(self, root):
            return True

        def scan(self, *a, **k):
            raise RuntimeError("source bug")

    with LocalService(layout, source=Broken()) as service:
        service.set_root(Root(str(docs)))
        service.start_indexing()
        assert service.wait_for_indexing(WAIT)
        assert isinstance(service.last_report, Failure) and service.last_report.reason is FailureReason.CRASHED
        probe = RunLock(layout.root / "index.lock")
        assert probe.acquire(), "the lock is given back after a crash"
        probe.release()


def test_saved_settings_govern_the_next_run_and_the_reading_connection(layout, docs, tmp_path):
    """apply_settings must reach the readers (chunking) and the store (closeness): the fingerprint said one
    thing and the readers did another."""
    from dugup.settings import Settings
    (docs / "long.txt").write_text(("word " * 60 + "\n") * 20, encoding="utf-8")
    with LocalService(layout, Settings(isolate_readers=False, content_marks=False)) as service:
        service.set_root(Root(str(docs)))
        service.run_indexing()
        before = service.store.text_of(service.store.get_file(str(docs / "long.txt")).row_id)
        assert all(len(text) <= 1800 for _, _, text in before) and any(len(text) > 300 for _, _, text in before)
        saved = service.apply_settings({"chunk_chars": 200, "chunk_overlap": 20, "near_words": 3})
        assert not isinstance(saved, Failure)
        assert service.store.near_words == 3, "the reading connection was reopened with the new closeness"
        from dugup.indexer import REREAD
        report = service.run_indexing()
        assert "chunk_chars" in getattr(report, "settings_changed", ()), "a run first says the settings that shape the index changed"
        report = service.run_indexing(on_settings_change=REREAD)
        after = service.store.text_of(service.store.get_file(str(docs / "long.txt")).row_id)
        assert after and all(len(text) <= 200 for _, _, text in after), "the readers rebuilt with the saved chunk size"



def test_another_thread_reads_through_a_connection_of_its_own(service, docs):
    """The window searches on a thread of its own; SQLite connections stay with the thread that opened them."""
    import threading

    service.set_root(Root(str(docs)))
    service.run_indexing()
    mine = service.store
    seen = {}

    def other():
        seen["store"] = service.store
        seen["again"] = service.store
        seen["found"] = service.search("needle", 10, 0).total
        service.release_thread_store()

    reader = threading.Thread(target=other)
    reader.start()
    reader.join(10)
    assert seen["store"] is not mine and seen["again"] is seen["store"], "one connection per thread, kept for it"
    assert seen["found"] == service.search("needle", 10, 0).total > 0
    assert service.store is mine


def test_the_thread_that_read_first_can_close_the_connection_it_opened(layout):
    """An assistant's server reads only from a thread of its own; when it ends, it closes what it opened, and
    the service then closes cleanly from the thread that made it."""
    import threading

    service = LocalService(layout)
    worker = threading.Thread(target=lambda: (service.store.file_count(), service.release_thread_store()))
    worker.start()
    worker.join(10)
    assert service._store is None
    service.close()                              # would raise if the worker's connection were still open
    assert service.store.file_count() == 0, "and the next thread to read opens one of its own"
    service.close()


def test_an_ending_given_a_new_type_is_read_that_way_on_the_next_update(layout, tmp_path):
    """With the real readers: .nfo is found by name only, until [extensions] says it is text."""
    from dugup.kinds import DEFAULT_EXTENSIONS
    from dugup.settings import Settings
    from dugup.shapes import FileKind
    folder = tmp_path / "Releases"
    folder.mkdir()
    (folder / "readme.nfo").write_text("a zebrafish release note", encoding="utf-8")
    with LocalService(layout) as service:
        service.set_root(Root(str(folder)))
        service.run_indexing()
        assert names(service.search("zebrafish")) == []
    with LocalService(layout, Settings(extensions={**DEFAULT_EXTENSIONS, "nfo": FileKind.TEXT})) as service:
        service.run_indexing()
        assert names(service.search("zebrafish")) == ["readme.nfo"]


def test_a_folder_is_left_out_put_back_and_its_files_are_never_touched(service, docs):
    weird = docs / "odd [1] folder"
    weird.mkdir()
    (weird / "w.txt").write_text("needle in a weird folder", encoding="utf-8")
    service.set_root(Root(str(docs)))
    service.run_indexing()
    assert names(service.search("needle")) == ["a.txt", "w.txt"]
    assert service.files_under(str(weird)) == 1 and service.files_under(str(docs / "sub")) == 1
    root, removed = service.leave_out(str(weird))
    assert removed == 1 and names(service.search("needle")) == ["a.txt"], "out of the index at once"
    assert (weird / "w.txt").exists(), "the file itself stays"
    assert service.left_out() == [(str(docs), str(weird))]
    service.run_indexing()
    assert names(service.search("needle")) == ["a.txt"], "and the next update leaves it out"
    assert service.leave_out(str(weird))[1] == 0, "leaving it out twice changes nothing"
    whole = service.leave_out(str(docs))
    assert isinstance(whole, Failure) and "whole indexed folder" in whole.detail
    assert isinstance(service.leave_out(str(docs.parent)), Failure), "outside every indexed folder"
    assert isinstance(service.put_back(str(weird)), Root) and service.left_out() == []
    service.run_indexing()
    assert names(service.search("needle")) == ["a.txt", "w.txt"], "put back: read again at the next update"
    assert isinstance(service.put_back(str(weird)), Failure), "it was not left out"
