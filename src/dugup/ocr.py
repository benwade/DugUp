"""OCR: reading words from pixels, in a worker process (CONTRACTS.md part 4).

`SandboxOcrBackend` is the OcrBackend the program uses. The engine (RapidOCR) never runs in the main
program: a picture that makes it hang or crash costs one worker and comes back as a stated Failure.
Its models ship inside the engine's package, so the worker needs no network, and has none.

Two things here are plain functions so they can be tested without an engine:
`lines_from_items` turns what an engine reports (pixel polygons) into lines with whole-page 0..1000
boxes in reading order, and `SandboxOcrBackend` only carries pictures to the worker and answers back.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from .jobs import OCR, PROBE, path_to_data
from .protocols import Limits, OcrLine, OcrResult, Sandbox, StopSignal
from .settings import Settings
from .shapes import Box, BoxError, Failure, FailureReason, box_from_polygon

ENGINE = "rapidocr"     # the engine behind this backend; another engine is another backend with its own name
LANGUAGES = ("en",)     # what the engine's bundled models have been tried on here
Shape = Optional[Tuple[int, int, bool]]   # (width, height, names a camera), or None when it cannot be opened


def reading_order(lines: Sequence[OcrLine]) -> List[OcrLine]:
    """Top to bottom, and left to right among lines that sit on the same row.

    Two lines are on the same row when the middle of one lies within the height of the row's first line.
    """
    rows: List[List[OcrLine]] = []
    for line in sorted(lines, key=lambda l: ((l.box.y0 + l.box.y1) / 2.0, l.box.x0)):
        middle = (line.box.y0 + line.box.y1) / 2.0
        if rows and rows[-1][0].box.y0 <= middle <= rows[-1][0].box.y1:
            rows[-1].append(line)
        else:
            rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda l: l.box.x0)]


def lines_from_items(items: Any, width: float, height: float) -> Tuple[OcrLine, ...]:
    """Lines from an engine's (text, score, polygon in pixels) items. Empty text and impossible boxes are dropped."""
    lines: List[OcrLine] = []
    for text, score, points in items:
        text = " ".join(str(text).split())
        if not text:
            continue
        try:
            box = box_from_polygon(points, width, height)
        except BoxError:
            continue
        confidence = None if score is None else min(1.0, max(0.0, float(score)))
        lines.append(OcrLine(text, box, confidence))
    return tuple(reading_order(lines))


def result_from_data(data: Any) -> "OcrResult | Failure":
    """What the worker answered, checked. Anything that is not a well-formed answer is a stated failure."""
    try:
        if "failure" in data:
            return Failure(FailureReason(data["failure"]), str(data["detail"]))
        width, height = float(data["width"]), float(data["height"])
        if width <= 0 or height <= 0:
            raise ValueError("a picture has a size")
        lines = lines_from_items(data["items"], width, height)
        return OcrResult(lines, str(data["engine"]), str(data["version"]))
    except (KeyError, TypeError, ValueError) as exc:
        return Failure(FailureReason.CRASHED, f"the OCR worker's answer could not be understood ({type(exc).__name__}: {exc})")


class SandboxOcrBackend:
    def __init__(self, sandbox: Sandbox, settings: Optional[Settings] = None, job: str = OCR,
                 probe_job: str = PROBE, threads: int = 0, gpu: bool = False) -> None:
        self.gpu = gpu              # ask the worker to use a graphics card; it uses the processor when it cannot
        self.last_device = ""       # what the last page was read on, in words ("processor", "graphics card (...)")
        self.threads = threads      # processor threads this reader's engine may use; 0 leaves it to the engine
        self.sandbox = sandbox
        self.settings = settings or Settings()
        self.job = job
        self.probe_job = probe_job

    # -- the contract

    def read(self, image: Path, language: str = "en", time_limit: Optional[float] = None,
             region: Optional[Box] = None, stop: Optional[StopSignal] = None) -> "OcrResult | Failure":
        return self._read(os.fspath(image), 0, language, time_limit, region, stop)

    # -- beyond the contract: a PDF page is drawn and read in the worker, so no picture of it is left on disk

    def read_page(self, pdf: Path, page: int, language: str = "en", time_limit: Optional[float] = None,
                  stop: Optional[StopSignal] = None) -> "OcrResult | Failure":
        if page < 1:
            raise ValueError("pages are numbered from 1")
        return self._read(os.fspath(pdf), page, language, time_limit, None, stop)

    def patient_limit(self, patience: float) -> float:
        """The time limit of a second try (see OcrRunner)."""
        return self.settings.ocr_seconds * max(1.0, patience)

    def _limits(self, time_limit: Optional[float]) -> Limits:
        seconds = self.settings.ocr_seconds if time_limit is None else time_limit
        return Limits(seconds, self.settings.ocr_memory_mb or None)

    def _read(self, path: str, page: int, language: str, time_limit: Optional[float], region: Optional[Box],
              stop: Optional[StopSignal]) -> "OcrResult | Failure":
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped before reading began")
        if language not in LANGUAGES:
            return Failure(FailureReason.UNSUPPORTED,
                           f"OCR in language {language!r} is not set up (available: {', '.join(LANGUAGES)})")
        payload = {"path": path_to_data(path), "page": page, "dpi": self.settings.ocr_dpi,
                   "max_pixels": self.settings.ocr_max_pixels, "threads": self.threads, "gpu": self.gpu,
                   "region": None if region is None else [region.x0, region.y0, region.x1, region.y1]}
        answer = self.sandbox.run(self.job, payload, self._limits(time_limit), stop)
        if isinstance(answer, Failure):
            return answer
        if isinstance(answer, dict) and answer.get("device"):
            self.last_device = str(answer["device"])
        return result_from_data(answer)

    def card(self, stop: Optional[StopSignal] = None) -> str:
        """The graphics card route this machine's OCR runtime has, in words, or "" when it has none.
        Asked of a worker, so the main program never loads the runtime."""
        answer = self.sandbox.run("dugup.jobs:ocr_device", {}, self._limits(None), stop)
        return str(answer.get("card") or "") if isinstance(answer, dict) else ""

    def probe(self, paths: Sequence[str], stop: Optional[StopSignal] = None) -> "List[Shape] | Failure":
        """The size of each picture and whether it names a camera. Used to order the queue; nothing is decoded."""
        answer = self.sandbox.run(self.probe_job, {"paths": [path_to_data(p) for p in paths]}, self._limits(None), stop)
        if isinstance(answer, Failure):
            return answer
        try:
            if "failure" in answer:
                return Failure(FailureReason(answer["failure"]), str(answer["detail"]))
            shapes: List[Shape] = [None if s is None else (int(s[0]), int(s[1]), bool(s[2])) for s in answer["shapes"]]
            if len(shapes) != len(paths):
                raise ValueError("one answer is needed for each picture")
            return shapes
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            return Failure(FailureReason.CRASHED, f"the worker's answer could not be understood ({type(exc).__name__}: {exc})")

    def engine_id(self) -> Optional[str]:
        """The engine's name and version as stored with every page it reads, or None when it is not installed.
        Asked of the package's metadata, so no worker is started and the engine is not loaded."""
        try:
            from importlib.metadata import PackageNotFoundError, version
            return f"{ENGINE} {version(ENGINE)}"
        except Exception:
            return None

    def close(self) -> None:
        close = getattr(self.sandbox, "close", None)
        if close is not None:
            close()
