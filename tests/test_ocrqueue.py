"""The OCR queue, with a scripted engine and the real store: order, resume, stop, and every kind of failure."""

import threading

import pytest

from dugup.indexer import only_waits_for_ocr
from dugup.ocrqueue import (DOCUMENT_PAGE, DOCUMENT_PICTURE, PHOTO, TINY, OcrRunner, is_ocr_note, ocr_notes, priority)
from dugup.protocols import (OCR_DONE, OCR_FAILED, OCR_WAITING, OcrLine, OcrResult, OcrTask, PageInfo, PageVerdict)
from dugup.settings import Settings
from dugup.shapes import Box, Failure, FailureReason, FileKind, FileRecord, FileState, SegmentIn, SegmentKind


def words(text="some words"):
    return OcrResult((OcrLine(text, Box(10, 10, 500, 60), 0.9),), "fake-ocr", "1.0")


BLANK = OcrResult((), "fake-ocr", "1.0")


class ScriptedOcr:
    """Answers from a table keyed by (path, page); anything not listed reads as words naming the file."""

    def __init__(self, answers=None, shapes=None):
        self.answers = dict(answers or {})
        self.shapes = shapes            # path -> (w, h, camera) | None; no table means "cannot measure"
        self.calls = []
        self.probed = []
        self.on_call = None

    def _answer(self, path, page, stop):
        self.calls.append((path, page))
        if self.on_call is not None:
            self.on_call(path, page)
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped")
        answer = self.answers.get((path, page), None)
        return words(f"words of {path.rsplit('/', 1)[-1]} page {page}") if answer is None else answer

    def read(self, image, language="en", time_limit=None, region=None, stop=None):
        return self._answer(str(image), 1, stop)

    def read_page(self, pdf, page, language="en", time_limit=None, stop=None):
        return self._answer(str(pdf), page, stop)

    def probe(self, paths, stop=None):
        self.probed.append(list(paths))
        if self.shapes is None:
            return Failure(FailureReason.NOT_INSTALLED, "no picture library")
        return [self.shapes.get(p) for p in paths]


def picture(store, path, note=None):
    row = store.upsert_file(FileRecord(path=path, size=10, modified=1.0, kind=FileKind.IMAGE, state=FileState.PARTIAL,
                                       error="1 of 1 page(s) wait for OCR" if note is None else note))
    store.set_pages(row, [PageInfo(1, PageVerdict.NO_LAYER, True)])
    return row


def scan(store, path, pages=3, layer_text=""):
    """A PDF whose page 1 is born digital and whose other pages need OCR."""
    note = f"{pages - 1} of {pages} page(s) wait for OCR"
    row = store.upsert_file(FileRecord(path=path, size=10, modified=1.0, kind=FileKind.PDF, state=FileState.PARTIAL, error=note))
    store.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, layer_text or "typed text on page one")])
    store.set_pages(row, [PageInfo(1, PageVerdict.BORN_DIGITAL, False)]
                    + [PageInfo(n, PageVerdict.NO_LAYER, True) for n in range(2, pages + 1)])
    return row


def state(store, path):
    record = store.get_file(path)
    return record.state, record.error


# ---- order -----------------------------------------------------------------------------------------------

def test_priority_classes():
    def task(kind=FileKind.IMAGE, width=None, height=None, camera=False):
        return OcrTask(1, "/x", kind, 1, width, height, camera)

    assert priority(task(FileKind.PDF), 200) == DOCUMENT_PAGE
    assert priority(task(width=1200, height=800), 200) == DOCUMENT_PICTURE
    assert priority(task(width=1200, height=800, camera=True), 200) == PHOTO
    assert priority(task(), 200) == PHOTO, "a picture not measured yet is neither first nor last"
    assert priority(task(width=0, height=0), 200) == PHOTO, "a picture that could not be measured likewise"
    assert priority(task(width=199, height=40), 200) == TINY
    assert priority(task(width=199, height=40, camera=True), 200) == TINY
    assert priority(task(width=200, height=40), 200) == DOCUMENT_PICTURE, "the cut-off is the longest side UNDER the setting"
    assert priority(task(width=40, height=200), 200) == DOCUMENT_PICTURE
    assert priority(task(width=150, height=150), 0) == DOCUMENT_PICTURE, "a cut-off of 0 turns the tiny class off"


def test_documents_first_then_document_pictures_then_photos_and_tiny_last(store):
    picture(store, "/r/icon.png")
    picture(store, "/r/holiday.jpg")
    picture(store, "/r/screenshot.png")
    picture(store, "/r/unknown.gif")
    scan(store, "/r/letter.pdf", pages=3)
    shapes = {"/r/icon.png": (48, 48, False), "/r/holiday.jpg": (4000, 3000, True),
              "/r/screenshot.png": (1920, 1080, False), "/r/unknown.gif": None}
    ocr = ScriptedOcr(shapes=shapes)
    report = OcrRunner(store, ocr).run()
    assert ocr.calls == [("/r/letter.pdf", 2), ("/r/letter.pdf", 3), ("/r/screenshot.png", 1), ("/r/holiday.jpg", 1),
                         ("/r/unknown.gif", 1), ("/r/icon.png", 1)]
    assert (report.total, report.read, report.with_words, report.failed) == (6, 6, 6, 0)
    assert sorted(ocr.probed[0]) == sorted(shapes), "only pictures are measured, each once"
    ocr.probed.clear()
    picture(store, "/r/new.png")
    OcrRunner(store, ScriptedOcr(shapes={"/r/new.png": (10, 10, False)})).run()
    assert store.ocr_queue() == []


def test_shapes_are_remembered_so_a_picture_is_measured_once(store):
    picture(store, "/r/a.png")
    stop = threading.Event()
    first = ScriptedOcr(shapes={"/r/a.png": (900, 900, False)})
    first.on_call = lambda path, page: stop.set()
    assert OcrRunner(store, first).run(stop).stopped
    second = ScriptedOcr(shapes={})
    OcrRunner(store, second).run()
    assert second.probed == [], "the shape was stored by the first run"


def test_without_a_way_to_measure_the_queue_still_runs(store):
    picture(store, "/r/a.png")
    scan(store, "/r/b.pdf", pages=2)
    ocr = ScriptedOcr(shapes=None)
    report = OcrRunner(store, ocr).run()
    assert ocr.calls == [("/r/b.pdf", 2), ("/r/a.png", 1)] and report.read == 2

    class NoProbe:
        def __init__(self):
            self.calls = []

        def read(self, image, language="en", stop=None):
            self.calls.append(image)
            return BLANK

    picture(store, "/r/c.png")
    plain = NoProbe()
    assert OcrRunner(store, plain).run().read == 1 and plain.calls == ["/r/c.png"]


# ---- what a page's outcome does to its file ---------------------------------------------------------------

def test_read_pages_become_searchable_and_the_file_becomes_done(store):
    row = scan(store, "/r/letter.pdf", pages=3)
    ocr = ScriptedOcr({("/r/letter.pdf", 2): words("Antonio Trattoria"), ("/r/letter.pdf", 3): BLANK})
    report = OcrRunner(store, ocr).run()
    assert (report.read, report.with_words) == (2, 1)
    assert state(store, "/r/letter.pdf") == (FileState.DONE, "")
    found = store.search_words("trattoria").rows
    assert [r.file_id for r in found] == [row] and found[0].evidence[0].page == 2
    assert found[0].evidence[0].box == Box(10, 10, 500, 60)
    assert store.search_words("typed").total == 1, "the text the file already had is kept"


def test_the_note_counts_down_page_by_page(store):
    scan(store, "/r/letter.pdf", pages=4)
    seen = []
    ocr = ScriptedOcr()
    ocr.on_call = lambda path, page: seen.append(state(store, path))
    OcrRunner(store, ocr).run()
    assert seen == [(FileState.PARTIAL, "3 of 4 page(s) wait for OCR"), (FileState.PARTIAL, "2 of 4 page(s) wait for OCR"),
                    (FileState.PARTIAL, "1 of 4 page(s) wait for OCR")]
    assert state(store, "/r/letter.pdf") == (FileState.DONE, "")


def test_other_notes_of_the_file_are_kept(store):
    other = "1 page(s) could not be read (first: page 5, the page could not be parsed)"
    picture(store, "/r/a.png", note=f"1 of 1 page(s) wait for OCR; {other}; pages after 5000 were not read")
    OcrRunner(store, ScriptedOcr()).run()
    assert state(store, "/r/a.png") == (FileState.PARTIAL, f"{other}; pages after 5000 were not read")


def test_a_failed_page_is_stated_and_leaves_the_queue(store):
    scan(store, "/r/letter.pdf", pages=3)
    ocr = ScriptedOcr({("/r/letter.pdf", 2): Failure(FailureReason.CORRUPT, "bad; very bad")})
    report = OcrRunner(store, ocr).run()
    assert (report.read, report.failed) == (1, 1)
    status, note = state(store, "/r/letter.pdf")
    assert status is FileState.PARTIAL
    assert note == "OCR failed on 1 of 3 page(s) (first: page 2, corrupt: bad, very bad)"
    assert not only_waits_for_ocr(note), "a file OCR failed on needs attention; it is not merely waiting"
    assert store.ocr_queue() == []
    again = ScriptedOcr()
    assert OcrRunner(store, again).run().total == 0 and again.calls == [], "a failed page is not tried again on its own"
    report = OcrRunner(store, again).run(retry_failed=True)
    assert again.calls == [("/r/letter.pdf", 2)] and report.read == 1
    assert state(store, "/r/letter.pdf") == (FileState.DONE, "")


def test_waiting_and_failed_pages_are_both_in_the_note(store):
    scan(store, "/r/letter.pdf", pages=4)
    stop = threading.Event()
    ocr = ScriptedOcr({("/r/letter.pdf", 2): Failure(FailureReason.TIMED_OUT, "too slow")})
    ocr.on_call = lambda path, page: stop.set() if page == 3 else None
    assert OcrRunner(store, ocr).run(stop).stopped
    assert state(store, "/r/letter.pdf")[1] == ("2 of 4 page(s) wait for OCR; "
                                              "OCR failed on 1 of 4 page(s) (first: page 2, timed_out: too slow)")


def test_retry_only_touches_the_folder_asked_for(store):
    picture(store, "/a/one.png")
    picture(store, "/b/two.png")
    bad = ScriptedOcr({("/a/one.png", 1): Failure(FailureReason.CORRUPT, "x"), ("/b/two.png", 1): Failure(FailureReason.CORRUPT, "x")})
    OcrRunner(store, bad, case_insensitive=False).run()
    good = ScriptedOcr()
    OcrRunner(store, good, case_insensitive=False).run(roots=["/a"], retry_failed=True)
    assert good.calls == [("/a/one.png", 1)]
    assert state(store, "/a/one.png")[0] is FileState.DONE and state(store, "/b/two.png")[0] is FileState.PARTIAL


def test_a_file_that_failed_to_read_is_not_touched(store):
    row = picture(store, "/r/a.png")
    store.set_state(row, FileState.FAILED, "crashed: the reader died")
    OcrRunner(store, ScriptedOcr()).refresh_file("/r/a.png")
    assert state(store, "/r/a.png") == (FileState.FAILED, "crashed: the reader died")
    OcrRunner(store, ScriptedOcr()).refresh_file("/r/never-heard-of-it.png")   # must not raise


def test_notes_helpers():
    assert ocr_notes([], 3) == []
    assert ocr_notes([(1, OCR_DONE, "")], 1) == []
    assert ocr_notes([(1, OCR_WAITING, ""), (2, OCR_FAILED, "a"), (3, OCR_FAILED, "b")], 5) == [
        "1 of 5 page(s) wait for OCR", "OCR failed on 2 of 5 page(s) (first: page 2, a)"]
    assert is_ocr_note("3 of 9 page(s) wait for OCR") and is_ocr_note("OCR failed on 1 of 2 page(s) (first: page 1, x)")
    assert not is_ocr_note("1 page(s) could not be read (first: page 5, x)")


# ---- stop, resume, time ---------------------------------------------------------------------------------

def test_stop_between_pages_and_resume(store):
    scan(store, "/r/letter.pdf", pages=5)
    stop = threading.Event()
    ocr = ScriptedOcr()

    def after_two(path, page):
        if len(ocr.calls) == 3:
            stop.set()          # arrives while page 4 is being read: that read answers "stopped"

    ocr.on_call = after_two
    report = OcrRunner(store, ocr).run(stop)
    assert report.stopped and (report.read, report.failed, report.remaining) == (2, 0, 2)
    assert [p for p, s, _ in store.ocr_status(store.get_file("/r/letter.pdf").row_id) if s == OCR_WAITING] == [4, 5]
    later = ScriptedOcr()
    report = OcrRunner(store, later).run()
    assert later.calls == [("/r/letter.pdf", 4), ("/r/letter.pdf", 5)], "the next run carries on; nothing is read twice"
    assert not report.stopped and state(store, "/r/letter.pdf")[0] is FileState.DONE


def test_stop_already_set_reads_nothing(store):
    picture(store, "/r/a.png")
    stop = threading.Event()
    stop.set()
    ocr = ScriptedOcr(shapes={})
    report = OcrRunner(store, ocr).run(stop)
    assert report.stopped and ocr.calls == [] and ocr.probed == []


def test_an_engine_that_ignores_stop_is_still_stopped_between_pages(store):
    scan(store, "/r/letter.pdf", pages=4)
    stop = threading.Event()

    class Deaf(ScriptedOcr):
        def read_page(self, pdf, page, language="en", time_limit=None, stop=None):
            return super().read_page(pdf, page, language, time_limit, None)

    ocr = Deaf()
    ocr.on_call = lambda path, page: stop.set()
    report = OcrRunner(store, ocr).run(stop)
    assert report.stopped and ocr.calls == [("/r/letter.pdf", 2)] and report.read == 1


def test_the_time_budget_ends_the_run_between_pages(store):
    scan(store, "/r/letter.pdf", pages=6)
    now = [0.0]
    ocr = ScriptedOcr()
    ocr.on_call = lambda path, page: now.__setitem__(0, now[0] + 40.0)
    report = OcrRunner(store, ocr, timer=lambda: now[0]).run(budget_seconds=100.0)
    assert report.out_of_time and not report.stopped
    assert (report.read, report.remaining) == (3, 2), "pages are started while there is time left"
    assert report.seconds == 120.0 and report.rate == pytest.approx(3 / 120.0)


def test_progress_is_reported_with_counts(store):
    scan(store, "/r/letter.pdf", pages=3)
    picture(store, "/r/a.png")
    seen = []
    ocr = ScriptedOcr({("/r/letter.pdf", 3): Failure(FailureReason.CORRUPT, "x")}, shapes={"/r/a.png": (500, 500, False)})
    OcrRunner(store, ocr, progress=seen.append).run()
    reading = [(p.done, p.total, p.failed, p.current) for p in seen if p.phase == "ocr"]
    assert reading == [(0, 3, 0, "/r/letter.pdf"), (1, 3, 0, "/r/letter.pdf"), (2, 3, 1, "/r/a.png"), (3, 3, 1, "")]
    assert [p.phase for p in seen][0] == "measure"


def test_an_empty_queue_is_a_quiet_success(store):
    report = OcrRunner(store, ScriptedOcr()).run()
    assert (report.total, report.read, report.stopped, report.rate, report.remaining) == (0, 0, False, 0.0, 0)


# ---- failures that are about the run, not the page -------------------------------------------------------

@pytest.mark.parametrize("reason", [FailureReason.NOT_INSTALLED, FailureReason.BUSY, FailureReason.UNSUPPORTED])
def test_an_engine_that_cannot_run_writes_nothing_off(store, reason):
    scan(store, "/r/letter.pdf", pages=4)
    ocr = ScriptedOcr({("/r/letter.pdf", 2): Failure(reason, "cannot run")})
    report = OcrRunner(store, ocr).run()
    assert report.not_ready == Failure(reason, "cannot run") and len(ocr.calls) == 1
    assert (report.read, report.failed) == (0, 0) and len(store.ocr_queue()) == 3
    assert state(store, "/r/letter.pdf") == (FileState.PARTIAL, "3 of 4 page(s) wait for OCR")


def test_a_file_that_cannot_be_reached_keeps_its_place(store):
    picture(store, "/r/away.png")
    picture(store, "/r/here.png")
    ocr = ScriptedOcr({("/r/away.png", 1): Failure(FailureReason.UNREACHABLE, "the file is not there right now")},
                      shapes={"/r/away.png": None, "/r/here.png": (500, 500, False)})
    report = OcrRunner(store, ocr).run()
    assert (report.read, report.failed, report.left) == (1, 0, 1)
    assert [t.path for t in store.ocr_queue()] == ["/r/away.png"]
    assert state(store, "/r/away.png") == (FileState.PARTIAL, "1 of 1 page(s) wait for OCR")


def test_missing_files_are_not_in_the_queue(store):
    row = picture(store, "/r/gone.png")
    store.set_missing(row, 5.0)
    ocr = ScriptedOcr()
    assert OcrRunner(store, ocr).run().total == 0 and ocr.calls == []


def test_a_run_of_engine_failures_pauses_the_run(store):
    scan(store, "/r/long.pdf", pages=10)
    ocr = ScriptedOcr({("/r/long.pdf", n): Failure(FailureReason.CRASHED, "the worker died") for n in range(2, 11)})
    report = OcrRunner(store, ocr, Settings(ocr_failures_in_a_row=3)).run()
    assert "3 pages in a row failed" in report.paused and "crashed" in report.paused
    assert (report.failed, len(ocr.calls), len(store.ocr_queue())) == (3, 3, 6), "the rest of the queue is left alone"


def test_damaged_pictures_do_not_pause_the_run_and_a_success_resets_the_count(store):
    scan(store, "/r/long.pdf", pages=9)
    answers = {("/r/long.pdf", n): Failure(FailureReason.CORRUPT, "damaged") for n in (2, 3, 4, 5)}
    answers.update({("/r/long.pdf", n): Failure(FailureReason.TIMED_OUT, "slow") for n in (6, 8)})
    report = OcrRunner(store, ScriptedOcr(answers), Settings(ocr_failures_in_a_row=2)).run()
    assert report.paused == "" and (report.read, report.failed) == (2, 6)


def test_an_engine_that_returns_nothing_is_a_stated_failure(store):
    picture(store, "/r/a.png")

    class Silent(ScriptedOcr):
        def read(self, image, language="en", time_limit=None, region=None, stop=None):
            return None

    report = OcrRunner(store, Silent(shapes={})).run()
    assert report.failed == 1 and "returned nothing" in state(store, "/r/a.png")[1]


def test_each_page_is_stored_as_it_is_read(store):
    """A run that dies loses at most the page it was on."""
    scan(store, "/r/letter.pdf", pages=4)

    class Dies(ScriptedOcr):
        def read_page(self, pdf, page, language="en", time_limit=None, stop=None):
            if page == 4:
                raise RuntimeError("the program is killed here")
            return super().read_page(pdf, page, language, time_limit, stop)

    with pytest.raises(RuntimeError):
        OcrRunner(store, Dies()).run()
    assert [(t.path, t.page) for t in store.ocr_queue()] == [("/r/letter.pdf", 4)]
    assert store.search_words("words page 3").total == 1
    assert state(store, "/r/letter.pdf") == (FileState.PARTIAL, "1 of 4 page(s) wait for OCR")
