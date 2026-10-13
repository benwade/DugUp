"""Several OCR readers side by side, one folder first, and reading a folder again (the "fast" run)."""

import io
import threading

import pytest

import helpers
from dugup import cli, machine, service as service_module
from dugup.ocrqueue import OcrRunner
from dugup.settings import Settings
from dugup.shapes import Failure, FailureReason, FileState
from dugup.store import SqliteStore
from fakes.fake_store import FakeStore
from test_ocr_cli import Shell
from test_ocrqueue import ScriptedOcr, picture, scan, state, words

WAIT = 20.0      # a ceiling for "this must happen"; nothing here waits this long when the code is right


@pytest.fixture(params=["fake", "real"])
def store(request, tmp_path):
    made = FakeStore() if request.param == "fake" else SqliteStore(tmp_path / "i.sqlite")
    yield made
    close = getattr(made, "close", None)
    if close:
        close()


# ---- how many readers ------------------------------------------------------------------------------------------

def test_the_number_of_readers_follows_cores_and_memory():
    gb = 1024 ** 3
    assert machine.readers_for(0, 2, 1500, core_count=32, free_bytes=35 * gb) == 8, "half the cores, two threads each"
    assert machine.readers_for(0, 2, 1500, core_count=32, free_bytes=4 * gb) == 2, "no more than the free memory holds"
    assert machine.readers_for(0, 2, 1500, core_count=32, free_bytes=None) == 8, "memory unknown: it does not limit"
    assert machine.readers_for(0, 2, 1500, core_count=2, free_bytes=64 * gb) == 1 and machine.readers_for(0, 2, 1500, core_count=1) == 1
    assert machine.readers_for(0, 1, 1500, core_count=128, free_bytes=512 * gb) == 16, "never more than the ceiling"
    assert machine.readers_for(5, 2, 1500, core_count=2, free_bytes=1) == 5, "a number the person gave is used as given"
    assert machine.readers_for(0, 0, 0, core_count=0, free_bytes=0) == 1, "nothing here divides by zero"
    assert machine.cores() >= 1 and (machine.free_memory_bytes() is None or machine.free_memory_bytes() > 0)


# ---- side by side ----------------------------------------------------------------------------------------------

def test_two_readers_work_at_the_same_time_and_every_page_is_read_once(store):
    for n in range(6):
        picture(store, f"/r/p{n}.png")
    together = threading.Barrier(2, timeout=WAIT)     # passes only when two reads are going on at once

    def meet(path, page):
        together.wait()

    first, second = ScriptedOcr(shapes={}), ScriptedOcr(shapes={})
    first.on_call = second.on_call = meet
    report = OcrRunner(store, first, extra_backends=[second]).run()
    assert (report.total, report.read, report.failed, report.readers) == (6, 6, 0, 2)
    read = first.calls + second.calls
    assert sorted(read) == sorted((f"/r/p{n}.png", 1) for n in range(6)), "each page once, none twice"
    assert first.calls and second.calls, "both readers did some of the work"
    assert store.ocr_queue() == [] and all(state(store, f"/r/p{n}.png")[0] is FileState.DONE for n in range(6))
    assert len(store.search_words("words").rows) == 6


def test_one_reader_is_exactly_the_old_one_at_a_time_run(store):
    for n in range(3):
        picture(store, f"/r/p{n}.png")
    ocr = ScriptedOcr(shapes={})
    seen = []
    ocr.on_call = lambda path, page: seen.append(threading.current_thread() is threading.main_thread())
    report = OcrRunner(store, ocr).run()
    assert report.readers == 1 and report.read == 3 and all(seen), "no extra threads when there is one reader"


def test_stopping_a_fast_run_keeps_what_was_read_and_leaves_the_rest_queued(store):
    for n in range(8):
        picture(store, f"/r/p{n}.png")
    stop = threading.Event()
    release = threading.Event()
    first, second = ScriptedOcr(shapes={}), ScriptedOcr(shapes={})

    def slow(path, page):          # this reader is held until the stop request is certain to have been made
        assert release.wait(WAIT)

    def quick(path, page):
        if len(second.calls) == 2:
            stop.set()
            release.set()

    first.on_call, second.on_call = slow, quick
    report = OcrRunner(store, first, extra_backends=[second]).run(stop)
    assert report.stopped and 1 <= report.read < 8
    left = len(store.ocr_queue())
    assert report.read + left == 8, "every page is either stored or still waiting; none is lost or written off"
    again = OcrRunner(store, ScriptedOcr(shapes={})).run()
    assert again.read == left and store.ocr_queue() == []


def test_failures_are_stated_and_a_run_of_them_pauses_a_fast_run_too(store):
    for n in range(12):
        picture(store, f"/r/p{n}.png")
    crash = Failure(FailureReason.CRASHED, "the engine fell over")
    answers = {(f"/r/p{n}.png", 1): crash for n in range(12)}
    report = OcrRunner(store, ScriptedOcr(answers, shapes={}), Settings(ocr_failures_in_a_row=3),
                       extra_backends=[ScriptedOcr(answers, shapes={})]).run()
    assert report.paused and "in a row" in report.paused and 3 <= report.failed <= 5, "paused at once, not after the whole queue"
    assert report.failed + len(store.ocr_queue()) == 12

    for path in ("/s/bad.png", "/s/good1.png", "/s/good2.png"):
        picture(store, path)
    answers = {("/s/bad.png", 1): Failure(FailureReason.CORRUPT, "not a picture")}
    report = OcrRunner(store, ScriptedOcr(answers, shapes={}), extra_backends=[ScriptedOcr(answers, shapes={})]).run(roots=["/s"])
    assert (report.read, report.failed) == (2, 1) and "corrupt" in state(store, "/s/bad.png")[1]


def test_a_reader_that_raises_does_not_take_the_run_down(store):
    for n in range(4):
        picture(store, f"/r/p{n}.png")

    class Broken(ScriptedOcr):
        def read(self, image, **more):
            raise RuntimeError("boom")

    report = OcrRunner(store, ScriptedOcr(shapes={}), Settings(ocr_failures_in_a_row=99), extra_backends=[Broken(shapes={})]).run()
    assert report.read + report.failed == 4 and report.read >= 1, "the good reader carried on"


def test_a_folder_asked_for_first_is_read_before_the_rest(store):
    scan(store, "/r/Other/letter.pdf", pages=2)          # a document page would normally go first
    picture(store, "/r/Now/a.png")
    picture(store, "/r/Now/b.png")
    picture(store, "/r/Nowhere/c.png")
    ocr = ScriptedOcr(shapes={})
    OcrRunner(store, ocr, first="/r/Now").run()
    assert [path for path, _ in ocr.calls[:2]] == ["/r/Now/a.png", "/r/Now/b.png"], "that folder first"
    assert {path for path, _ in ocr.calls[2:]} == {"/r/Other/letter.pdf", "/r/Nowhere/c.png"}, "then everything else"


# ---- through the service and the command -----------------------------------------------------------------------

@pytest.fixture
def sh(tmp_path, monkeypatch):
    made = Shell(tmp_path, monkeypatch, ScriptedOcr(shapes={}))
    (made.docs / "Sub").mkdir()
    for name in ("d.png", "e.png"):
        # not the same bytes as a.png: a copy would take its text from a.png and never reach a reader
        (made.docs / "Sub" / name).write_bytes((made.docs / "a.png").read_bytes() + name.encode("ascii"))
    made.extra = []

    def more(self, count, gpu=False):
        made.extra = [ScriptedOcr(shapes={}) for _ in range(count)]
        made.asked_gpu = gpu
        for reader in made.extra:
            reader.close = lambda: made.closed.append(1)
            reader.last_device = made.device if gpu else "processor"
        return list(made.extra)

    made.device = "processor"          # what a reader asked for the card says it read on
    made.asked_gpu = None

    made.closed = []
    monkeypatch.setattr(service_module.LocalService, "_more_readers", more)
    return made


def test_fast_uses_the_readers_asked_for_and_closes_them(sh):
    sh.ready()
    code, text = sh.run("ocr", "--readers", "3")
    assert code == 0, text
    assert "Fast: 3 reader(s) side by side" in text and "busier" in text and "[3 readers side by side]" in text
    assert "OCR read 5 of 5 waiting page(s)" in text
    assert len(sh.extra) == 3 and len(sh.closed) == 3, "three readers of the limited kind, and all were closed"
    assert sh.ocr.calls == [] and sum(len(r.calls) for r in sh.extra) == 5, "the usual, unlimited reader takes no part"
    code, text = sh.run("ocr", "--fast")
    assert code == 0 and "Fast:" in text and "Nothing waits for OCR." in text


def test_one_folder_alone_and_reading_it_again(sh):
    sh.ready()
    sub = str(sh.docs / "Sub")
    code, text = sh.run("ocr", "--folder", sub)
    assert code == 0 and "OCR read 2 of 2 waiting page(s)" in text, text
    assert sorted(path for path, _ in sh.ocr.calls) == [str(sh.docs / "Sub" / "d.png"), str(sh.docs / "Sub" / "e.png")]
    code, text = sh.run("ocr", "--folder", sub)
    assert "Nothing waits for OCR." in text
    code, text = sh.run("ocr", "--folder", sub, "--again")
    assert code == 0 and "OCR read 2 of 2 waiting page(s)" in text, "read afresh, and only that folder"
    code, text = sh.run("ocr")
    assert "OCR read 3 of 3 waiting page(s)" in text, "the rest was still waiting"


def test_requests_that_cannot_be_met_are_said(sh, tmp_path):
    sh.ready()
    code, text = sh.run("ocr", "--again")
    assert code == 1 and "--again needs --folder" in text
    code, text = sh.run("ocr", "--folder", str(tmp_path / "elsewhere"))
    assert code == 1 and "not inside an indexed folder" in text
    assert sh.ocr.calls == []


# ---- the graphics card is an option ----------------------------------------------------------------------------

def test_the_processor_is_the_default_and_the_card_is_asked_for(sh):
    sh.ready()
    code, text = sh.run("ocr")
    assert code == 0 and "graphics card" not in text and sh.asked_gpu is None, "nothing about a card unless it is asked for"
    sh.device = "graphics card (DirectML)"
    code, text = sh.run("ocr", "--folder", str(sh.docs), "--again", "--gpu")
    assert code == 0 and "Read on the graphics card (DirectML)." in text and sh.asked_gpu is True
    assert len(sh.extra) == 1 and len(sh.closed) >= 1, "one reader on the card for an ordinary run, closed afterwards"


def test_fast_on_the_card_uses_a_few_readers_and_the_setting_turns_it_on(sh):
    sh.ready()
    (sh.data / "config.toml").write_text("ocr_gpu = true\nocr_gpu_readers = 3\n", encoding="utf-8")
    sh.device = "graphics card (CUDA)"
    code, text = sh.run("ocr", "--fast")
    assert code == 0 and sh.asked_gpu is True and len(sh.extra) == 3, "the setting asks for the card; fast uses ocr_gpu_readers"
    assert "Fast: 3 reader(s) side by side, sharing the graphics card" in text, "the count said is the count used"
    code, text = sh.run("ocr", "--folder", str(sh.docs), "--again", "--readers", "5")
    assert "Fast: 5 reader(s) side by side, sharing the graphics card" in text and len(sh.extra) == 5
    assert "Read on the graphics card (CUDA)." in text
    code, text = sh.run("ocr", "--folder", str(sh.docs), "--again", "--no-gpu")
    assert code == 0 and "graphics card" not in text, "--no-gpu wins over the setting"


def test_a_card_that_is_asked_for_but_cannot_be_used_is_said_and_the_work_is_done_all_the_same(sh):
    sh.ready()
    code, text = sh.run("ocr", "--gpu")           # the fake readers answer "processor": the runtime has no card route
    assert code == 0 and "OCR read 5 of 5 waiting page(s)" in text
    assert "A graphics card was asked for, but the OCR runtime installed here cannot use one" in text and "dugup doctor" in text


def test_the_worker_picks_the_card_route_the_runtime_has(monkeypatch):
    import sys
    import types
    from dugup import jobs

    def runtime(*providers):
        module = types.ModuleType("onnxruntime")
        module.get_available_providers = lambda: list(providers)
        monkeypatch.setitem(sys.modules, "onnxruntime", module)

    runtime("CPUExecutionProvider")
    assert jobs.gpu_provider() == "" and jobs.ocr_device({}) == {"card": "", "provider": ""}
    runtime("DmlExecutionProvider", "CPUExecutionProvider")
    assert jobs.gpu_provider() == "DmlExecutionProvider" and "DirectML" in jobs.ocr_device({})["card"]
    runtime("CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider")
    assert jobs.gpu_provider() == "CUDAExecutionProvider"
    broken = types.ModuleType("onnxruntime")
    monkeypatch.setitem(sys.modules, "onnxruntime", broken)
    assert jobs.gpu_provider() == "", "a runtime that cannot answer counts as no card"


def test_the_doctor_says_whether_a_card_can_be_used():
    from dugup.doctor import Environment
    lines = {name: detail for name, _, detail in _doctor(Environment(can_import=lambda name: True, ocr_card=lambda: "graphics card (DirectML)"))}
    assert "can be used: graphics card (DirectML)" in lines["ocr: graphics card"] and "ocr_gpu = true" in lines["ocr: graphics card"]
    lines = {name: detail for name, _, detail in _doctor(Environment(can_import=lambda name: True, ocr_card=lambda: ""))}
    assert "optional" in lines["ocr: graphics card"] and "onnxruntime-directml" in lines["ocr: graphics card"]


def _doctor(env):
    from dugup import doctor
    return [(check.name, check.status, check.detail) for check in doctor.run_checks(env)]
