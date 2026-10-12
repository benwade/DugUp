"""A fake OcrBackend. Its "pictures" are small JSON files that list the lines they pretend to show."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from dugup.protocols import OcrLine, OcrResult, StopSignal
from dugup.shapes import Box, Failure, FailureReason

LineSpec = Tuple[str, Tuple[int, int, int, int]]   # (text, (x0, y0, x1, y1) on the 0..1000 scale)


class FakeOcr:
    engine = "fake-ocr"
    version = "1.0"
    languages = ("en",)

    def __init__(self, hang: Optional[threading.Event] = None, crash: bool = False) -> None:
        self.hang = hang      # when given, the "engine" is stuck until this event is set
        self.crash = crash

    # --- the contract
    def read(self, image: Path, language: str = "en", time_limit: float = 60.0,
             region: Optional[Box] = None, stop: Optional[StopSignal] = None):
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped before reading began")
        if language not in self.languages:
            return self._unsupported(language)
        loaded = self._load(Path(image))
        if isinstance(loaded, (Failure, OcrResult)):
            return loaded
        if self.crash:
            return self._crashed()
        if self.hang is not None:
            stuck = self._wait_while_stuck(time_limit, stop)
            if stuck is not None:
                return stuck
        width, height, lines = loaded
        out: List[OcrLine] = []
        for text, (x0, y0, x1, y1) in sorted(lines, key=lambda s: (s[1][1], s[1][0])):
            if region is not None and not self._inside(region, (x0, y0, x1, y1)):
                continue
            out.append(OcrLine(self._text(text), self._box(Box(x0, y0, x1, y1), width, height), 0.9))
        return OcrResult(tuple(out), self.engine, self.version)

    # --- pieces the broken variants replace
    def _unsupported(self, language):
        return Failure(FailureReason.UNSUPPORTED, f"language {language!r} is not supported")

    def _crashed(self):
        return Failure(FailureReason.CRASHED, "the engine process died")

    def _text(self, text: str) -> str:
        return text

    def _box(self, box: Box, width: int, height: int) -> Box:
        return box

    def _inside(self, region: Box, box: Tuple[int, int, int, int]) -> bool:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return region.x0 <= cx <= region.x1 and region.y0 <= cy <= region.y1

    def _damaged(self, why: str):
        return Failure(FailureReason.CORRUPT, why)

    def _wait_while_stuck(self, time_limit: float, stop: Optional[StopSignal]):
        deadline = time.monotonic() + time_limit
        while not self.hang.is_set():
            if stop is not None and stop.is_set():
                return Failure(FailureReason.STOPPED, "stopped while reading")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return Failure(FailureReason.TIMED_OUT, f"no answer within {time_limit} seconds")
            self.hang.wait(min(0.01, remaining))
        return None

    def _load(self, image: Path):
        try:
            raw = image.read_bytes()
        except OSError as exc:
            return Failure(FailureReason.OTHER, f"cannot open the picture ({type(exc).__name__})")
        try:
            data = json.loads(raw.decode("utf-8"))
            return int(data["width"]), int(data["height"]), [(l[0], tuple(l[1])) for l in data["lines"]]
        except (ValueError, KeyError, TypeError, IndexError):
            return self._damaged("the picture cannot be decoded")


# ---- deliberately broken variants ------------------------------------------------------------------------

class EmptyOnCrash(FakeOcr):
    def _crashed(self):
        return OcrResult((), self.engine, self.version)


class RaisesOnCrash(FakeOcr):
    def _crashed(self):
        raise RuntimeError("engine died")


class EmptyForUnsupportedLanguage(FakeOcr):
    def _unsupported(self, language):
        return OcrResult((), self.engine, self.version)


class SwallowsDamagedPictures(FakeOcr):
    def _damaged(self, why):
        return OcrResult((), self.engine, self.version)


class IgnoresTimeLimit(FakeOcr):
    def _wait_while_stuck(self, time_limit, stop):
        while not self.hang.is_set():
            if stop is not None and stop.is_set():
                return Failure(FailureReason.STOPPED, "stopped while reading")
            self.hang.wait(0.01)
        return None


class IgnoresStop(FakeOcr):
    def read(self, image, language="en", time_limit=60.0, region=None, stop=None):
        return super().read(image, language, time_limit, region, None)


class DropsNonAscii(FakeOcr):
    def _text(self, text):
        return "".join(c for c in text if ord(c) < 128)


class IgnoresRegion(FakeOcr):
    def _inside(self, region, box):
        return True


class PixelBoxes(FakeOcr):
    """Reports boxes in the picture's own scale instead of 0..1000."""

    def _box(self, box, width, height):
        def s(v, size):
            return min(1000, int(round(v * size / 1000)))
        return Box(s(box.x0, width), s(box.y0, height), s(box.x1, width), s(box.y1, height))


class Harness:
    slack = 1.0   # how long a check waits on a part that should already have answered (kept short for the fake)

    def __init__(self, backend_class=FakeOcr, directory: Optional[Path] = None) -> None:
        import tempfile
        self.backend_class = backend_class
        self._tmp = None
        if directory is None:
            self._tmp = tempfile.TemporaryDirectory()
            directory = Path(self._tmp.name)
        self.directory = Path(directory)
        self._count = 0
        self.backend = backend_class()

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()

    def _write(self, content: bytes) -> Path:
        self._count += 1
        path = self.directory / f"picture{self._count}.img"
        path.write_bytes(content)
        return path

    def picture(self, lines: Sequence[LineSpec], width: int = 800, height: int = 1000) -> Path:
        data = {"width": width, "height": height, "lines": [[t, list(b)] for t, b in lines]}
        return self._write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def blank_picture(self) -> Path:
        return self.picture([])

    def damaged_picture(self) -> Path:
        return self._write(b"\x89not a picture at all\x00\xff")

    def missing_picture(self) -> Path:
        return self.directory / "never-created.img"

    def hanging_backend(self, release: threading.Event):
        return self.backend_class(hang=release)

    def crashing_backend(self):
        return self.backend_class(crash=True)
