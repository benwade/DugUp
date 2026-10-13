"""SandboxedExtractor: the ordinary readers, run in a worker process.

It is an Extractor like any other, so the indexer does not know the difference. What changes is what a
bad file can do: a parser that hangs is ended after the file's time limit, one that crashes takes a
worker with it and not the program, and a file that needs absurd memory is refused. Each of those comes
back as a stated Failure and the file is listed under Needs attention.

Only files that need parsing go to a worker. A kind with no reader, and an image (which is only marked
for OCR), are answered here without the cost of a round trip.

A stop request ends the worker at once, in the middle of a file if need be, and the file simply stays
in the queue for the next run. That is safe because a worker never writes to the index, and it keeps
Ctrl+C prompt even when a reader is stuck on a file with a long time limit.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any, Dict, Optional

from ..jobs import EXTRACT, CONTENT_MARK, path_to_data, record_to_data, result_from_data
from ..protocols import Extraction, Limits, Sandbox, StopSignal
from ..settings import Settings
from ..shapes import Failure, FailureReason, FileKind, FileRecord
from .common import STOPPED, stopped
from .image import ImageExtractor

PARSED_IN_A_WORKER = (FileKind.PDF, FileKind.TEXT, FileKind.OFFICE)


def settings_to_data(settings: Settings) -> Dict[str, Any]:
    """The settings a worker needs to read files the same way, as plain data."""
    data: Dict[str, Any] = {}
    for item in fields(settings):
        value = getattr(settings, item.name)
        if item.name == "extensions":
            data[item.name] = {ext: kind.value for ext, kind in value.items()}
        else:
            data[item.name] = value
    return data


class SandboxedExtractor:
    def __init__(self, sandbox: Sandbox, settings: Optional[Settings] = None) -> None:
        self.sandbox = sandbox
        self.settings = settings or Settings()
        self._settings_data = settings_to_data(self.settings)
        self._image = ImageExtractor()

    def extract(self, record: FileRecord, stop: Optional[StopSignal] = None) -> "Extraction | Failure":
        return self.extract_patiently(record, stop, 1.0)

    def extract_patiently(self, record: FileRecord, stop: Optional[StopSignal] = None,
                          patience: float = 2.0) -> "Extraction | Failure":
        """The same read with the time limit multiplied: the second try of a file that ran out of time."""
        if stopped(stop):
            return STOPPED
        if record.kind is FileKind.IMAGE:
            return self._image.extract(record, stop)
        if record.kind not in PARSED_IN_A_WORKER:
            return Failure(FailureReason.UNSUPPORTED, f"no reader for this kind of file ({record.kind.value})")
        limits = Limits(self.settings.reader_limit(record.size) * max(1.0, patience), self.settings.reader_memory_mb or None)
        answer = self.sandbox.run(EXTRACT, {"record": record_to_data(record), "settings": self._settings_data}, limits, stop)
        if isinstance(answer, Failure):
            return answer
        return result_from_data(answer)

    def content_mark(self, record: FileRecord, stop: Optional[StopSignal] = None) -> "str | Failure":
        """The fingerprint of the file's content (contentmark.py), read in the worker under the file's time limit."""
        if stopped(stop):
            return STOPPED
        limits = Limits(self.settings.reader_limit(record.size), self.settings.reader_memory_mb or None)
        answer = self.sandbox.run(CONTENT_MARK, {"path": path_to_data(record.path)}, limits, stop)
        if isinstance(answer, Failure):
            return answer
        try:
            if "failure" in answer:
                return Failure(FailureReason(answer["failure"]), str(answer["detail"]))
            return str(answer["content"])
        except (KeyError, TypeError, ValueError) as exc:
            return Failure(FailureReason.CRASHED, f"the worker's answer could not be understood ({type(exc).__name__})")

    def close(self) -> None:
        close = getattr(self.sandbox, "close", None)
        if close is not None:
            close()
