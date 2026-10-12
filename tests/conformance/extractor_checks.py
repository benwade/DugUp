"""Conformance checks for Extractor (CONTRACTS.md part 3 and guide 5d item 8).

Each check takes a harness `h` with:
  h.extractor                       the Extractor under test
  h.make(spec) -> FileRecord        a file described by DocSpec / PageSpec (below):
                                    kind pdf|text|office|image; each PDF page has a layer
                                    born_digital|scanner|garbled|none|unreadable, its text, and whether it shows ink
A real harness builds real small files with the same descriptions (generated data only).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Tuple

import helpers
from dugup.protocols import Extraction, PageVerdict
from dugup.shapes import FailureReason, SegmentKind

from .common import CountingStop, require, require_failure, require_result


@dataclass(frozen=True)
class PageSpec:
    layer: str            # born_digital | scanner | garbled | none | unreadable
    text: str = ""        # the layer's text (born_digital, scanner, garbled)
    ink: bool = True      # whether the page shows anything (matters when there is no layer)


@dataclass(frozen=True)
class DocSpec:
    kind: str                         # pdf | text | office | image
    pages: Tuple[PageSpec, ...] = ()
    text: str = ""
    password: bool = False
    corrupt: bool = False


ALLOWED_KINDS = {SegmentKind.TEXT, SegmentKind.NATIVE}   # an extractor never produces name, OCR or image segments


def _extract(h, spec, stop=None):
    result = require_result(h.extractor.extract(h.make(spec), stop), "extract")
    require(isinstance(result, Extraction), "extract: did not return an Extraction")
    _basic(result)
    return result


def _basic(result):
    seen = set()
    for seg in result.segments:
        require(seg.kind in ALLOWED_KINDS, f"extract: an extractor may not produce {seg.kind.value} segments")
        require(seg.text.strip(), "extract: an empty segment was produced")
        require(seg.page >= 0, "extract: negative page")
        require((seg.page, seg.order) not in seen, "extract: two segments share a page and order")
        seen.add((seg.page, seg.order))
    numbers = [p.page for p in result.pages]
    require(numbers == list(range(1, len(numbers) + 1)), "extract: pages must be numbered 1..N once each, in order")


def _text_pages(result, page):
    return [s.text for s in result.segments if s.page == page and s.kind is SegmentKind.TEXT]


def _page(result, number):
    return result.pages[number - 1]


def check_born_digital_pdf_is_trusted(h):
    spec = DocSpec("pdf", tuple(PageSpec("born_digital", f"alpha beta page {i}") for i in range(1, 4)))
    r = _extract(h, spec)
    require(len(r.pages) == 3, "extract: every page must be accounted for")
    for i in (1, 2, 3):
        require(_page(r, i).verdict is PageVerdict.BORN_DIGITAL, f"page {i}: wrong verdict")
        require(not _page(r, i).needs_ocr, f"page {i}: a born-digital page must not be sent to OCR")
        require(_text_pages(r, i) == [f"alpha beta page {i}"], f"page {i}: its layer text must be one text segment on that page")
    require(r.pages_needing_ocr == (), "extract: no page should need OCR")


def check_scanner_layer_is_indexed_and_read(h):
    r = _extract(h, DocSpec("pdf", (PageSpec("scanner", "noisy scanner words"),)))
    require(_page(r, 1).verdict is PageVerdict.SCANNER_LAYER, "page 1: wrong verdict")
    require(_page(r, 1).needs_ocr, "page 1: a scanner layer must also be sent to OCR")
    require(_text_pages(r, 1) == ["noisy scanner words"], "page 1: a scanner layer's text is still indexed")


def check_garbled_layer_is_not_indexed(h):
    r = _extract(h, DocSpec("pdf", (PageSpec("garbled", "xq9 zzv #%& ###"),)))
    require(len(r.pages) == 1, "extract: a garbled page must still be listed, not dropped")
    require(_page(r, 1).verdict is PageVerdict.GARBLED, "page 1: wrong verdict")
    require(_page(r, 1).needs_ocr, "page 1: a garbled page must be sent to OCR")
    require(_text_pages(r, 1) == [], "page 1: garbled text must not be indexed")


def check_no_layer_with_ink_goes_to_ocr(h):
    r = _extract(h, DocSpec("pdf", (PageSpec("none", ink=True),)))
    require(_page(r, 1).verdict is PageVerdict.NO_LAYER and _page(r, 1).needs_ocr, "page 1: a picture-only page must go to OCR")
    require(r.segments == (), "extract: a page with no layer has no text segment")


def check_blank_page_is_not_sent_to_ocr(h):
    r = _extract(h, DocSpec("pdf", (PageSpec("none", ink=False),)))
    require(_page(r, 1).verdict is PageVerdict.NO_LAYER, "page 1: wrong verdict")
    require(not _page(r, 1).needs_ocr, "page 1: a blank page must not be sent to OCR")


def check_unreadable_page_is_stated(h):
    r = _extract(h, DocSpec("pdf", (PageSpec("born_digital", "first page"), PageSpec("unreadable"),
                                    PageSpec("born_digital", "third page"))))
    require(len(r.pages) == 3, "extract: an unreadable page must be listed, not dropped")
    require(_page(r, 2).verdict is PageVerdict.UNREADABLE, "page 2: wrong verdict")
    require(_page(r, 2).unread_reason.strip(), "page 2: an unreadable page must say why")
    require(_text_pages(r, 3) == ["third page"], "page 3: one bad page must not stop the others being read")


def check_mixed_document_accounts_for_every_page(h):
    layers = ["born_digital", "scanner", "garbled", "none", "born_digital", "none"]
    inks = [True, True, True, True, True, False]
    spec = DocSpec("pdf", tuple(PageSpec(l, f"words on page {i}", ink) for i, (l, ink) in enumerate(zip(layers, inks), 1)))
    r = _extract(h, spec)
    require(len(r.pages) == 6, "extract: every page must be accounted for exactly once")
    require(r.pages_needing_ocr == (2, 3, 4), "extract: the wrong pages were sent to OCR")
    require(sorted({s.page for s in r.segments}) == [1, 2, 5], "extract: text segments are on the wrong pages")


def check_image_goes_to_ocr(h):
    r = _extract(h, DocSpec("image"))
    require(r.pages_needing_ocr == (1,), "extract: an image must be marked as needing OCR")
    require(r.segments == (), "extract: an image has no text of its own")


def check_text_file(h):
    words = f"plain {helpers.CAFE} text {helpers.CJK}"
    r = _extract(h, DocSpec("text", text=words))
    require([s.text for s in r.segments] == [words] and r.segments[0].kind is SegmentKind.TEXT, "extract: text file content lost")
    require(all(s.page == 0 for s in r.segments), "extract: text files are not page-bound (page 0)")
    require(r.pages == (), "extract: a text file has no PDF pages")


def check_empty_text_file_is_an_empty_result(h):
    r = _extract(h, DocSpec("text", text="   \n"))
    require(r.segments == (), "extract: an empty file has no segments")


def check_office_native_text(h):
    r = _extract(h, DocSpec("office", text="quarterly budget memo"))
    require([s.text for s in r.segments] == ["quarterly budget memo"], "extract: Office text lost")
    require(all(s.kind is SegmentKind.NATIVE and s.page == 0 for s in r.segments), "extract: native Office text is kind native on page 0")


def check_password_is_stated(h):
    value = h.extractor.extract(h.make(DocSpec("pdf", (PageSpec("born_digital", "secret"),), password=True)))
    require_failure(value, FailureReason.PASSWORD_PROTECTED, "extract of a password-protected file")


def check_corrupt_is_stated(h):
    value = h.extractor.extract(h.make(DocSpec("pdf", (PageSpec("born_digital", "x"),), corrupt=True)))
    require_failure(value, FailureReason.CORRUPT, "extract of a damaged file")


def check_stop_before_start(h):
    stop = threading.Event()
    stop.set()
    value = h.extractor.extract(h.make(DocSpec("pdf", (PageSpec("born_digital", "a"),))), stop)
    require_failure(value, FailureReason.STOPPED, "extract with stop already set")


def check_stop_between_pages(h):
    spec = DocSpec("pdf", tuple(PageSpec("born_digital", f"page {i}") for i in range(1, 41)))
    stop = CountingStop(after=5)
    value = h.extractor.extract(h.make(spec), stop)
    require_failure(value, FailureReason.STOPPED, "extract stopped part way (the extractor must look at the stop request between pages)")
    again = _extract(h, spec)
    require(len(again.pages) == 40, "extract: after a stopped run the next run must be complete")


CHECKS = {
    "born_digital_pdf_is_trusted": check_born_digital_pdf_is_trusted,
    "scanner_layer_is_indexed_and_read": check_scanner_layer_is_indexed_and_read,
    "garbled_layer_is_not_indexed": check_garbled_layer_is_not_indexed,
    "no_layer_with_ink_goes_to_ocr": check_no_layer_with_ink_goes_to_ocr,
    "blank_page_is_not_sent_to_ocr": check_blank_page_is_not_sent_to_ocr,
    "unreadable_page_is_stated": check_unreadable_page_is_stated,
    "mixed_document_accounts_for_every_page": check_mixed_document_accounts_for_every_page,
    "image_goes_to_ocr": check_image_goes_to_ocr,
    "text_file": check_text_file,
    "empty_text_file_is_an_empty_result": check_empty_text_file_is_an_empty_result,
    "office_native_text": check_office_native_text,
    "password_is_stated": check_password_is_stated,
    "corrupt_is_stated": check_corrupt_is_stated,
    "stop_before_start": check_stop_before_start,
    "stop_between_pages": check_stop_between_pages,
}
