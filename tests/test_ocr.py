"""OCR plumbing that needs no engine: engine output to lines, and the backend's talk with its worker."""

import threading

import pytest

import helpers
from dugup import jobs
from dugup.ocr import SandboxOcrBackend, lines_from_items, reading_order, result_from_data
from dugup.protocols import Limits, OcrBackend, OcrLine, OcrResult
from dugup.settings import Settings
from dugup.shapes import Box, Failure, FailureReason


def square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def test_pixel_polygons_become_whole_page_boxes_in_reading_order():
    items = [["Thank you", 0.9, square(100, 800, 400, 850)],
             ["right", 0.8, square(600, 105, 900, 150)],
             ["left", 0.7, square(100, 100, 400, 150)]]
    lines = lines_from_items(items, 1000, 2000)
    assert [l.text for l in lines] == ["left", "right", "Thank you"], "top to bottom, and left to right on one row"
    assert lines[0].box == Box(100, 50, 400, 75), "pixels on a 1000 x 2000 page, on the 0..1000 scale"
    assert lines[0].confidence == 0.7


def test_a_tilted_line_gets_the_box_around_it():
    lines = lines_from_items([["tilted", None, [[10, 20], [110, 10], [112, 40], [12, 50]]]], 200, 100)
    assert lines[0].box == Box(50, 100, 560, 500) and lines[0].confidence is None


def test_empty_text_and_impossible_boxes_are_dropped_and_scores_are_kept_in_range():
    items = [["   ", 0.9, square(0, 0, 10, 10)],
             ["off the page", 0.9, square(5000, 5000, 6000, 6000)],
             ["kept  with \n spaces", 1.7, square(0, 0, 10, 10)],
             ["low", -0.2, square(0, 50, 10, 60)]]
    lines = lines_from_items(items, 100, 100)
    assert [(l.text, l.confidence) for l in lines] == [("kept with spaces", 1.0), ("low", 0.0)]


def test_a_box_that_sticks_out_of_the_page_is_cut_to_it():
    lines = lines_from_items([["edge", 0.5, square(-20, 90, 50, 130)]], 100, 100)
    assert lines[0].box == Box(0, 900, 500, 1000)


def test_reading_order_groups_a_row_by_the_height_of_its_first_line():
    a = OcrLine("a", Box(500, 100, 600, 200))
    b = OcrLine("b", Box(100, 140, 200, 240))     # its middle (190) is inside a's height: same row, further left
    c = OcrLine("c", Box(100, 210, 200, 300))     # its middle (255) is below: the next row
    assert [l.text for l in reading_order([c, a, b])] == ["b", "a", "c"]


def test_worker_answers_are_checked():
    good = result_from_data({"width": 100, "height": 100, "engine": "e", "version": "1",
                             "items": [["word", 0.5, square(0, 0, 10, 10)]]})
    assert isinstance(good, OcrResult) and good.lines[0].text == "word" and (good.engine, good.version) == ("e", "1")
    stated = result_from_data({"failure": "corrupt", "detail": "bad picture"})
    assert stated == Failure(FailureReason.CORRUPT, "bad picture")
    for nonsense in ({}, {"items": 5, "width": 1, "height": 1, "engine": "e", "version": "1"}, None, [1],
                     {"failure": "no such reason", "detail": "x"},
                     {"width": 0, "height": 0, "engine": "e", "version": "1", "items": [["w", 0.5, "zz"]]}):
        answer = result_from_data(nonsense)
        assert isinstance(answer, Failure) and answer.reason is FailureReason.CRASHED and answer.detail


class RecordingSandbox:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []
        self.closed = False

    def run(self, job, payload, limits, stop=None):
        self.calls.append((job, dict(payload), limits, stop))
        return self.answer(payload) if callable(self.answer) else self.answer

    def close(self):
        self.closed = True


ONE_LINE = {"width": 10, "height": 10, "engine": "e", "version": "1", "items": [["hi", 0.5, square(1, 1, 5, 5)]]}


def test_the_backend_fits_the_contract_and_sends_what_the_worker_needs():
    box = RecordingSandbox(ONE_LINE)
    settings = Settings(ocr_seconds=33.0, ocr_memory_mb=512, ocr_dpi=99, ocr_max_pixels=1234)
    backend = SandboxOcrBackend(box, settings)
    assert isinstance(backend, OcrBackend)
    path = f"/pics/{helpers.CAFE} {helpers.CJK}.png"
    stop = threading.Event()
    result = backend.read(path, region=Box(0, 0, 1000, 500), stop=stop)
    assert isinstance(result, OcrResult) and [l.text for l in result.lines] == ["hi"]
    job, payload, limits, passed = box.calls[0]
    assert job == jobs.OCR and passed is stop
    assert jobs.path_from_data(payload["path"]) == path
    assert (payload["page"], payload["dpi"], payload["max_pixels"], payload["region"]) == (0, 99, 1234, [0, 0, 1000, 500])
    assert limits == Limits(33.0, 512)
    backend.read(path, time_limit=7.0)
    assert box.calls[1][2].seconds == 7.0, "a time limit given for one read is used for that read"
    backend.read_page("/docs/scan.pdf", 4)
    assert box.calls[2][1]["page"] == 4 and box.calls[2][1]["region"] is None
    with pytest.raises(ValueError):
        backend.read_page("/docs/scan.pdf", 0)
    backend.close()
    assert box.closed


def test_no_memory_limit_is_the_default_for_ocr():
    box = RecordingSandbox(ONE_LINE)
    SandboxOcrBackend(box).read("/x.png")
    assert box.calls[0][2].memory_mb is None


def test_stop_and_language_are_settled_before_any_worker_is_used():
    box = RecordingSandbox(ONE_LINE)
    backend = SandboxOcrBackend(box)
    stop = threading.Event()
    stop.set()
    assert backend.read("/x.png", stop=stop).reason is FailureReason.STOPPED
    answer = backend.read("/x.png", language="xx")
    assert answer.reason is FailureReason.UNSUPPORTED and "xx" in answer.detail
    assert box.calls == []


@pytest.mark.parametrize("reason", [FailureReason.TIMED_OUT, FailureReason.CRASHED, FailureReason.OUT_OF_MEMORY,
                                    FailureReason.STOPPED])
def test_a_worker_failure_is_passed_on_as_it_is(reason):
    backend = SandboxOcrBackend(RecordingSandbox(Failure(reason, "why")))
    assert backend.read("/x.png") == Failure(reason, "why")
    assert backend.probe(["/x.png"]) == Failure(reason, "why")


def test_probe_answers_are_checked():
    box = RecordingSandbox({"shapes": [[640, 480, True], None]})
    backend = SandboxOcrBackend(box)
    assert backend.probe(["/a.jpg", "/b.jpg"]) == [(640, 480, True), None]
    assert box.calls[0][0] == jobs.PROBE and len(box.calls[0][1]["paths"]) == 2
    short = SandboxOcrBackend(RecordingSandbox({"shapes": [[1, 1, False]]})).probe(["/a.jpg", "/b.jpg"])
    assert isinstance(short, Failure) and short.reason is FailureReason.CRASHED, "one answer for each picture, or none at all"
    missing = SandboxOcrBackend(RecordingSandbox({"failure": "not_installed", "detail": "no pillow"})).probe(["/a.jpg"])
    assert missing == Failure(FailureReason.NOT_INSTALLED, "no pillow")


def test_awkward_paths_survive_the_trip():
    for path in (f"C:\\Users\\{helpers.MUELLER}\\{helpers.CJK} {helpers.EMOJI}.jpg", "/data/bad\udcff name.png"):
        sent = jobs.path_to_data(path)
        import json
        assert jobs.path_from_data(json.loads(json.dumps(sent))) == path
