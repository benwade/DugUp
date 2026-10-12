"""PDF text layers, with a verdict for every page (guide 5d item 8, CONTRACTS.md part 3).

Reader: pypdfium2 (permissive license; guide 5c item 8). The file is opened by this module and handed to
the library as an open file, so non-ASCII and very long Windows paths never reach the library as text.

How a page is judged, by structure and not by who produced the file:
  no text at all             -> no_layer; it goes to OCR when the page shows anything (an image, a drawing)
  text that is not readable  -> garbled; not indexed, goes to OCR
  text over a page-size image, or text that is all invisible
                             -> scanner_layer; the text is indexed and the page also goes to OCR
  anything else              -> born_digital; trusted
  ... with a sizeable picture -> born_digital still, and the page also goes to OCR, because words inside
                                a figure or a pasted screenshot are in no text layer
  the page cannot be opened  -> unreadable, with the reason

The two thresholds (how much of the page an image must cover, how much unusable text makes a layer
garbled) are settings and are guesses until they are scored against labeled files [unverified].
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..protocols import Extraction, PageInfo, PageVerdict, StopSignal
from ..settings import Settings
from ..shapes import Failure, FailureReason, FileRecord, SegmentIn, SegmentKind
from .chunking import chunk_text, unusable_fraction
from .common import STOPPED, open_file, stopped

try:
    import pypdfium2 as _pdfium
    import pypdfium2.raw as _raw
except Exception as _exc:   # not installed, or its native library failed to load
    _pdfium = None
    _raw = None
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
else:
    _IMPORT_ERROR = ""

INVISIBLE = 3   # PDF text render mode 3: neither filled nor stroked


def available() -> bool:
    return _pdfium is not None


def reader_version() -> str:
    if _pdfium is None:
        return ""
    return f"pypdfium2 {getattr(_pdfium, 'PYPDFIUM_INFO', '?')} / pdfium {getattr(_pdfium, 'PDFIUM_INFO', '?')}"


def _bounds(obj) -> Optional[Tuple[float, float, float, float]]:
    getter = getattr(obj, "get_bounds", None) or getattr(obj, "get_pos", None)   # the name changed in version 5
    try:
        return tuple(getter()) if getter else None   # type: ignore[return-value]
    except Exception:
        return None


def judge_page(text: str, has_objects: bool, image_fraction: float, visible_text: bool, settings: Settings
               ) -> Tuple[PageVerdict, bool, bool]:
    """(verdict, needs OCR, index the text). Pure, so the rule can be tested as a table."""
    if not text.strip():
        return PageVerdict.NO_LAYER, has_objects, False
    if unusable_fraction(text) >= settings.garbled_fraction:
        return PageVerdict.GARBLED, True, False
    if image_fraction >= settings.full_page_image_fraction or not visible_text:
        return PageVerdict.SCANNER_LAYER, True, True
    figure = settings.figure_fraction > 0 and image_fraction >= settings.figure_fraction
    return PageVerdict.BORN_DIGITAL, figure, True


class PdfExtractor:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()

    def extract(self, record: FileRecord, stop: Optional[StopSignal] = None) -> "Extraction | Failure":
        if stopped(stop):
            return STOPPED
        if _pdfium is None:
            return Failure(FailureReason.NOT_INSTALLED,
                           f"the PDF reader pypdfium2 is not available ({_IMPORT_ERROR}); pip install pypdfium2")
        handle = open_file(record.path)
        if isinstance(handle, Failure):
            return handle
        with handle:
            try:
                document = _pdfium.PdfDocument(handle)
            except _pdfium.PdfiumError as exc:
                message = str(exc)
                if "password" in message.lower():
                    return Failure(FailureReason.PASSWORD_PROTECTED, "the PDF needs a password")
                return Failure(FailureReason.CORRUPT, f"the PDF could not be opened ({message})")
            except Exception as exc:
                return Failure(FailureReason.CORRUPT, f"the PDF could not be opened ({type(exc).__name__}: {exc})")
            try:
                return self._read(document, stop)
            finally:
                try:
                    document.close()
                except Exception:
                    pass

    def _read(self, document, stop: Optional[StopSignal]) -> "Extraction | Failure":
        try:
            count = len(document)
        except Exception as exc:
            return Failure(FailureReason.CORRUPT, f"the PDF's page list could not be read ({type(exc).__name__})")
        limit = self.settings.max_pdf_pages
        segments: List[SegmentIn] = []
        pages: List[PageInfo] = []
        for index in range(min(count, limit)):
            if stopped(stop):
                return Failure(FailureReason.STOPPED, f"stopped before page {index + 1}")
            number = index + 1
            try:
                info, texts = self._page(document, index, number)
            except Exception as exc:   # one bad page must not stop the others being read
                info, texts = PageInfo(number, PageVerdict.UNREADABLE, False, f"{type(exc).__name__}: {exc}"), []
            pages.append(info)
            segments.extend(SegmentIn(number, order, SegmentKind.TEXT, piece) for order, piece in enumerate(texts))
        unread: Tuple[str, ...] = ()
        if count > limit:
            unread = (f"pages {limit + 1} to {count} were not read (the limit is {limit} pages a file)",)
        return Extraction(tuple(segments), tuple(pages), unread)

    def _page(self, document, index: int, number: int) -> Tuple[PageInfo, List[str]]:
        page = document[index]
        try:
            width, height = page.get_size()
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_bounded()
            finally:
                textpage.close()
            area = float(width) * float(height)
            has_objects = False
            visible_text = False
            image_fraction = 0.0
            for obj in page.get_objects(max_depth=3):
                has_objects = True
                if obj.type == _raw.FPDF_PAGEOBJ_TEXT:
                    if _raw.FPDFTextObj_GetTextRenderMode(obj.raw) != INVISIBLE:
                        visible_text = True
                elif obj.type == _raw.FPDF_PAGEOBJ_IMAGE and area > 0:
                    box = _bounds(obj)
                    if box:
                        left, bottom, right, top = box
                        covered = max(0.0, min(right, width) - max(left, 0.0)) * max(0.0, min(top, height) - max(bottom, 0.0))
                        image_fraction = max(image_fraction, covered / area)
        finally:
            page.close()
        verdict, needs_ocr, index_text = judge_page(text, has_objects, image_fraction, visible_text, self.settings)
        texts = chunk_text(text, self.settings.chunk_chars, self.settings.chunk_overlap) if index_text else []
        return PageInfo(number, verdict, needs_ocr), texts
