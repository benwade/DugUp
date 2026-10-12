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
from dugup.shapes import (POLICY_NAMES_ONLY, POLICY_OPEN, Failure, FailureReason, FileState, Passage, Root,
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


def test_a_bad_sort_key_is_a_stated_failure_not_an_exception(service):
    assert service.search("x", sort="nonsense").reason is FailureReason.UNSUPPORTED


def test_status_and_needs_attention(service, docs):
    service.set_root(Root(str(docs)))
    assert service.indexing_status()["counts"] == {} and service.indexing_status()["running"] is False
    service.run_indexing()
    status = service.indexing_status()
    assert status["files"] == 4 and status["counts"] == {"done": 2, "failed": 1, "partial": 1}
    assert status["roots"][0]["counts"] == status["counts"] and status["roots"][0]["reachable"] is True
    attention = service.needs_attention()
    assert [(os.path.basename(r.path), r.state) for r in attention] == [("locked.docx", FileState.FAILED)], \
        "a picture that only waits for OCR is queued work, not trouble"
    assert all(r.error for r in attention)
    assert dict(service.attention_counts()) == {"attention": 1, "waiting_for_ocr": 1}


def test_trouble_is_listed_failed_first_and_a_page_that_could_not_be_read_counts(service, docs):
    from corpus.pdfwriter import PdfPage, build_pdf
    (docs / "a broken page.pdf").write_bytes(build_pdf([PdfPage("none", ink=True), PdfPage("unreadable")]))
    service.set_root(Root(str(docs)))
    service.run_indexing()
    assert [(os.path.basename(r.path), r.state) for r in service.needs_attention()] == [
        ("locked.docx", FileState.FAILED), ("a broken page.pdf", FileState.PARTIAL)]
    assert len(service.needs_attention(limit=1)) == 1 and len(service.needs_attention(offset=1)) == 1
    assert dict(service.attention_counts()) == {"attention": 2, "waiting_for_ocr": 1}


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
