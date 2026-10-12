"""The real OCR engine, in a real worker process. Skipped, with the reason, where the engine is not installed.

Pictures are drawn here (generated words only). The engine reads clean print well, so these tests ask for
the words; they do not measure how well it reads hard pictures (see EXPERIMENT_LOG.md for that).
"""

import io
import os
import threading

import pytest

pytest.importorskip("PIL", reason="Pillow is not installed (pip install rapidocr onnxruntime)")
pytest.importorskip("rapidocr", reason="the OCR engine is not installed (pip install rapidocr onnxruntime)")
pytest.importorskip("onnxruntime", reason="onnxruntime is not installed (pip install rapidocr onnxruntime)")
pytest.importorskip("pypdfium2", reason="the PDF reader pypdfium2 is not installed (pip install -r requirements.txt)")

import helpers
from conformance.ocr_checks import CHECKS
from corpus.pictures import draw_lines, save, sign
from dugup import cli
from dugup.ocr import SandboxOcrBackend
from dugup.protocols import OcrResult
from dugup.sandbox import SubprocessSandbox
from dugup.shapes import Box, Failure, FailureReason
from test_sandbox import TESTS

pytestmark = pytest.mark.real_tool

# The engine's bundled models read English (and Chinese); Greek and accented words are outside what they
# are set up for here, so that one check cannot apply. Everything else in the contract does.
NOT_FOR_THIS_ENGINE = {"non_ascii_text_survives"}


@pytest.fixture(scope="module")
def sandbox():
    with SubprocessSandbox(extra_paths=[TESTS]) as box:
        yield box


class RealHarness:
    slack = 60.0     # a first read starts a worker and loads the engine's models

    def __init__(self, folder, sandbox):
        self.folder = folder / helpers.AWKWARD_FOLDER      # spaces, an accent, CJK and a dash, on purpose
        self.folder.mkdir(parents=True, exist_ok=True)
        self.sandbox = sandbox
        self.backend = SandboxOcrBackend(sandbox)
        self._count = 0

    def _path(self, ext="png"):
        self._count += 1
        return self.folder / f"{helpers.CAFE} picture {self._count}.{ext}"

    def picture(self, lines, width=800, height=1000):
        return save(draw_lines(lines, width, height), self._path())

    def blank_picture(self):
        return save(draw_lines([], 400, 300), self._path())

    def damaged_picture(self):
        path = self._path()
        path.write_bytes(b"\x89PNG\r\n\x1a\n this is not a picture at all \x00\xff" * 4)
        return path

    def missing_picture(self):
        return self.folder / "never-created.png"

    def hanging_backend(self, release):
        return SandboxOcrBackend(self.sandbox, job="sandbox_jobs:hang_quietly")

    def crashing_backend(self):
        return SandboxOcrBackend(self.sandbox, job="sandbox_jobs:die")


@pytest.mark.parametrize("name", sorted(set(CHECKS) - NOT_FOR_THIS_ENGINE))
def test_the_engine_in_a_worker_meets_the_ocr_contract(name, tmp_path, sandbox):
    CHECKS[name](RealHarness(tmp_path, sandbox))


def texts(result):
    assert isinstance(result, OcrResult), result
    return " ".join(line.text for line in result.lines).casefold()


def test_a_missing_picture_is_unreachable_not_damaged(tmp_path, sandbox):
    answer = SandboxOcrBackend(sandbox).read(tmp_path / "nothing-here.png")
    assert isinstance(answer, Failure) and answer.reason is FailureReason.UNREACHABLE


def test_a_nonsense_answer_from_the_worker_is_a_stated_failure(tmp_path, sandbox):
    answer = SandboxOcrBackend(sandbox, job="sandbox_jobs:answer_nonsense").read(tmp_path / "x.png")
    assert isinstance(answer, Failure) and answer.reason is FailureReason.CRASHED


def test_the_worker_stays_cut_off_from_the_network_and_is_reused(tmp_path, sandbox):
    backend = SandboxOcrBackend(sandbox)
    picture = save(sign(["Harbour Bakery"]), tmp_path / "sign.png")
    assert "bakery" in texts(backend.read(picture))
    started = sandbox.started
    assert "bakery" in texts(backend.read(picture))
    assert sandbox.started == started, "the engine is loaded once and kept"
    assert sandbox.describe()["network"] in ("namespace", "python")


def test_a_photo_saved_sideways_is_read_the_right_way_up(tmp_path, sandbox):
    from PIL import Image

    upright = sign(["Antonio Trattoria", "open daily"])
    exif = Image.Exif()
    exif[274] = 6                       # "rotate 90 degrees clockwise to view"
    path = save(upright.rotate(90, expand=True), tmp_path / "phone.jpg", exif=exif, quality=92)
    result = SandboxOcrBackend(sandbox).read(path)
    assert "trattoria" in texts(result) and "daily" in texts(result)
    top = next(l for l in result.lines if "trattoria" in l.text.casefold())
    assert top.box.x1 - top.box.x0 > top.box.y1 - top.box.y0, "the box is on the picture as a viewer shows it"


def test_a_very_large_picture_is_scaled_down_and_boxes_stay_whole_page(tmp_path, sandbox):
    from dugup.settings import Settings

    spec = [("Quarterly budget", (100, 100, 700, 180))]
    path = save(draw_lines(spec, 3000, 2400), tmp_path / "large.png")
    result = SandboxOcrBackend(sandbox, Settings(ocr_max_pixels=1000)).read(path)
    assert "budget" in texts(result)
    box = result.lines[0].box
    assert all(abs(a - b) <= 30 for a, b in zip((box.x0, box.y0, box.x1, box.y1), spec[0][1]))


def scanned_pdf(path, pages):
    """A PDF with nothing but a picture on each page: what a scanner makes."""
    first, *rest = [sign(words, 1240, 1754) for words in pages]
    first.save(path, save_all=True, append_images=rest, resolution=150.0)
    return path


def test_a_page_of_a_scanned_pdf_is_read(tmp_path, sandbox):
    path = scanned_pdf(tmp_path / "scan.pdf", [["First page", "meeting notes"], ["Second page", "garden plan"]])
    backend = SandboxOcrBackend(sandbox)
    assert "garden" in texts(backend.read_page(path, 2))
    assert "meeting" in texts(backend.read_page(path, 1))
    beyond = backend.read_page(path, 3)
    assert isinstance(beyond, Failure) and beyond.reason is FailureReason.CORRUPT
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 nothing that makes a document")
    assert backend.read_page(broken, 1).reason is FailureReason.CORRUPT


def test_pictures_are_measured_without_reading_them(tmp_path, sandbox):
    from PIL import Image

    plain = save(sign(["x"], 640, 480), tmp_path / "plain.png")
    exif = Image.Exif()
    exif[271], exif[272] = "ExampleCam", "Model 1"
    photo = save(sign(["x"], 800, 600), tmp_path / f"{helpers.CJK} photo.jpg", exif=exif)
    damaged = tmp_path / "damaged.png"
    damaged.write_bytes(b"not a picture")
    shapes = SandboxOcrBackend(sandbox).probe([str(plain), str(photo), str(damaged), str(tmp_path / "missing.png")])
    assert shapes == [(640, 480, False), (800, 600, True), None, None]


# ---- the whole way, through the command line --------------------------------------------------------------

def run(data, *argv):
    out = io.StringIO()
    code = cli.main([*argv, "--data-dir", str(data)], stdout=out, environ={})
    return code, out.getvalue()


def test_index_then_ocr_then_search_finds_the_words_in_pictures(tmp_path):
    docs = tmp_path / helpers.AWKWARD_FOLDER
    docs.mkdir()
    data = tmp_path / "data"
    (docs / "notes.txt").write_text("plain notes about the harbour", encoding="utf-8")
    save(sign(["Antonio Trattoria", "open daily"]), docs / "IMG_0001.jpg", quality=92)
    save(sign(["ok"], 64, 48), docs / "button.png")
    scanned_pdf(docs / "letter.pdf", [["Dear neighbour", "garden fence"]])
    (docs / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n not really")

    assert run(data, "roots", "add", str(docs))[0] == 0
    code, text = run(data, "index")
    assert code == 0 and "dugup ocr" in text
    assert "No files found" in run(data, "search", "trattoria")[1], "before OCR the words in a picture cannot be found"
    code, text = run(data, "status")
    assert "4 file(s) wait for OCR" in text and "dugup ocr" in text

    code, text = run(data, "ocr")
    assert code == 0, text
    assert "OCR read 3 of 4 waiting page(s)" in text and "1 failed" in text

    code, text = run(data, "search", "trattoria")
    assert code == 0 and "IMG_0001.jpg" in text and "in the picture: " in text and "[Trattoria]" in text
    code, text = run(data, "search", "garden", "fence")
    assert "letter.pdf" in text and "p.1, read by OCR" in text
    code, text = run(data, "status", "--attention")
    assert "wait for OCR" not in text
    assert "broken.png" in text and "OCR failed on 1 of 1 page(s)" in text and "corrupt" in text
    assert "done 4" in text and "partial 1" in text

    code, text = run(data, "ocr")
    assert code == 0 and "Nothing waits for OCR." in text, "a second run has nothing to do"
    code, text = run(data, "index")
    assert code == 0 and "Read 0 file(s)" in text, "indexing again does not undo what OCR stored"
    assert "IMG_0001.jpg" in run(data, "search", "trattoria")[1]

    log = (data / "logs" / "dugup.log").read_text(encoding="utf-8")
    for private in ("trattoria", "Trattoria", "IMG_0001", "letter.pdf", "broken.png", "garden"):
        assert private not in log, "the log holds counts, ids and reasons; never words or file names"
    assert "ocr failed" in log and "corrupt" in log


def test_a_changed_picture_is_read_again(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    data = tmp_path / "data"
    save(sign(["Harbour Bakery"]), docs / "sign.png")
    run(data, "roots", "add", str(docs))
    run(data, "index")
    assert run(data, "ocr")[0] == 0
    assert "sign.png" in run(data, "search", "bakery")[1]
    save(sign(["Mountain Library"]), docs / "sign.png")
    os.utime(docs / "sign.png", (2_000_000_000, 2_000_000_000))
    run(data, "index")
    assert "No files found" in run(data, "search", "bakery")[1], "the old words left with the old picture"
    assert run(data, "ocr")[0] == 0
    assert "sign.png" in run(data, "search", "library")[1]
