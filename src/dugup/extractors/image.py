"""Images: nothing to read without OCR, so the answer is "one page, needs OCR" (CONTRACTS.md part 3)."""

from __future__ import annotations

from typing import Optional

from ..protocols import Extraction, PageInfo, PageVerdict, StopSignal
from ..shapes import Failure, FileRecord
from .common import STOPPED, stopped


class ImageExtractor:
    def extract(self, record: FileRecord, stop: Optional[StopSignal] = None) -> "Extraction | Failure":
        if stopped(stop):
            return STOPPED
        return Extraction((), (PageInfo(1, PageVerdict.NO_LAYER, True),))
