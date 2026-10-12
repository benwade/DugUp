"""Second tries, the pause after a run of failures, the disk guards, OCR that survives a re-read,
pages read by an older engine, and typed pages with figures. Scripted readers and engines; nothing sleeps."""

import io
import threading

import pytest

import helpers
from corpus.images import build_png
from dugup import cli, service as service_module, store as store_module
from dugup.datadir import DataLayout
from dugup.indexer import REREAD, RETRY_NOTE, Indexer
from dugup.ocrqueue import (DOCUMENT_PAGE, DOCUMENT_PICTURE, FIGURE_PAGE, SECOND_TRY, TINY, OcrRunner, priority)
from dugup.protocols import Extraction, OcrLine, OcrResult, OcrTask, PageInfo, PageVerdict
from dugup.service import LocalService
from dugup.settings import Settings
from dugup.shapes import Box, Failure, FailureReason, FileKind, FileRecord, FileState, Root, SegmentIn, SegmentKind
from dugup.store import SqliteStore
from fakes.fake_filesource import Harness as DiskHarness
from fakes.fake_store import FakeStore
from test_ocrqueue import ScriptedOcr, picture, scan, state, words

SLOW = Failure(FailureReason.TIMED_OUT, "the job did not finish in time and was ended")
DEAD = Failure(FailureReason.CRASHED, "the worker ended without an answer")


class PatientReader:
    """Answers by file name from a table of lists (one answer per try); anything else reads as its name."""

    def __init__(self, answers=None, patient=True):
        self.answers = {k: list(v) for k, v in (answers or {}).items()}
        self.calls = []            # (name, patience)
        self.on_call = None
        if not patient:
            self.extract_patiently = None

    def _answer(self, record, stop, patience):
        name = record.path.rsplit("/", 1)[-1]
        self.calls.append((name, patience))
        if self.on_call is not None:
            self.on_call(name)
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped")
        left = self.answers.get(name)
        if left:
            return left.pop(0) if len(left) > 1 else left[0]
        return Extraction((SegmentIn(0, 0, SegmentKind.TEXT, f"words of {name}"),))

    def extract(self, record, stop=None):
        return self._answer(record, stop, 1.0)

    def extract_patiently(self, record, stop=None, patience=2.0):
        return self._answer(record, stop, patience)


class World:
    def __init__(self, names, reader, settings=None, store=None, room=None):
        self.disk = DiskHarness()
        self.store = store or FakeStore()
        self.root = self.disk.new_root()
        self.store.set_root(Root(self.root))
        for name in names:
            self.disk.put(self.root, name, name.encode())
        self.reader = reader
        self.settings = settings or Settings()
        self.room = room

    def run(self, stop=None, **more):
        return Indexer(self.store, self.disk.source, self.reader, self.settings, room=self.room).run(stop, **more)

    def states(self):
        return {r.path.rsplit("/", 1)[-1]: (r.state, r.error) for r in self.store.list_files()}


NAMES = ["a.txt", "b.txt", "c.txt", "d.txt"]


# ---- a file that runs out of time ---------------------------------------------------------------------------

def test_a_file_that_runs_out_of_time_is_read_again_last_with_more_time():
    w = World(NAMES, PatientReader({"b.txt": [SLOW, Extraction((SegmentIn(0, 0, SegmentKind.TEXT, "slow but fine"),))]}))
    report = w.run()
    assert w.reader.calls == [("a.txt", 1.0), ("b.txt", 1.0), ("c.txt", 1.0), ("d.txt", 1.0), ("b.txt", 2.0)]
    assert w.states()["b.txt"] == (FileState.DONE, "")
    assert (report.read, report.done, report.failed, report.second_tries, report.paused) == (4, 4, 0, 1, "")
    assert w.store.search_words("slow").total == 1


def test_a_second_timeout_fails_the_file_with_its_reason():
    w = World(NAMES, PatientReader({"b.txt": [SLOW]}), Settings(retry_patience=3.0))
    report = w.run()
    assert w.reader.calls[-1] == ("b.txt", 3.0), "the patience is a setting"
    status, note = w.states()["b.txt"]
    assert status is FileState.FAILED and note.startswith("timed_out: ")
    assert (report.done, report.failed, report.second_tries) == (3, 1, 1)
    again = w.run()
    assert again.read == 0, "a failed file is not tried again unasked"


def test_the_first_try_is_remembered_across_runs():
    stop = threading.Event()
    reader = PatientReader({"a.txt": [SLOW, Extraction((SegmentIn(0, 0, SegmentKind.TEXT, "second try worked"),))]})
    reader.on_call = lambda name: stop.set() if name == "c.txt" else None
    w = World(NAMES, reader)
    assert w.run(stop).stopped
    assert w.states()["a.txt"] == (FileState.PENDING, RETRY_NOTE), "it waits, and says why"
    reader.on_call = None
    del reader.calls[:]
    report = w.run()
    assert reader.calls == [("c.txt", 1.0), ("d.txt", 1.0), ("a.txt", 2.0)], \
        "the next run does not repeat the short try: the file goes last, with more time"
    assert w.states()["a.txt"] == (FileState.DONE, "") and report.second_tries == 1


@pytest.mark.parametrize("case", ["patience off", "reader has no patient read"])
def test_without_a_second_try_a_timeout_fails_at_once(case):
    settings = Settings(retry_patience=1.0) if case == "patience off" else Settings()
    reader = PatientReader({"b.txt": [SLOW]}, patient=case == "patience off")
    w = World(NAMES, reader, settings)
    report = w.run()
    assert [name for name, _ in reader.calls] == NAMES
    assert w.states()["b.txt"][0] is FileState.FAILED and report.second_tries == 0


def test_stop_during_the_last_pass_leaves_the_file_waiting_for_it():
    stop = threading.Event()
    reader = PatientReader({"a.txt": [SLOW]})
    w = World(NAMES, reader)
    reader.on_call = lambda name: stop.set() if reader.calls[-1] == ("a.txt", 2.0) else None
    assert w.run(stop).stopped
    assert w.states()["a.txt"] == (FileState.PENDING, RETRY_NOTE)


def test_a_changed_file_starts_over():
    reader = PatientReader({"a.txt": [SLOW]})
    stop = threading.Event()
    reader.on_call = lambda name: stop.set() if name == "b.txt" else None
    w = World(NAMES, reader)
    w.run(stop)
    w.disk.put(w.root, "a.txt", b"new content, new size")
    reader.on_call = None
    reader.answers.clear()
    del reader.calls[:]
    w.run()
    assert ("a.txt", 1.0) in reader.calls and ("a.txt", 2.0) not in reader.calls


# ---- a run of failures ---------------------------------------------------------------------------------------

MANY = [f"f{n:02d}.txt" for n in range(12)]


def test_a_run_of_reader_crashes_pauses_the_run_and_keeps_the_rest_queued():
    w = World(MANY, PatientReader({name: [DEAD] for name in MANY}), Settings(reader_failures_in_a_row=3))
    report = w.run()
    assert "3 files in a row" in report.paused and "crashed" in report.paused
    assert not report.stopped and report.failed == 3 and len(w.reader.calls) == 3
    counts = w.store.state_counts()
    assert (counts["failed"], counts["pending"]) == (3, 9), "the rest is not written off"


def test_first_timeouts_count_towards_the_pause_and_nothing_is_failed_by_them():
    w = World(MANY, PatientReader({name: [SLOW] for name in MANY}), Settings(reader_failures_in_a_row=4))
    report = w.run()
    assert "ran out of time" in report.paused or "timed out" in report.paused
    assert report.failed == 0 and w.store.state_counts() == {"pending": 12}
    assert sum(1 for s, note in w.states().values() if note == RETRY_NOTE) == 4


def test_a_success_resets_the_count_and_file_specific_failures_do_not_count():
    answers = {name: [DEAD] for name in MANY[0:2] + MANY[3:5]}
    answers.update({name: [Failure(FailureReason.CORRUPT, "damaged")] for name in MANY[6:]})
    w = World(MANY, PatientReader(answers), Settings(reader_failures_in_a_row=3))
    report = w.run()
    assert report.paused == "" and (report.done, report.failed) == (2, 10)


def test_the_pause_can_be_turned_off():
    w = World(MANY, PatientReader({name: [DEAD] for name in MANY}), Settings(reader_failures_in_a_row=0))
    report = w.run()
    assert report.paused == "" and report.failed == 12


# ---- no room -------------------------------------------------------------------------------------------------

def test_no_room_at_the_start_means_nothing_is_touched():
    w = World(NAMES, PatientReader(), room=lambda: "the drive is full")
    report = w.run()
    assert report.paused == "the drive is full" and w.reader.calls == [] and w.store.file_count() == 0


def test_room_running_out_part_way_pauses_and_the_next_run_continues():
    names = [f"n{n:03d}.txt" for n in range(60)]
    reader = PatientReader()
    full = {"now": False}
    reader.on_call = lambda name: full.__setitem__("now", len(reader.calls) >= 30)
    w = World(names, reader, room=lambda: "the index reached its limit" if full["now"] else "")
    report = w.run()
    assert report.paused == "the index reached its limit" and not report.stopped
    assert 30 <= report.read < 60, "the check is made every so many files, not only at the start"
    full["now"] = False
    reader.on_call = None
    assert w.run().read == 60 - report.read and w.store.state_counts() == {"done": 60}


def test_the_service_guards(tmp_path, monkeypatch):
    layout = DataLayout(tmp_path / "data").ensure()
    megabyte = 1024 * 1024
    size = {"index": 0, "free": 10_000 * megabyte}
    monkeypatch.setattr(DataLayout, "index_bytes", lambda self: size["index"])
    monkeypatch.setattr(DataLayout, "free_bytes", lambda self: size["free"])
    with LocalService(layout, Settings(min_free_mb=0, max_index_mb=0)) as service:
        size.update(index=10**12, free=0)
        assert service.room() == "", "both guards can be turned off"
    with LocalService(layout, Settings(max_index_mb=5, min_free_mb=10)) as service:
        size.update(index=5 * megabyte, free=10 * megabyte)
        assert service.room() == "", "exactly at the limit, and exactly at the floor, is still room"
        size.update(index=5 * megabyte + 1)
        assert "spending limit of 5 MB" in service.room() and "dugup space --limit" in service.room()
        size.update(index=0, free=10 * megabyte - 1)
        assert "less than 10 MB free" in service.room()
        size.update(free=None)
        assert service.room() == "", "a drive that cannot be measured does not stop the work"


def test_the_index_is_measured_with_the_files_beside_it(tmp_path):
    layout = DataLayout(tmp_path / "data").ensure()
    assert layout.index_bytes() == 0, "no index yet is no bytes, not an error"
    layout.index.write_bytes(bytes(1000))
    layout.index.with_name("index.sqlite-wal").write_bytes(bytes(300))
    assert layout.index_bytes() == 1300
    assert layout.free_bytes() > 0
    assert DataLayout(tmp_path / "not" / "there").free_bytes() is None


# ---- OCR: second tries, figure pages, the pause, no room -----------------------------------------------------

def test_ocr_priority_has_figure_pages_and_second_tries():
    scanned = OcrTask(1, "/x.pdf", FileKind.PDF, 2, verdict=PageVerdict.NO_LAYER.value)
    figure = OcrTask(1, "/x.pdf", FileKind.PDF, 3, verdict=PageVerdict.BORN_DIGITAL.value)
    garbled = OcrTask(1, "/x.pdf", FileKind.PDF, 4, verdict=PageVerdict.GARBLED.value)
    again = OcrTask(1, "/x.pdf", FileKind.PDF, 5, verdict=PageVerdict.NO_LAYER.value, retry=True)
    tiny_again = OcrTask(2, "/i.png", FileKind.IMAGE, 1, 10, 10, retry=True)
    assert [priority(t, 200) for t in (scanned, figure, garbled, again, tiny_again)] == [
        DOCUMENT_PAGE, FIGURE_PAGE, DOCUMENT_PAGE, SECOND_TRY, SECOND_TRY]
    assert DOCUMENT_PAGE < FIGURE_PAGE < DOCUMENT_PICTURE < TINY < SECOND_TRY


def figure_pdf(store, path):
    row = store.upsert_file(FileRecord(path=path, size=10, modified=1.0, kind=FileKind.PDF, state=FileState.PARTIAL,
                                       error="1 of 2 page(s) wait for OCR"))
    store.set_pages(row, [PageInfo(1, PageVerdict.BORN_DIGITAL, False), PageInfo(2, PageVerdict.BORN_DIGITAL, True)])
    return row


def test_figure_pages_come_after_scanned_pages_and_before_pictures(store):
    picture(store, "/r/shot.png")
    figure_pdf(store, "/r/report.pdf")
    scan(store, "/r/scan.pdf", pages=2)
    ocr = ScriptedOcr(shapes={"/r/shot.png": (900, 900, False)})
    OcrRunner(store, ocr).run()
    assert ocr.calls == [("/r/scan.pdf", 2), ("/r/report.pdf", 2), ("/r/shot.png", 1)]


class TimedOcr(ScriptedOcr):
    """Records the time limit of every read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.limits = []

    def read(self, image, language="en", time_limit=None, region=None, stop=None):
        self.limits.append(time_limit)
        return super().read(image, language, time_limit, region, stop)

    def read_page(self, pdf, page, language="en", time_limit=None, stop=None):
        self.limits.append(time_limit)
        return super().read_page(pdf, page, language, time_limit, stop)


def test_a_page_that_runs_out_of_time_is_read_last_with_double_the_time(store):
    scan(store, "/r/scan.pdf", pages=3)
    picture(store, "/r/icon.png")
    ocr = TimedOcr(shapes={"/r/icon.png": (16, 16, False)})
    tries = []

    def answer(path, page):
        if (path, page) == ("/r/scan.pdf", 2):
            tries.append(1)
            ocr.answers[(path, page)] = SLOW if len(tries) == 1 else words("made it")

    ocr.on_call = answer
    report = OcrRunner(store, ocr, Settings(ocr_seconds=50.0)).run()
    assert ocr.calls == [("/r/scan.pdf", 2), ("/r/scan.pdf", 3), ("/r/icon.png", 1), ("/r/scan.pdf", 2)], \
        "the second try comes after everything else, tiny pictures included"
    assert ocr.limits == [None, None, None, 100.0]
    assert (report.read, report.failed, report.second_tries) == (3, 0, 1)
    assert state(store, "/r/scan.pdf") == (FileState.DONE, "") and store.search_words("made").total == 1


def test_a_second_timeout_fails_the_page_and_the_first_is_remembered_across_runs(store):
    scan(store, "/r/scan.pdf", pages=3)
    stop = threading.Event()
    ocr = TimedOcr({("/r/scan.pdf", 2): SLOW})
    ocr.on_call = lambda path, page: stop.set() if page == 3 else None
    report = OcrRunner(store, ocr).run(stop)
    assert report.stopped and report.failed == 0
    assert state(store, "/r/scan.pdf") == (FileState.PARTIAL, "2 of 3 page(s) wait for OCR"), "not failed yet: it still waits"
    later = TimedOcr({("/r/scan.pdf", 2): SLOW})
    report = OcrRunner(store, later, Settings(ocr_seconds=30.0, retry_patience=4.0)).run()
    assert later.calls == [("/r/scan.pdf", 3), ("/r/scan.pdf", 2)] and later.limits == [None, 120.0]
    assert (report.read, report.failed, report.second_tries) == (1, 1, 1)
    assert state(store, "/r/scan.pdf")[1] == "OCR failed on 1 of 3 page(s) (first: page 2, timed_out: the job did not finish in time and was ended)"


def test_ocr_without_second_tries_fails_a_timeout_at_once(store):
    scan(store, "/r/scan.pdf", pages=2)
    ocr = TimedOcr({("/r/scan.pdf", 2): SLOW})
    report = OcrRunner(store, ocr, Settings(retry_patience=1.0)).run()
    assert len(ocr.calls) == 1 and report.failed == 1 and report.second_tries == 0


def test_an_engine_that_only_times_out_pauses_without_failing_anything(store):
    scan(store, "/r/long.pdf", pages=9)
    ocr = ScriptedOcr({("/r/long.pdf", n): SLOW for n in range(2, 10)})
    report = OcrRunner(store, ocr, Settings(ocr_failures_in_a_row=3)).run()
    assert "ran out of time" in report.paused and report.failed == 0 and len(ocr.calls) == 3
    assert len(store.ocr_queue()) == 8, "every page still waits; three of them for their second try"
    assert sum(1 for t in store.ocr_queue() if t.retry) == 3


def test_ocr_stops_when_there_is_no_room(store):
    scan(store, "/r/long.pdf", pages=61)
    ocr = ScriptedOcr()
    assert OcrRunner(store, ocr, room=lambda: "no room").run().paused == "no room" and ocr.calls == []
    report = OcrRunner(store, ocr, room=lambda: "no room" if len(ocr.calls) >= 25 else "").run()
    assert report.paused == "no room" and 25 <= report.read < 60 and len(store.ocr_queue()) == 60 - report.read


# ---- OCR survives a re-read ----------------------------------------------------------------------------------

class PictureReader:
    """Every file is one picture page with no text of its own."""

    def extract(self, record, stop=None):
        return Extraction((), (PageInfo(1, PageVerdict.NO_LAYER, True),))


def test_reading_every_file_again_keeps_what_ocr_read(store_path):
    with SqliteStore(store_path) as store:
        w = World(["sign.png", "other.png"], PictureReader(), store=store)
        w.run()
        sign = f"{w.root}/sign.png"
        ocr = ScriptedOcr({(sign, 1): words("Antonio Trattoria")}, shapes={sign: (900, 600, True)})
        OcrRunner(store, ocr, case_insensitive=False).run(roots=[w.root])
        assert state(store, sign) == (FileState.DONE, "")

        w.settings = Settings(chunk_chars=900)                 # a setting that shapes the index changed
        report = w.run(on_settings_change=REREAD)
        assert report.read == 2, "both files were read again"
        assert state(store, sign) == (FileState.DONE, ""), "and the picture is still done, not waiting again"
        assert store.search_words("trattoria").total == 1
        assert store.ocr_queue() == []
        again = ScriptedOcr()
        assert OcrRunner(store, again).run().total == 0 and again.calls == [], "nothing is read by OCR twice"

        w.disk.put(w.root, "sign.png", b"a different picture now")
        w.run()
        assert store.search_words("trattoria").total == 0, "a changed picture loses the words of the old one"
        assert state(store, sign) == (FileState.PARTIAL, "1 of 1 page(s) wait for OCR")
        task = store.ocr_queue()[0]
        assert (task.path, task.width) == (sign, None), "and it is measured and read afresh"


def test_a_page_ocr_failed_on_gets_a_fresh_chance_on_a_reread(store_path):
    with SqliteStore(store_path) as store:
        w = World(["sign.png"], PictureReader(), store=store)
        w.run()
        sign = f"{w.root}/sign.png"
        OcrRunner(store, ScriptedOcr({(sign, 1): Failure(FailureReason.CORRUPT, "bad")})).run()
        assert state(store, sign)[1].startswith("OCR failed on 1 of 1")
        w.settings = Settings(chunk_chars=900)
        w.run(on_settings_change=REREAD)
        assert state(store, sign) == (FileState.PARTIAL, "1 of 1 page(s) wait for OCR")


# ---- pages read by an older engine ---------------------------------------------------------------------------

class VersionedOcr(ScriptedOcr):
    def __init__(self, version, **kwargs):
        super().__init__(**kwargs)
        self.version = version

    def engine_id(self):
        return None if self.version is None else f"fake-ocr {self.version}"

    def _answer(self, path, page, stop):
        self.calls.append((path, page))
        return OcrResult((OcrLine(f"read by version {self.version.replace('.', ' ')}", Box(1, 1, 9, 9)),), "fake-ocr", self.version)


def test_pages_read_by_an_older_engine_are_read_again_only_when_asked(store):
    scan(store, "/r/scan.pdf", pages=3)
    OcrRunner(store, VersionedOcr("1.0")).run()
    assert dict(store.ocr_engines()) == {"fake-ocr 1.0": 2}
    newer = VersionedOcr("2.0")
    report = OcrRunner(store, newer).run()
    assert report.total == 0 and newer.calls == [], "a new version changes nothing by itself"
    report = OcrRunner(store, newer).run(reread_old=True)
    assert (report.requeued, report.read) == (1, 2) and len(newer.calls) == 2
    assert dict(store.ocr_engines()) == {"fake-ocr 2.0": 2}
    assert store.search_words("version 1").total == 0 and store.search_words("version 2").total == 1
    assert state(store, "/r/scan.pdf") == (FileState.DONE, "")
    assert OcrRunner(store, newer).run(reread_old=True).total == 0, "asking again finds nothing old"


def test_with_no_engine_to_compare_with_nothing_is_requeued(store):
    scan(store, "/r/scan.pdf", pages=2)
    OcrRunner(store, VersionedOcr("1.0")).run()
    for backend in (VersionedOcr(None), ScriptedOcr()):
        report = OcrRunner(store, backend).run(reread_old=True)
        assert report.requeued == 0 and report.total == 0


def test_the_real_backend_names_its_engine_without_starting_a_worker():
    from dugup.ocr import SandboxOcrBackend

    class NoSandbox:
        def run(self, *args, **kwargs):
            raise AssertionError("no worker is needed to ask which engine is installed")

    name = SandboxOcrBackend(NoSandbox()).engine_id()
    assert name is None or (name.startswith("rapidocr ") and len(name) > len("rapidocr "))


def test_an_index_from_before_engines_were_recorded_learns_them(store_path, monkeypatch):
    everything = list(store_module.MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", everything[:3])
    with SqliteStore(store_path) as old:
        assert old.schema_version() == 3
        old.conn.execute("INSERT INTO files(id, path, name, folder, size, modified, kind, state) "
                         "VALUES(1, '/d/scan.pdf', 'scan.pdf', '/d', 1, 1.0, 'pdf', 'partial')")
        old.conn.execute("INSERT INTO segments(id, file_id, page, ord, kind, text) VALUES(5, 1, 1, 1000000, 'ocr', 'oldword')")
        old.conn.execute("INSERT INTO fts(rowid, name, body) VALUES(5, 'scan.pdf', 'oldword')")
        old.conn.execute("INSERT INTO regions(segment_id, engine, label, x0, y0, x1, y1, text) "
                         "VALUES(5, 'rapidocr 3.10.0', 'line', 1, 1, 9, 9, 'oldword')")
        for page, needs, ocr in ((1, 0, "done"), (2, 0, "done"), (3, 1, ""), (4, 1, "failed")):
            old.conn.execute("INSERT INTO pages(file_id, page, verdict, needs_ocr, ocr) VALUES(1, ?, 'no_layer', ?, ?)",
                             (page, needs, ocr))
    monkeypatch.setattr(store_module, "MIGRATIONS", everything)
    with SqliteStore(store_path) as new:
        assert new.schema_version() == len(everything)
        assert dict(new.ocr_engines()) == {"rapidocr 3.10.0": 2}, "the page with no words is credited to the same engine"
        assert new.search_words("oldword").total == 1
        assert [t.page for t in new.ocr_queue()] == [3]


# ---- the command line ----------------------------------------------------------------------------------------

class Shell:
    def __init__(self, tmp_path):
        self.data = tmp_path / "data"
        self.docs = tmp_path / helpers.AWKWARD_FOLDER
        self.docs.mkdir()
        (self.docs / "notes.txt").write_text("plain notes with a needle", encoding="utf-8")

    def run(self, *argv):
        out = io.StringIO()
        code = cli.main([*argv, "--data-dir", str(self.data)], stdout=out, environ={})
        return code, out.getvalue()


@pytest.fixture
def sh(tmp_path):
    return Shell(tmp_path)


def test_adding_the_first_folder_says_what_is_free_and_offers_a_limit(sh):
    code, text = sh.run("roots", "add", str(sh.docs))
    assert code == 0 and "MB free where the index is kept" in text and "No spending limit is set" in text
    assert "dugup space --limit " in text
    other = sh.docs.parent / "second"
    other.mkdir()
    assert "spending limit" not in sh.run("roots", "add", str(other))[1], "said once, not with every folder"


def test_space_shows_and_sets_the_limit(sh):
    assert sh.run("space")[0] == 1, "nothing to show before an index exists"
    sh.run("roots", "add", str(sh.docs))
    sh.run("index")
    code, text = sh.run("space")
    assert code == 0 and "The index uses " in text and "Spending limit: none." in text and "dugup space --limit" in text
    assert "KB of index for each of the 1 file(s)" in text
    code, text = sh.run("space", "--limit", "750")
    assert code == 0 and "Spending limit: 750 MB (0% used)" in text
    assert (sh.data / "config.toml").read_text(encoding="utf-8") == "max_index_mb = 750\n"
    assert "Spending limit: none." in sh.run("space", "--limit", "0")[1]
    assert sh.run("space", "--limit", "-5")[0] == 1


def test_setting_the_limit_keeps_the_rest_of_the_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("# my settings\nocr_seconds = 300\nmax_index_mb = 10   # old\n\n[extensions]\nlog = \"text\"\nmax_index_mb = \"not this one\"\n",
                      encoding="utf-8")
    cli.set_config_value(config, "max_index_mb", 2000)
    assert config.read_text(encoding="utf-8") == ("# my settings\nocr_seconds = 300\nmax_index_mb = 2000\n\n[extensions]\n"
                                                  "log = \"text\"\nmax_index_mb = \"not this one\"\n")
    tables_only = tmp_path / "other.toml"
    tables_only.write_text("[extensions]\nlog = \"text\"\n", encoding="utf-8")
    cli.set_config_value(tables_only, "max_index_mb", 5)
    assert tables_only.read_text(encoding="utf-8") == "max_index_mb = 5\n[extensions]\nlog = \"text\"\n"
    from dugup.settings import load
    settings, notes = load(tables_only)
    assert settings.max_index_mb == 5 and notes == []


def test_a_reached_limit_pauses_indexing_and_search_still_works(sh, monkeypatch):
    sh.run("roots", "add", str(sh.docs))
    assert sh.run("index")[0] == 0
    (sh.docs / "more.txt").write_text("later words", encoding="utf-8")
    (sh.data / "config.toml").write_text("max_index_mb = 1\n", encoding="utf-8")
    monkeypatch.setattr(DataLayout, "index_bytes", lambda self: 2 * 1024 * 1024)
    code, text = sh.run("index")
    assert code == 1 and "Indexing paused itself" in text and "spending limit of 1 MB" in text
    assert "notes.txt" in sh.run("search", "needle")[1], "searching does not need room"
    assert "No files found" in sh.run("search", "later")[1]
    (sh.data / "config.toml").write_text("max_index_mb = 100\n", encoding="utf-8")
    assert sh.run("index")[0] == 0 and "more.txt" in sh.run("search", "later")[1]


def test_status_names_pages_read_by_another_ocr_version(sh, monkeypatch):
    (sh.docs / "a.png").write_bytes(build_png())
    old = VersionedOcr("1.0", shapes={})
    monkeypatch.setattr(service_module, "SandboxOcrBackend", lambda sandbox, settings: old)
    sh.run("roots", "add", str(sh.docs))
    sh.run("index")
    assert sh.run("ocr")[0] == 0
    assert "another OCR version" not in sh.run("status")[1]
    old.version = "2.0"
    code, text = sh.run("status")
    assert "1 page(s) were read by another OCR version (fake-ocr 1.0)" in text and "dugup ocr --reread-old" in text
    code, text = sh.run("ocr", "--reread-old")
    assert code == 0 and "1 file(s) had pages read by an older OCR version" in text and "OCR read 1 of 1" in text
    assert "another OCR version" not in sh.run("status")[1]


def test_a_paused_index_run_says_so(sh, monkeypatch):
    for n in range(4):
        (sh.docs / f"bad{n}.txt").write_text("x", encoding="utf-8")
    (sh.data).mkdir()
    (sh.data / "config.toml").write_text("reader_failures_in_a_row = 2\nisolate_readers = false\n", encoding="utf-8")

    class Dies:
        def extract(self, record, stop=None):
            return DEAD

    monkeypatch.setattr(service_module, "default_extractors", lambda settings: Dies())
    sh.run("roots", "add", str(sh.docs))
    code, text = sh.run("index")
    assert code == 1 and "Indexing paused itself: 2 files in a row" in text and "dugup index --retry-failed" in text
