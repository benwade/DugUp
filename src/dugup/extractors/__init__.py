"""Extractors: one per family of file, and the set that picks between them (CONTRACTS.md part 3).

`ExtractorSet` is itself an Extractor, so the indexer holds one object and never asks what kind a file is.
A family can be replaced or added without touching the others:

    extractors = default_extractors(settings)
    extractors.register(FileKind.PDF, MyOtherPdfReader())
"""

from __future__ import annotations

from typing import Dict, Optional

from ..protocols import Extraction, Extractor, StopSignal
from ..settings import Settings
from ..shapes import Failure, FailureReason, FileKind, FileRecord
from .image import ImageExtractor
from .office import OfficeExtractor
from .pdf import PdfExtractor
from .textfile import TextFileExtractor

__all__ = ["ExtractorSet", "default_extractors", "ImageExtractor", "OfficeExtractor", "PdfExtractor",
           "TextFileExtractor"]


class ExtractorSet:
    def __init__(self, by_kind: Optional[Dict[FileKind, Extractor]] = None) -> None:
        self._by_kind: Dict[FileKind, Extractor] = dict(by_kind or {})

    def register(self, kind: FileKind, extractor: Extractor) -> None:
        self._by_kind[kind] = extractor

    def extract(self, record: FileRecord, stop: Optional[StopSignal] = None) -> "Extraction | Failure":
        extractor = self._by_kind.get(record.kind)
        if extractor is None:
            return Failure(FailureReason.UNSUPPORTED, f"no reader for this kind of file ({record.kind.value})")
        return extractor.extract(record, stop)

    def content_mark(self, record: FileRecord, stop: Optional[StopSignal] = None) -> "str | Failure":
        """The fingerprint of the file's content (contentmark.py), read here, in this program."""
        from .. import contentmark as printing
        try:
            found = printing.of_file(record.path, stop)
        except FileNotFoundError as exc:
            return Failure(FailureReason.UNREACHABLE, f"the file was not there ({type(exc).__name__})")
        except OSError as exc:
            return Failure(FailureReason.OTHER, f"the file could not be read ({type(exc).__name__})")
        return found if found is not None else Failure(FailureReason.STOPPED, "stopped while the file was read")


def default_extractors(settings: Optional[Settings] = None) -> ExtractorSet:
    settings = settings or Settings()
    return ExtractorSet({
        FileKind.PDF: PdfExtractor(settings),
        FileKind.TEXT: TextFileExtractor(settings),
        FileKind.OFFICE: OfficeExtractor(settings),
        FileKind.IMAGE: ImageExtractor(),
    })
