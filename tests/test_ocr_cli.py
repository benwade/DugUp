"""`dugup ocr` and Service.run_ocr with a scripted engine: what is said, what is returned, what is locked."""

import io
import threading

import pytest

import helpers
from corpus.images import build_png
from dugup import cli, service as service_module
from dugup.cli import ProgressPrinter
from dugup.datadir import DataLayout
from dugup.indexer import Progress
from dugup.service import LocalService
from dugup.shapes import Failure, FailureReason, FileState, Root
from test_ocrqueue import BLANK, ScriptedOcr, words


class Shell:
    def __init__(self, tmp_path, monkeypatch, ocr):
        self.data = tmp_path / "data"
        self.docs = tmp_path / helpers.AWKWARD_FOLDER
        self.docs.mkdir()
        self.ocr = ocr
        monkeypatch.setattr(service_module, "SandboxOcrBackend", lambda sandbox, settings: ocr)
        (self.docs / "notes.txt").write_text("plain notes", encoding="utf-8")
        for index, name in enumerate(("a.png", "b.png", "c.png")):
            (self.docs / name).write_bytes(build_png(seed=index))

    def path(self, name):
        return str(self.docs / name)

    def run(self, *argv, **more):
        out = io.StringIO()
        code = cli.main([*argv, "--data-dir", str(self.data)], stdout=out, environ={}, **more)
        return code, out.getvalue()

    def ready(self):
        assert self.run("roots", "add", str(self.docs))[0] == 0
        assert self.run("index", "--no-ocr")[0] == 0      # these tests look at the OCR command on its own
        return self


@pytest.fixture
def sh(tmp_path, monkeypatch):
    return Shell(tmp_path, monkeypatch, ScriptedOcr(shapes={}))


def test_before_an_index_exists_ocr_says_how_to_start(sh):
    code, text = sh.run("ocr")
    assert code == 1 and "roots add" in text and not sh.data.exists()


def test_ocr_reads_what_waits_and_reports_it(sh):
    sh.ready()
    sh.ocr.answers[(sh.path("b.png"), 1)] = BLANK
    code, text = sh.run("ocr")
    assert code == 0
    assert "OCR read 3 of 3 waiting page(s)" in text and "2 with words, 1 with none, 0 failed" in text
    code, text = sh.run("search", "words", "a.png")
    assert "a.png" in text and "in the picture: " in text
    code, text = sh.run("status")
    assert "wait for OCR" not in text and "done 4" in text
    assert "Nothing waits for OCR." in sh.run("ocr")[1]


def test_failures_are_reported_with_the_way_to_see_and_retry_them(sh):
    sh.ready()
    sh.ocr.answers[(sh.path("b.png"), 1)] = Failure(FailureReason.TIMED_OUT, "the job did not finish within 120 s and was ended")
    code, text = sh.run("ocr")
    assert code == 0 and "1 failed" in text and "dugup status --attention" in text and "dugup ocr --retry-failed" in text
    code, text = sh.run("status", "--attention")
    assert "b.png" in text and "OCR failed on 1 of 1 page(s) (first: page 1, timed_out: " in text
    del sh.ocr.answers[(sh.path("b.png"), 1)]
    assert "Nothing waits for OCR." in sh.run("ocr")[1], "a failed page is not retried unasked"
    code, text = sh.run("ocr", "--retry-failed")
    assert code == 0 and "OCR read 1 of 1 waiting page(s)" in text


def test_a_missing_engine_is_said_plainly_and_nothing_is_written_off(sh):
    sh.ready()
    for name in ("a.png", "b.png", "c.png"):
        sh.ocr.answers[(sh.path(name), 1)] = Failure(FailureReason.NOT_INSTALLED, "install it with: pip install rapidocr onnxruntime")
    code, text = sh.run("ocr")
    assert code == 1 and "OCR cannot run (not installed)" in text and "pip install rapidocr onnxruntime" in text
    assert "Nothing was marked as failed" in text and len(sh.ocr.calls) == 1
    assert "3 file(s) wait for OCR" in sh.run("status")[1]


def test_a_paused_run_says_so(sh):
    sh.ready()
    (sh.data / "config.toml").write_text("ocr_failures_in_a_row = 2\n", encoding="utf-8")
    for name in ("a.png", "b.png", "c.png"):
        sh.ocr.answers[(sh.path(name), 1)] = Failure(FailureReason.CRASHED, "the worker ended without an answer")
    code, text = sh.run("ocr")
    assert code == 1 and "OCR paused itself: 2 pages in a row failed" in text and "The rest stays queued" in text
    assert len(sh.ocr.calls) == 2


def test_stop_ends_cleanly_with_the_interrupted_code(sh):
    sh.ready()
    stop = threading.Event()
    sh.ocr.on_call = lambda path, page: stop.set() if len(sh.ocr.calls) == 2 else None
    out = io.StringIO()
    args = cli.build_parser().parse_args(["ocr", "--data-dir", str(sh.data)])
    code = cli.cmd_ocr(args, None, out, {}, stop=stop, install_signals=False)
    text = out.getvalue()
    assert code == 130 and "[Interrupted] Stopped cleanly" in text and "OCR read 1 of 3" in text
    code, text = sh.run("ocr")
    assert code == 0 and "OCR read 2 of 2 waiting page(s)" in text


def test_minutes_and_root_are_checked(sh):
    sh.ready()
    assert "--minutes must be above 0" in sh.run("ocr", "--minutes", "0")[1]
    code, text = sh.run("ocr", "--root", str(sh.docs.parent / "elsewhere"))
    assert code == 1 and "not an indexed folder" in text and sh.ocr.calls == []
    code, text = sh.run("ocr", "--root", str(sh.docs), "--minutes", "5")
    assert code == 0 and "OCR read 3 of 3" in text


def test_the_time_given_is_respected(sh, monkeypatch):
    sh.ready()
    now = [1000.0]
    sh.ocr.on_call = lambda path, page: now.__setitem__(0, now[0] + 45.0)
    monkeypatch.setattr("dugup.ocrqueue.time.monotonic", lambda: now[0])
    code, text = sh.run("ocr", "--minutes", "1")
    assert code == 0 and "The time given is used up; 1 page(s) still wait" in text


def test_progress_lines():
    out = io.StringIO()
    printer = ProgressPrinter(out, interval=0.0)
    printer(Progress("measure", "", 100, 400))
    printer(Progress("ocr", "", 30, 120, failed=2, elapsed=60.0, current=f"/pics/{helpers.CJK}.jpg"))
    printer(Progress("ocr", "", 0, 0))
    lines = out.getvalue().splitlines()
    assert lines[0] == "[ocr] looking at the size of the pictures: 100 of 400"
    assert lines[1] == f"[ocr] 30 of 120 page(s) (25.0%)  30.0 pages/min  about 3 min 00 s left  failed 2  | {helpers.CJK}.jpg"
    assert lines[2].startswith("[ocr] 0 of 0 page(s) (0.0%)  0.0 pages/min"), "no division by zero when nothing waits"


# ---- the service ------------------------------------------------------------------------------------------

def make_service(tmp_path, ocr, **more):
    layout = DataLayout(tmp_path / "data").ensure()
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "a.png").write_bytes(build_png())
    service = LocalService(layout, ocr_backend=ocr, **more)
    assert not isinstance(service.set_root(Root(str(docs))), Failure)
    return service, docs


def test_ocr_and_indexing_never_run_at_the_same_time(tmp_path):
    ocr = ScriptedOcr(shapes={})
    service, docs = make_service(tmp_path, ocr)
    with service:
        service.run_indexing()
        seen = {}
        ocr.on_call = lambda path, page: seen.update(index=service.run_indexing(), ocr=service.run_ocr())
        report = service.run_ocr()
        assert report.read == 1
        assert seen["index"].reason is FailureReason.BUSY and seen["ocr"].reason is FailureReason.BUSY
        assert not isinstance(service.run_indexing(), Failure), "the lock is given back afterwards"


def test_a_folder_that_is_away_is_left_alone(tmp_path):
    ocr = ScriptedOcr(shapes={})
    service, docs = make_service(tmp_path, ocr)

    class Away:
        def __init__(self, inner):
            self.inner = inner
            self.away = False

        def reachable(self, root):
            return not self.away

        def __getattr__(self, name):
            return getattr(self.inner, name)

    with service:
        service.run_indexing()
        service.source = Away(service.source)
        service.source.away = True
        report = service.run_ocr()
        assert report.away == (str(docs),) and report.total == 0 and ocr.calls == []
        service.source.away = False
        report = service.run_ocr()
        assert report.away == () and report.read == 1
        assert service.store.get_file(str(docs / "a.png")).state is FileState.DONE


def test_closing_the_service_ends_the_ocr_worker(tmp_path):
    class Closable(ScriptedOcr):
        closed = False

        def close(self):
            self.closed = True

    ocr = Closable(shapes={})
    service, _ = make_service(tmp_path, ocr)
    service.close()
    assert ocr.closed


# ---- `dugup index` goes on into OCR ---------------------------------------------------------------------------

def test_index_goes_straight_on_to_the_pictures(sh):
    sh.ocr.answers[(sh.path("a.png"), 1)] = words("needle on a sign")
    assert sh.run("roots", "add", str(sh.docs))[0] == 0
    code, text = sh.run("index")
    assert code == 0, text
    assert text.index("Read 4 file(s)") < text.index("Now reading 3 picture(s) and scanned page(s) (OCR)") < text.index("OCR read 3 of 3")
    assert "dugup index --no-ocr" in text and "Ctrl+C stops" in text
    assert "a.png" in sh.run("search", "needle")[1], "one command made the picture findable"
    code, text = sh.run("index")
    assert code == 0 and "Now reading" not in text, "nothing waits: no OCR step"


def test_index_leaves_ocr_out_when_asked_or_set_or_not_wanted_by_the_folder(sh):
    assert sh.run("roots", "add", str(sh.docs))[0] == 0
    code, text = sh.run("index", "--no-ocr")
    assert code == 0 and "Now reading" not in text and sh.ocr.calls == []
    (sh.data / "config.toml").write_text("ocr_after_index = false\n", encoding="utf-8")
    assert "Now reading" not in sh.run("index")[1] and sh.ocr.calls == []
    (sh.data / "config.toml").unlink()
    sh.run("roots", "level", str(sh.docs), "words")
    assert "Now reading" not in sh.run("index")[1] and sh.ocr.calls == [], "the folder asks for words only"
    sh.run("roots", "level", str(sh.docs), "ocr")
    assert "Now reading 3 picture(s)" in sh.run("index")[1]


def test_stopping_during_the_ocr_step_says_the_index_is_done(sh):
    assert sh.run("roots", "add", str(sh.docs))[0] == 0
    stop = threading.Event()
    sh.ocr.on_call = lambda path, page: stop.set()    # the stop request arrives while the first picture is read
    out = io.StringIO()
    args = cli.build_parser().parse_args(["index", "--data-dir", str(sh.data)])
    code = cli.cmd_index(args, None, out, {}, stop=stop, install_signals=False)
    text = out.getvalue()
    assert code == 130 and "The index is up to date; run `dugup ocr` to read the rest." in text
    assert "Read 4 file(s)" in text
