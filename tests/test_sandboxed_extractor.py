"""Readers in a worker process: the same answers as in-process, and every way a worker can fail is a stated failure."""

import os
import threading

import pytest

pytest.importorskip("pypdfium2", reason="the PDF reader pypdfium2 is not installed (pip install -r requirements.txt)")

import helpers
from conformance.extractor_checks import CHECKS
from corpus import officewriter as ow
from corpus.pdfwriter import PdfPage, build_pdf
from dugup import jobs
from dugup.datadir import DataLayout
from dugup.extractors import default_extractors
from dugup.extractors.sandboxed import SandboxedExtractor, settings_to_data
from dugup.indexer import Indexer
from dugup.launcher import alive
from dugup.protocols import Extraction, Extractor, Limits, PageInfo, PageVerdict
from dugup.sandbox import SubprocessSandbox
from dugup.service import LocalService
from dugup.settings import Settings, from_mapping
from dugup.shapes import Failure, FailureReason, FileKind, FileRecord, FileState, Root, SegmentIn, SegmentKind
from extractor_harness import RealHarness
from test_sandbox import TESTS, WAIT, wait_gone

# This one check counts how often the reader looks at the stop signal between pages. A worker is stopped
# from outside instead (it is ended), which the tests below cover, so the count does not apply here.
NOT_FOR_A_WORKER = {"stop_between_pages"}


@pytest.fixture(scope="module")
def sandbox():
    with SubprocessSandbox() as box:
        yield box


def harness(tmp_path, sandbox, settings=None):
    h = RealHarness(tmp_path, settings)
    h.inside = h.extractor
    h.extractor = SandboxedExtractor(sandbox, settings or Settings())
    return h


@pytest.mark.parametrize("name", sorted(set(CHECKS) - NOT_FOR_A_WORKER))
def test_readers_in_a_worker_meet_the_extractor_contract(name, tmp_path, sandbox):
    CHECKS[name](harness(tmp_path, sandbox))


def test_it_is_an_extractor(sandbox):
    assert isinstance(SandboxedExtractor(sandbox), Extractor)


def test_a_worker_gives_exactly_the_answer_the_reader_gives_in_process(tmp_path, sandbox):
    h = harness(tmp_path, sandbox)
    mixed = f"{helpers.CAFE} {helpers.GREEK} {helpers.CJK} {helpers.EMOJI} {helpers.EM_DASH}"
    files = [
        h.write(f"{helpers.CJK} {helpers.EMOJI}.pdf", build_pdf([PdfPage("born_digital", f"one {mixed}"), PdfPage("scanner", "two"),
                                                                  PdfPage("garbled", "three"), PdfPage("none"), PdfPage("unreadable")])),
        h.write("letter.docx", ow.build_docx([f"body {mixed}", "tab\there"], header="head")),
        h.write("book.xlsx", ow.build_xlsx({"S": [["a", 1], [mixed, 2.5]]})),
        h.write("notes.txt", (("line " + mixed + "\n") * 400).encode("utf-8")),
        h.write("locked.pdf", build_pdf([PdfPage("born_digital", "x")], password="pw")),
        h.write("broken.docx", b"PK\x03\x04 nope"),
        h.write("old.doc", ow.ole_file()),
        h.write("empty.txt", b""),
    ]
    for record in files:
        assert h.extractor.extract(record) == h.inside.extract(record), record.path


def test_settings_reach_the_worker(tmp_path, sandbox):
    small = Settings(chunk_chars=100, chunk_overlap=10, max_pdf_pages=2)
    h = harness(tmp_path, sandbox, small)
    text = h.extractor.extract(h.write("long.txt", b"word " * 200))
    assert len(text.segments) > 5 and all(len(s.text) <= 100 for s in text.segments)
    pdf = h.extractor.extract(h.write("many.pdf", build_pdf([PdfPage("born_digital", f"p{i}") for i in range(5)])))
    assert len(pdf.pages) == 2 and "3 to 5" in pdf.unread[0]
    assert from_mapping(settings_to_data(small))[0] == small, "every setting survives the trip as plain data"


def test_only_files_that_need_parsing_go_to_a_worker(tmp_path):
    class NoSandbox:
        def run(self, *a, **k):
            raise AssertionError("this file should not have been sent to a worker")

    extractor = SandboxedExtractor(NoSandbox())
    image = extractor.extract(FileRecord("/x/photo.png", 1, 1.0, FileKind.IMAGE))
    assert image == Extraction((), (PageInfo(1, PageVerdict.NO_LAYER, True),))
    other = extractor.extract(FileRecord("/x/app.exe", 1, 1.0, FileKind.OTHER))
    assert isinstance(other, Failure) and other.reason is FailureReason.UNSUPPORTED
    stop = threading.Event()
    stop.set()
    assert extractor.extract(FileRecord("/x/a.pdf", 1, 1.0, FileKind.PDF), stop).reason is FailureReason.STOPPED


class Scripted:
    """A Sandbox that answers from a script and records what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def run(self, job, payload, limits, stop=None):
        self.asked.append((job, payload, limits, stop))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.mark.parametrize("reason", [FailureReason.TIMED_OUT, FailureReason.CRASHED, FailureReason.OUT_OF_MEMORY,
                                    FailureReason.STOPPED, FailureReason.NOT_INSTALLED])
def test_every_way_a_worker_fails_comes_back_as_that_failure(reason):
    result = SandboxedExtractor(Scripted(Failure(reason, "why"))).extract(FileRecord("/x/a.pdf", 10, 1.0, FileKind.PDF))
    assert isinstance(result, Failure) and result.reason is reason and result.detail == "why"


@pytest.mark.parametrize("nonsense", [None, 7, "text", [], {}, {"segments": "x", "pages": []}, {"segments": [[1, 0, "wrong", "t"]], "pages": []},
                                      {"failure": "not_a_reason", "detail": "x"}, {"segments": [], "pages": [[0, "no_layer", True, ""]]}])
def test_an_answer_that_makes_no_sense_is_a_stated_failure_not_an_exception(nonsense):
    result = SandboxedExtractor(Scripted(nonsense)).extract(FileRecord("/x/a.pdf", 10, 1.0, FileKind.PDF))
    assert isinstance(result, Failure) and result.reason is FailureReason.CRASHED and result.detail.strip()


def test_the_time_limit_grows_with_the_file_and_stops_at_the_cap():
    s = Settings(reader_seconds=60, reader_seconds_per_mb=2, reader_seconds_cap=900)
    mb = 1024 * 1024
    assert s.reader_limit(0) == 60 and s.reader_limit(-5) == 60 and s.reader_limit(10 * mb) == 80
    assert s.reader_limit(10_000 * mb) == 900
    box = Scripted({"segments": [], "pages": [], "unread": []})
    SandboxedExtractor(box, Settings(reader_memory_mb=0)).extract(FileRecord("/x/a.pdf", 100 * mb, 1.0, FileKind.PDF))
    assert box.asked[0][2] == Limits(260.0, None), "no memory limit when the setting is 0"
    SandboxedExtractor(box, s).extract(FileRecord("/x/a.pdf", 100 * mb, 1.0, FileKind.PDF))
    assert box.asked[1][2] == Limits(260.0, 4096)


@pytest.mark.parametrize("bad", [dict(reader_seconds=0), dict(reader_seconds_cap=-1), dict(reader_seconds_per_mb=-1), dict(reader_memory_mb=-1)])
def test_impossible_isolation_settings_are_refused(bad):
    with pytest.raises(ValueError):
        Settings(**bad)


def test_the_isolation_switch_is_read_from_the_config_file(tmp_path):
    from dugup.settings import load
    (tmp_path / "config.toml").write_text("isolate_readers = false\nreader_seconds = 5\n", encoding="utf-8")
    settings, notes = load(tmp_path / "config.toml")
    assert notes == [] and settings.isolate_readers is False and settings.reader_seconds == 5
    assert from_mapping({"isolate_readers": 1})[1], "a number is not a yes or no"


@pytest.mark.linux_only
def test_a_file_name_that_is_not_valid_text_survives_the_trip(tmp_path, sandbox):
    raw = os.fsencode(str(tmp_path)) + b"/caf\xe9-latin1.txt"
    with open(raw, "wb") as handle:
        handle.write(b"oddnameword")
    path = os.fsdecode(raw)
    record = FileRecord(path, 11, 1.0, FileKind.TEXT)
    assert jobs.record_from_data(jobs.record_to_data(record)).path == path
    assert [s.text for s in SandboxedExtractor(sandbox).extract(record).segments] == ["oddnameword"]


# ---- what the indexer makes of it ----------------------------------------------------------------------------

def test_a_file_that_times_out_or_kills_its_worker_is_listed_and_the_run_goes_on(tmp_path):
    from fakes.fake_filesource import Harness as DiskHarness
    from fakes.fake_store import FakeStore
    disk, store = DiskHarness(), FakeStore()
    root = disk.new_root()
    store.set_root(Root(root))
    for name in ("a.pdf", "b.pdf", "c.pdf", "d.pdf"):
        disk.put(root, name, b"x")
    good = {"segments": [[1, 0, "text", "fineword"]], "pages": [[1, "born_digital", False, ""]], "unread": []}
    box = Scripted(Failure(FailureReason.TIMED_OUT, "the job did not finish within 60 s and was ended"),
                   Failure(FailureReason.CRASHED, "the worker ended without an answer (exit code -11)"),
                   Failure(FailureReason.OUT_OF_MEMORY, "the job needed more memory than the worker is allowed"), good)
    report = Indexer(store, disk.source, SandboxedExtractor(box)).run()
    states = {r.path.rsplit("/", 1)[-1]: (r.state, r.error.split(":")[0]) for r in store.list_files()}
    assert states == {"a.pdf": (FileState.FAILED, "timed_out"), "b.pdf": (FileState.FAILED, "crashed"),
                      "c.pdf": (FileState.FAILED, "out_of_memory"), "d.pdf": (FileState.DONE, "")}
    assert (report.failed, report.done, report.stopped) == (3, 1, False)
    assert [r.path.rsplit("/", 1)[-1] for r in store.search_words("fineword").rows] == ["d.pdf"]


def test_a_stop_while_a_reader_is_stuck_leaves_the_file_waiting(tmp_path):
    arrives = threading.Event()

    class Stuck:
        """A reader that never finishes. The stop request arrives once the job is under way; the sandbox
        only learns of it through the signal it was handed, exactly as the real one does."""

        def run(self, job, payload, limits, stop=None):
            arrives.set()
            if stop is not None and stop.wait(WAIT):
                return Failure(FailureReason.STOPPED, "stopped while a worker was busy")
            return Failure(FailureReason.TIMED_OUT, "the stop request never reached the sandbox")

    from fakes.fake_filesource import Harness as DiskHarness
    from fakes.fake_store import FakeStore
    disk, store = DiskHarness(), FakeStore()
    root = disk.new_root()
    store.set_root(Root(root))
    disk.put(root, "a.pdf", b"x")
    report = Indexer(store, disk.source, SandboxedExtractor(Stuck())).run(arrives)
    record = store.list_files()[0]
    assert report.stopped and (record.state, record.error) == (FileState.PENDING, ""), "the file is simply still waiting"


# ---- the real thing, through the service -----------------------------------------------------------------------

def test_the_service_reads_in_a_worker_by_default_and_ends_it_when_closed(tmp_path):
    docs = tmp_path / helpers.AWKWARD_FOLDER
    docs.mkdir()
    (docs / f"{helpers.CAFE}.txt").write_text(f"workerword {helpers.CJK}", encoding="utf-8")
    (docs / "scan.pdf").write_bytes(build_pdf([PdfPage("born_digital", "pdfword here"), PdfPage("none")]))
    data = tmp_path / "data"
    data.mkdir()
    service = LocalService(DataLayout(data).ensure())
    assert isinstance(service.extractor, SandboxedExtractor)
    service.set_root(Root(str(docs)))
    report = service.run_indexing()
    assert (report.done, report.partial, report.failed) == (1, 1, 0)
    assert service.search("workerword").total == 1 and service.search("pdfword").total == 1
    pids = service.extractor.sandbox.worker_pids()
    assert len(pids) == 1 and pids[0] != os.getpid() and alive(pids[0])
    service.close()
    wait_gone(pids, "the reader's worker to end when the service closes")


def test_isolation_can_be_switched_off(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with LocalService(DataLayout(data).ensure(), Settings(isolate_readers=False)) as service:
        assert not isinstance(service.extractor, SandboxedExtractor)
