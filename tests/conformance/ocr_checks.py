"""Conformance checks for OcrBackend (CONTRACTS.md part 4).

Each check takes a harness `h` with:
  h.backend                          the OcrBackend under test
  h.slack                            seconds to wait for a call that should already have answered
  h.picture(lines, width, height)    a picture showing lines [(text, (x0, y0, x1, y1) on 0..1000)] -> Path
  h.blank_picture() / h.damaged_picture() / h.missing_picture()
  h.hanging_backend(release_event)   a backend whose engine is stuck until the event is set
  h.crashing_backend()               a backend whose engine dies on every read
"""

from __future__ import annotations

import threading
import unicodedata

import helpers
from dugup.protocols import OcrResult
from dugup.shapes import Box, Failure, FailureReason, failed

from .common import call_with_deadline, require, require_failure, require_result

BOX_TOLERANCE = 30   # real engines do not reproduce a drawn box exactly


def _lines_of(h, **kwargs):
    top = ("Invoice 2041", (100, 100, 500, 140))
    middle = ("Total due", (100, 400, 400, 440))
    bottom = ("Thank you", (100, 800, 400, 840))
    return [bottom, top, middle]   # deliberately not in reading order


def _near(a: Box, b, tolerance=BOX_TOLERANCE) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip((a.x0, a.y0, a.x1, a.y1), b))


def check_reads_lines_in_reading_order(h):
    result = require_result(h.backend.read(h.picture(_lines_of(h))), "read")
    require(isinstance(result, OcrResult), "read: did not return an OcrResult")
    require([l.text for l in result.lines] == ["Invoice 2041", "Total due", "Thank you"],
            "read: the lines are missing or not in reading order (top to bottom)")
    require(isinstance(result.engine, str) and result.engine.strip(), "read: no engine name")
    require(isinstance(result.version, str) and result.version.strip(), "read: no engine version")
    for line in result.lines:
        require(line.confidence is None or 0.0 <= line.confidence <= 1.0, "read: confidence outside 0..1")


def check_boxes_are_whole_page_and_size_independent(h):
    spec = _lines_of(h)
    drawn = {t: b for t, b in spec}
    for width, height in ((600, 800), (2400, 3200)):
        result = require_result(h.backend.read(h.picture(spec, width, height)), f"read {width}x{height}")
        for line in result.lines:
            require(_near(line.box, drawn[line.text]),
                    f"read: the box for a line is not on the 0..1000 whole-page scale at {width}x{height}")


def check_blank_picture_is_empty_result(h):
    result = h.backend.read(h.blank_picture())
    require(not failed(result) and isinstance(result, OcrResult), "read: a blank picture must be a successful result")
    require(result.lines == (), "read: a blank picture must have no lines")


def check_non_ascii_text_survives(h):
    words = [helpers.CAFE + " " + helpers.MUELLER, helpers.GREEK, helpers.CJK]
    spec = [(w, (100, 100 + 200 * i, 600, 140 + 200 * i)) for i, w in enumerate(words)]
    result = require_result(h.backend.read(h.picture(spec)), "read")
    got = [unicodedata.normalize("NFC", l.text) for l in result.lines]
    want = [unicodedata.normalize("NFC", w) for w in words]
    require(got == want, "read: text with accents, Greek or Japanese letters was changed or lost")


def check_region_limits_the_reading_but_keeps_page_coordinates(h):
    spec = [("upper line", (100, 100, 500, 140)), ("lower line", (100, 800, 500, 840))]
    region = Box(0, 0, 1000, 500)
    result = require_result(h.backend.read(h.picture(spec), region=region), "read with region")
    require([l.text for l in result.lines] == ["upper line"], "read: a region must limit the reading to that region")
    require(_near(result.lines[0].box, (100, 100, 500, 140)), "read: boxes must stay in whole-page coordinates when a region is read")
    lower = require_result(h.backend.read(h.picture(spec), region=Box(0, 500, 1000, 1000)), "read lower region")
    require([l.text for l in lower.lines] == ["lower line"], "read: the lower region read the wrong text")
    require(_near(lower.lines[0].box, (100, 800, 500, 840)), "read: the lower region's box is not in whole-page coordinates")


def check_unsupported_language_is_stated(h):
    require_failure(_safe(lambda: h.backend.read(h.picture(_lines_of(h)), language="xx")), FailureReason.UNSUPPORTED,
                    "read in an unsupported language")


def check_damaged_picture_is_stated(h):
    require_failure(_safe(lambda: h.backend.read(h.damaged_picture())), FailureReason.CORRUPT, "read of a damaged picture")


def check_missing_picture_is_stated(h):
    value = _safe(lambda: h.backend.read(h.missing_picture()))
    require(not isinstance(value, BaseException), "read of a missing picture: raised instead of returning a Failure")
    require(failed(value), "read of a missing picture: must be a Failure, not a result")
    require(value.detail.strip(), "read of a missing picture: the Failure has no detail")


def check_crash_is_a_failure_not_silence_or_exception(h):
    backend = h.crashing_backend()
    require_failure(_safe(lambda: backend.read(h.picture(_lines_of(h)))), FailureReason.CRASHED, "read when the engine crashes")


def check_time_limit_is_kept(h):
    release = threading.Event()
    backend = h.hanging_backend(release)
    picture = h.picture(_lines_of(h))
    try:
        finished, value = call_with_deadline(lambda: backend.read(picture, time_limit=0.2), 0.2 + h.slack)
        require(finished, "read: did not give up after its time limit (a stuck engine hung the caller)")
        require_failure(value, FailureReason.TIMED_OUT, "read of a stuck engine")
    finally:
        release.set()


def check_stop_ends_a_stuck_read(h):
    release = threading.Event()
    backend = h.hanging_backend(release)
    picture = h.picture(_lines_of(h))
    stop = threading.Event()
    box: dict = {}

    def run():
        try:
            box["value"] = backend.read(picture, time_limit=60.0, stop=stop)
        except BaseException as exc:
            box["value"] = exc

    thread = threading.Thread(target=run, daemon=True)
    try:
        thread.start()
        stop.set()   # whether it lands before or after the engine gets stuck, the read must end
        thread.join(h.slack)
        require(not thread.is_alive(), "read: a stop request did not end a stuck read")
        require_failure(box.get("value"), FailureReason.STOPPED, "read stopped while stuck")
    finally:
        release.set()
        thread.join(h.slack)


def check_stop_before_start(h):
    stop = threading.Event()
    stop.set()
    require_failure(_safe(lambda: h.backend.read(h.picture(_lines_of(h)), stop=stop)), FailureReason.STOPPED,
                    "read with stop already set")


def _safe(fn):
    try:
        return fn()
    except Exception as exc:
        return exc


CHECKS = {
    "reads_lines_in_reading_order": check_reads_lines_in_reading_order,
    "boxes_are_whole_page_and_size_independent": check_boxes_are_whole_page_and_size_independent,
    "blank_picture_is_empty_result": check_blank_picture_is_empty_result,
    "non_ascii_text_survives": check_non_ascii_text_survives,
    "region_limits_the_reading_but_keeps_page_coordinates": check_region_limits_the_reading_but_keeps_page_coordinates,
    "unsupported_language_is_stated": check_unsupported_language_is_stated,
    "damaged_picture_is_stated": check_damaged_picture_is_stated,
    "missing_picture_is_stated": check_missing_picture_is_stated,
    "crash_is_a_failure_not_silence_or_exception": check_crash_is_a_failure_not_silence_or_exception,
    "time_limit_is_kept": check_time_limit_is_kept,
    "stop_ends_a_stuck_read": check_stop_ends_a_stuck_read,
    "stop_before_start": check_stop_before_start,
}
