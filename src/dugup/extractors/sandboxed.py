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

from ..jobs import EXTRACT, record_to_data, result_from_data
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
        if stopped(stop):
            return STOPPED
        if record.kind is FileKind.IMAGE:
            return self._image.extract(record, stop)
        if record.kind not in PARSED_IN_A_WORKER:
            return Failure(FailureReason.UNSUPPORTED, f"no reader for this kind of file ({record.kind.value})")
        limits = Limits(self.settings.reader_limit(record.size), self.settings.reader_memory_mb or None)
        answer = self.sandbox.run(EXTRACT, {"record": record_to_data(record), "settings": self._settings_data}, limits, stop)
        if isinstance(answer, Failure):
            return answer
        return result_from_data(answer)

    def close(self) -> None:
        close = getattr(self.sandbox, "close", None)
        if close is not None:
            close()
