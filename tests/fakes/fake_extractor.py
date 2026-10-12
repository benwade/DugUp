"""A fake Extractor driven by a description of the document ("spec") instead of a real file."""

from __future__ import annotations

from typing import Dict, Optional

from conformance.extractor_checks import DocSpec, PageSpec
from dugup.protocols import Extraction, PageInfo, PageVerdict, StopSignal
from dugup.shapes import Failure, FailureReason, FileKind, FileRecord, SegmentIn, SegmentKind

KINDS = {"pdf": FileKind.PDF, "text": FileKind.TEXT, "office": FileKind.OFFICE, "image": FileKind.IMAGE}


class FakeExtractor:
    def __init__(self, registry: Dict[str, DocSpec]) -> None:
        self.registry = registry

    def extract(self, record: FileRecord, stop: Optional[StopSignal] = None):
        if self._look(stop):
            return Failure(FailureReason.STOPPED, "stopped before reading began")
        spec = self.registry.get(record.path)
        if spec is None:
            return Failure(FailureReason.OTHER, "the fake knows nothing about this file")
        if spec.password:
            return self._password()
        if spec.corrupt:
            return self._corrupt()
        if spec.kind == "text":
            return Extraction((SegmentIn(0, 0, SegmentKind.TEXT, spec.text),) if spec.text.strip() else ())
        if spec.kind == "office":
            return Extraction((SegmentIn(0, 0, SegmentKind.NATIVE, spec.text),) if spec.text.strip() else ())
        if spec.kind == "image":
            return Extraction((), (PageInfo(1, PageVerdict.NO_LAYER, True),))
        segments, pages = [], []
        for number, page in enumerate(spec.pages, 1):
            if self._look_between_pages(stop):
                return Failure(FailureReason.STOPPED, f"stopped before page {number}")
            self._page(number, page, segments, pages)
        return Extraction(tuple(segments), tuple(pages))

    # --- pieces the broken variants replace
    def _look(self, stop):
        return stop is not None and stop.is_set()

    def _look_between_pages(self, stop):
        return stop is not None and stop.is_set()

    def _password(self):
        return Failure(FailureReason.PASSWORD_PROTECTED, "the file needs a password")

    def _corrupt(self):
        return Failure(FailureReason.CORRUPT, "the file is damaged")

    def _page(self, number, page, segments, pages):
        text = SegmentIn(number, 0, SegmentKind.TEXT, page.text)
        if page.layer == "born_digital":
            segments.append(text)
            pages.append(PageInfo(number, PageVerdict.BORN_DIGITAL, False))
        elif page.layer == "scanner":
            segments.append(text)
            pages.append(PageInfo(number, PageVerdict.SCANNER_LAYER, True))
        elif page.layer == "garbled":
            pages.append(PageInfo(number, PageVerdict.GARBLED, True))
        elif page.layer == "none":
            pages.append(PageInfo(number, PageVerdict.NO_LAYER, page.ink))
        elif page.layer == "unreadable":
            pages.append(PageInfo(number, PageVerdict.UNREADABLE, False, "the page could not be parsed"))
        else:
            raise ValueError(f"unknown layer {page.layer!r}")


# ---- deliberately broken variants ------------------------------------------------------------------------

class DropsGarbledPages(FakeExtractor):
    def _page(self, number, page, segments, pages):
        if page.layer != "garbled":
            super()._page(number, page, segments, pages)


class IndexesGarbledText(FakeExtractor):
    def _page(self, number, page, segments, pages):
        super()._page(number, page, segments, pages)
        if page.layer == "garbled":
            segments.append(SegmentIn(number, 0, SegmentKind.TEXT, page.text))


class ScannerLayerNotSentToOcr(FakeExtractor):
    def _page(self, number, page, segments, pages):
        super()._page(number, page, segments, pages)
        if page.layer == "scanner":
            pages[-1] = PageInfo(number, PageVerdict.SCANNER_LAYER, False)


class BornDigitalSentToOcr(FakeExtractor):
    def _page(self, number, page, segments, pages):
        super()._page(number, page, segments, pages)
        if page.layer == "born_digital":
            pages[-1] = PageInfo(number, PageVerdict.BORN_DIGITAL, True)


class BlankPagesSentToOcr(FakeExtractor):
    def _page(self, number, page, segments, pages):
        super()._page(number, page, segments, pages)
        if page.layer == "none":
            pages[-1] = PageInfo(number, PageVerdict.NO_LAYER, True)


class DropsUnreadablePages(FakeExtractor):
    def _page(self, number, page, segments, pages):
        if page.layer != "unreadable":
            super()._page(number, page, segments, pages)


class EmptyForPassword(FakeExtractor):
    def _password(self):
        return Extraction()


class EmptyForCorrupt(FakeExtractor):
    def _corrupt(self):
        return Extraction()


class IgnoresStop(FakeExtractor):
    def _look(self, stop):
        return False

    def _look_between_pages(self, stop):
        return False


class StopsOnlyAtStart(FakeExtractor):
    def _look_between_pages(self, stop):
        return False


class ImageNotSentToOcr(FakeExtractor):
    def extract(self, record, stop=None):
        result = super().extract(record, stop)
        if isinstance(result, Extraction) and result.pages and self.registry[record.path].kind == "image":
            return Extraction((), (PageInfo(1, PageVerdict.NO_LAYER, False),))
        return result


class OfficeTextAsLayerText(FakeExtractor):
    """Marks native Office text as a PDF text layer on page 1."""

    def extract(self, record, stop=None):
        result = super().extract(record, stop)
        if isinstance(result, Extraction) and self.registry[record.path].kind == "office":
            return Extraction(tuple(SegmentIn(1, s.order, SegmentKind.TEXT, s.text) for s in result.segments))
        return result


class Harness:
    def __init__(self, extractor_class=FakeExtractor) -> None:
        self.registry: Dict[str, DocSpec] = {}
        self.extractor = extractor_class(self.registry)
        self._count = 0

    def make(self, spec: DocSpec) -> FileRecord:
        self._count += 1
        ext = {"pdf": "pdf", "text": "txt", "office": "docx", "image": "png"}[spec.kind]
        path = f"fake:/docs/file{self._count}.{ext}"
        self.registry[path] = spec
        return FileRecord(path=path, size=1000, modified=1_700_000_000.0, kind=KINDS[spec.kind])
