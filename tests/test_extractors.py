"""The real extractors on generated files: encodings, each Office format, PDF verdicts as a table, limits."""

import codecs
import os
import threading

import pytest

import helpers
from conformance.common import CountingStop
from corpus import officewriter as ow
from corpus.pdfwriter import PdfPage, build_pdf
from dugup.extractors import ExtractorSet, default_extractors
from dugup.extractors import pdf as pdf_module
from dugup.extractors.chunking import chunk_text, clean_text, unusable_fraction
from dugup.extractors.pdf import judge_page
from dugup.extractors.textfile import decode_bytes, html_to_text
from dugup.kinds import classify, extension
from dugup.protocols import Extraction, Extractor, PageVerdict
from dugup.settings import Settings
from dugup.shapes import Failure, FailureReason, FileKind, FileRecord, SegmentKind
from extractor_harness import RealHarness

needs_pdf = pytest.mark.skipif(not pdf_module.available(), reason="pypdfium2 is not installed")
MIXED = f"{helpers.CAFE} {helpers.MUELLER} {helpers.GREEK} {helpers.CYRILLIC} {helpers.CJK} {helpers.EMOJI} {helpers.LEFT_QUOTE}q{helpers.RIGHT_QUOTE} {helpers.EM_DASH}"


def texts(result):
    assert isinstance(result, Extraction), result
    return [s.text for s in result.segments]


def whole(result):
    return "\n".join(texts(result))


@pytest.fixture
def h(tmp_path):
    return RealHarness(tmp_path)


# ---- kinds -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path,kind", [
    ("C:\\Docs\\Report.PDF", FileKind.PDF), ("/a/b/photo.JpEg", FileKind.IMAGE), ("notes.txt", FileKind.TEXT),
    ("/a/sheet.xlsx", FileKind.OFFICE), ("/a/old.doc", FileKind.OFFICE), ("/a/archive.tar.gz", FileKind.OTHER),
    ("/a/no_extension", FileKind.OTHER), ("/a/.gitignore", FileKind.OTHER), ("/a.b/readme", FileKind.OTHER),
    (f"/a/{helpers.CJK}.pdf", FileKind.PDF), ("/a/trailing.", FileKind.OTHER),
])
def test_kind_comes_from_the_extension(path, kind):
    assert classify(path) is kind


def test_extension_understands_both_separators():
    assert extension("C:\\a.b\\c") == "" and extension("C:\\a.b\\c.TXT") == "txt" and extension("/x/y.tar.gz") == "gz"


def test_the_extension_table_can_be_changed():
    assert classify("/a/x.log", {"log": FileKind.OTHER}) is FileKind.OTHER
    assert classify("/a/x.nfo", {"nfo": FileKind.TEXT}) is FileKind.TEXT


# ---- chunking --------------------------------------------------------------------------------------------

def test_short_text_is_one_chunk_and_blank_text_is_none():
    assert chunk_text("  hello world \r\n", 100, 10) == ["hello world"]
    assert chunk_text(" \n\t ", 100, 10) == []


def test_long_text_is_cut_with_overlap_and_nothing_is_lost():
    words = [f"w{i:04d}" for i in range(2000)]
    chunks = chunk_text(" ".join(words), 300, 60)
    assert len(chunks) > 10 and all(len(c) <= 300 for c in chunks)
    seen = set()
    for chunk in chunks:
        seen.update(chunk.split())
    assert seen == set(words), "every word must be whole in at least one chunk, and no chunk may begin or end inside a word"
    for a, b in zip(chunks, chunks[1:]):
        assert set(a.split()) & set(b.split()), "neighbouring chunks must share some words"


def test_text_without_spaces_still_ends():
    chunks = chunk_text("x" * 5000, 100, 99)
    assert "".join(c[:1] for c in chunks) and sum(len(c) for c in chunks) >= 5000
    assert chunk_text("y" * 250, 100, 0) == ["y" * 100, "y" * 100, "y" * 50]


def test_an_overlap_larger_than_a_chunk_can_be_still_ends_and_loses_nothing():
    words = [f"w{i:03d}" for i in range(300)]
    chunks = chunk_text(" ".join(words), 100, 99)      # a cut at a space leaves less than the overlap
    assert len(chunks) < 2000
    seen = set()
    for chunk in chunks:
        seen.update(chunk.split())
    assert seen == set(words), "every word whole, and no pieces of words"


def test_chunking_always_moves_forward():
    words = [f"word{i:06d}" for i in range(200)]            # ten letters and a space: cuts land exactly one overlap apart
    chunks = chunk_text(" ".join(words), 100, 99)
    assert len(chunks) <= 200 and {w for c in chunks for w in c.split()} == set(words)


def test_non_ascii_text_survives_chunking():
    text = " ".join([MIXED] * 200)
    chunks = chunk_text(text, 200, 40)
    assert all(helpers.CJK in c or helpers.CAFE in c for c in chunks)
    assert helpers.EMOJI in "".join(chunks)


def test_control_characters_and_lone_surrogates_are_removed():
    assert clean_text("a\x00b\x02c\x03d\x7fe\ud800f\tg\r\nh") == "a b c d e f\tg\nh"


def test_unusable_fraction_guards_empty_text():
    assert unusable_fraction("") == 0.0 and unusable_fraction("   ") == 0.0
    assert unusable_fraction("abc") == 0.0 and unusable_fraction("\ufffd\ue000ab") == 0.5


# ---- plain text ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("encoding,bom", [
    ("utf-8", b""), ("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
    ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE),
])
def test_text_files_decode_in_every_unicode_encoding(h, encoding, bom):
    record = h.write("mixed.txt", bom + MIXED.encode(encoding))
    assert texts(h.extractor.extract(record)) == [MIXED]


def test_legacy_windows_text_is_read_as_cp1252(h):
    record = h.write("legacy.txt", f"na{chr(0xEF)}ve {helpers.CAFE} {helpers.LEFT_QUOTE}quoted{helpers.RIGHT_QUOTE}".encode("cp1252"))
    assert texts(h.extractor.extract(record)) == [f"na{chr(0xEF)}ve {helpers.CAFE} {helpers.LEFT_QUOTE}quoted{helpers.RIGHT_QUOTE}"]


def test_binary_data_named_txt_is_refused_with_a_reason(h):
    result = h.extractor.extract(h.write("data.txt", bytes(range(256)) * 10))
    assert isinstance(result, Failure) and result.reason is FailureReason.UNSUPPORTED


def test_a_text_file_over_the_limit_is_read_in_part_and_says_so(tmp_path):
    h = RealHarness(tmp_path, Settings(max_text_bytes=994))
    data = ((helpers.CAFE + " word ") * 400).encode("utf-8")   # 11 bytes a time; byte 994 is the middle of an accented letter
    assert data[993:995] == helpers.E_ACUTE.encode("utf-8"), "the test must cut inside a character"
    result = h.extractor.extract(h.write("big.txt", data))
    assert isinstance(result, Extraction) and result.segments
    assert len(result.unread) == 1 and "994" in result.unread[0]
    assert "\ufffd" not in whole(result), "a character split by the cut must not become a replacement character"
    assert helpers.CAFE in whole(result) and chr(0xC3) not in whole(result), "the cut must not make the reader guess another encoding"


def test_an_empty_text_file_is_an_empty_result_not_a_failure(h):
    result = h.extractor.extract(h.write("empty.txt", b""))
    assert isinstance(result, Extraction) and result.segments == () and result.unread == ()


def test_html_tags_scripts_and_styles_are_dropped(h):
    page = (f"<html><head><title>Title here</title><style>p {{color: red}}</style><script>var hidden = 1;</script></head>"
            f"<body><p>First &amp; {helpers.CAFE}</p><div>Second<br>line</div></body></html>")
    text = whole(h.extractor.extract(h.write("page.html", page.encode("utf-8"))))
    assert f"First & {helpers.CAFE}" in text and "Second" in text and "Title here" in text
    assert "hidden" not in text and "color" not in text and "<" not in text
    assert html_to_text("<p>unclosed <b>tags") == "unclosed tags"


def test_a_file_that_vanished_is_unreachable_not_empty(h):
    record = FileRecord(path=str(h.folder / "never-existed.txt"), size=1, modified=1.0, kind=FileKind.TEXT)
    result = h.extractor.extract(record)
    assert isinstance(result, Failure) and result.reason is FailureReason.UNREACHABLE


def test_decoding_a_cut_buffer_never_raises():
    data = (helpers.CJK * 50).encode("utf-8")
    for cut in range(1, 12):
        text, name = decode_bytes(data[:cut], complete=False)
        assert name == "utf-8" and "\ufffd" not in text


def test_a_file_in_a_very_long_path_is_read(tmp_path):
    import os
    import shutil
    from dugup.dirscan import long_path
    from dugup.extractors import default_extractors as build
    folder = os.path.join(str(tmp_path), *(["deep folder " + "x" * 40] * 6))   # well over 260 characters in all
    path = os.path.join(folder, f"{helpers.CAFE} note.txt")
    assert len(path) > 260
    try:
        try:
            os.makedirs(long_path(folder))
            with open(long_path(path), "wb") as handle:
                handle.write(b"longpathword")
        except OSError as exc:   # a file system that refuses such paths: nothing to test here
            pytest.skip(f"this file system refused a long path: {exc}")
        record = FileRecord(path=path, size=12, modified=1.0, kind=FileKind.TEXT)   # the plain path, as a scan reports it
        assert texts(build().extract(record)) == ["longpathword"]
    finally:
        shutil.rmtree(long_path(os.path.join(str(tmp_path), "deep folder " + "x" * 40)), ignore_errors=True)


# ---- Office ----------------------------------------------------------------------------------------------

def test_docx_text_headers_footnotes_tabs_and_breaks(h):
    data = ow.build_docx([f"First {MIXED}", "cell one\tcell two", "line one\nline two"], header="Header words",
                         footnote="Footnote words", deleted="removedword")
    text = whole(h.extractor.extract(h.write("doc.docx", data)))
    for expected in (f"First {MIXED}", "cell one\tcell two", "line one\nline two", "Header words", "Footnote words"):
        assert expected in text
    assert "removedword" not in text, "text deleted with change tracking is not part of the document"


def test_xlsx_sheets_strings_and_numbers(h):
    data = ow.build_xlsx({"Budget 2026": [["Item", "Cost"], [f"Rent {helpers.CAFE}", 1250.5], ["Total", 2500]],
                          f"{helpers.CJK} sheet": [["second sheet word", 42]]})
    result = h.extractor.extract(h.write("book.xlsx", data))
    text = whole(result)
    assert all(s.kind is SegmentKind.NATIVE and s.page == 0 for s in result.segments)
    for expected in ("Budget 2026", f"{helpers.CJK} sheet", f"Rent {helpers.CAFE}\t1250.5", "Total\t2500", "second sheet word\t42"):
        assert expected in text


def test_a_wide_and_long_spreadsheet_is_chunked_not_lost(tmp_path):
    h = RealHarness(tmp_path, Settings(chunk_chars=500, chunk_overlap=50))
    rows = [[f"r{r}c{c}" for c in range(60)] for r in range(80)]
    result = h.extractor.extract(h.write("wide.xlsx", ow.build_xlsx({"Wide": rows})))
    assert len(result.segments) > 10 and [s.order for s in result.segments] == list(range(len(result.segments)))
    found = set(whole(result).split())
    assert {"r0c0", "r79c59", "r40c30"} <= found


def test_pptx_slides_in_order_and_speaker_notes(h):
    data = ow.build_pptx([[f"Slide {i} title", f"point {i}"] for i in range(1, 13)], notes=["note for slide one"])
    text = whole(h.extractor.extract(h.write("deck.pptx", data)))
    assert text.index("Slide 2 title") < text.index("Slide 10 title") < text.index("Slide 12 title"), "slide 10 comes after slide 2"
    assert "note for slide one" in text


def test_open_document_text_sheet_and_slides(h):
    odt = whole(h.extractor.extract(h.write("doc.odt", ow.build_odt([f"Body {MIXED}", "two  spaces\tand tab\nand break"], heading="A heading"))))
    assert "A heading" in odt and f"Body {MIXED}" in odt and "two  spaces\tand tab\nand break" in odt
    ods = whole(h.extractor.extract(h.write("book.ods", ow.build_ods({"Sheet": [["alpha", 1], ["beta", 2]]}))))
    assert "alpha" in ods and "beta" in ods
    odp = whole(h.extractor.extract(h.write("deck.odp", ow.build_odp([["slide one text"], ["slide two text"]]))))
    assert "slide one text" in odp and "slide two text" in odp


@pytest.mark.parametrize("name,data,reason", [
    ("locked.docx", ow.ole_file(), FailureReason.PASSWORD_PROTECTED),
    ("locked.odt", ow.build_odt(["secret"], encrypted=True), FailureReason.PASSWORD_PROTECTED),
    ("broken.xlsx", b"PK\x03\x04 nope", FailureReason.CORRUPT),
    ("empty.pptx", b"", FailureReason.CORRUPT),
    ("old.doc", ow.ole_file(), FailureReason.UNSUPPORTED),
    ("old.xls", ow.ole_file(), FailureReason.UNSUPPORTED),
    ("note.rtf", b"{\\rtf1 hello}", FailureReason.UNSUPPORTED),
])
def test_office_files_that_cannot_be_read_say_why(h, name, data, reason, monkeypatch):
    from dugup.extractors import office
    monkeypatch.setattr(office, "find_converter", lambda: None)      # as on a computer without LibreOffice
    result = h.extractor.extract(h.write(name, data))
    assert isinstance(result, Failure) and result.reason is reason and result.detail.strip()


def test_a_docx_without_its_main_part_is_corrupt_not_empty(h):
    import io, zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/header1.xml", "<x/>")
    result = h.extractor.extract(h.write("hollow.docx", buffer.getvalue()))
    assert isinstance(result, Failure) and result.reason is FailureReason.CORRUPT


def _repack(data: bytes, replace: dict) -> bytes:
    import io, zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            dst.writestr(name, replace.get(name, src.read(name)))
        for name, body in replace.items():
            if name not in src.namelist():
                dst.writestr(name, body)
    return out.getvalue()


def test_a_damaged_side_part_is_reported_unread_and_the_rest_is_kept(h):
    data = _repack(ow.build_docx(["main body words"], header="Header words"), {"word/header1.xml": b"<broken"})
    result = h.extractor.extract(h.write("part.docx", data))
    assert "main body words" in whole(result)
    assert len(result.unread) == 1 and "word/header1.xml" in result.unread[0]


def test_a_part_that_declares_entities_is_refused(h):
    bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>')
    result = h.extractor.extract(h.write("bomb.docx", _repack(ow.build_docx(["x"]), {"word/document.xml": bomb})))
    assert isinstance(result, Failure) and result.reason is FailureReason.CORRUPT and "DTD" in result.detail


def test_a_part_over_the_size_limit_is_reported_unread(tmp_path):
    h = RealHarness(tmp_path, Settings(max_member_bytes=2000))
    data = ow.build_xlsx({"Small": [["tinyword"]], "Huge": [[r * 1000 + c for c in range(30)] for r in range(60)]})
    result = h.extractor.extract(h.write("mixed.xlsx", data))
    assert isinstance(result, Extraction) and "tinyword" in whole(result)
    assert any("sheet2.xml" in note and "limit" in note for note in result.unread)


def test_a_missing_string_table_makes_the_sheets_unread_not_numbers(h):
    data = _repack(ow.build_xlsx({"S": [["wordone", "wordtwo"]]}), {"xl/sharedStrings.xml": b"<broken"})
    result = h.extractor.extract(h.write("nostrings.xlsx", data))
    assert isinstance(result, Extraction) and len(result.unread) == 2
    assert "0\t1" not in whole(result), "string index numbers must not be indexed as if they were the text"


def test_office_reading_can_be_stopped(h):
    record = h.write("deck.pptx", ow.build_pptx([[f"slide {i}"] for i in range(30)]))
    result = h.extractor.extract(record, CountingStop(after=5))
    assert isinstance(result, Failure) and result.reason is FailureReason.STOPPED


# ---- PDF -------------------------------------------------------------------------------------------------

S = Settings()


@pytest.mark.parametrize("text,has_objects,image,visible,expected", [
    ("", False, 0.0, False, (PageVerdict.NO_LAYER, False, False)),                 # a blank page
    ("  \n ", True, 1.0, False, (PageVerdict.NO_LAYER, True, False)),              # a scan with no layer
    ("", True, 0.0, False, (PageVerdict.NO_LAYER, True, False)),                   # a drawing with no text
    ("plain words", True, 0.0, True, (PageVerdict.BORN_DIGITAL, False, True)),
    ("a caption", True, 0.3, True, (PageVerdict.BORN_DIGITAL, True, True)),        # text beside a figure: trusted, and read too
    ("a caption", True, 0.1, True, (PageVerdict.BORN_DIGITAL, True, True)),        # exactly at the figure setting
    ("a letterhead", True, 0.05, True, (PageVerdict.BORN_DIGITAL, False, True)),   # a logo is not a figure
    ("ocr words", True, 1.0, False, (PageVerdict.SCANNER_LAYER, True, True)),      # hidden text over a page image
    ("ocr words", True, 1.0, True, (PageVerdict.SCANNER_LAYER, True, True)),       # visible text over a page image
    ("ocr words", True, 0.0, False, (PageVerdict.SCANNER_LAYER, True, True)),      # all text hidden, image not found
    ("\ue000\ue001\ue002 \ufffd\ufffd", True, 0.0, True, (PageVerdict.GARBLED, True, False)),
    ("\ue000\ue001\ue002 \ufffd\ufffd", True, 1.0, False, (PageVerdict.GARBLED, True, False)),
    ("mostly fine words here \ufffd", True, 0.0, True, (PageVerdict.BORN_DIGITAL, False, True)),
])
def test_the_page_rule_as_a_table(text, has_objects, image, visible, expected):
    assert judge_page(text, has_objects, image, visible, S) == expected


def test_figures_can_be_turned_off_and_the_size_is_a_setting():
    assert judge_page("a caption", True, 0.3, True, Settings(figure_fraction=0.0)) == (PageVerdict.BORN_DIGITAL, False, True)
    assert judge_page("a caption", True, 0.3, True, Settings(figure_fraction=0.5)) == (PageVerdict.BORN_DIGITAL, False, True)
    assert judge_page("a letterhead", True, 0.05, True, Settings(figure_fraction=0.05))[1] is True


def test_a_typed_page_with_a_figure_is_trusted_and_also_sent_to_ocr(tmp_path):
    from corpus.pdfwriter import PdfPage, build_pdf
    from extractor_harness import RealHarness

    h = RealHarness(tmp_path)
    record = h.write("report.pdf", build_pdf([PdfPage("born_digital", "plain typed page"),
                                              PdfPage("figure", "typed words beside a chart"),
                                              PdfPage("logo", "typed words under a small logo")]))
    result = h.extractor.extract(record)
    assert [(p.verdict, p.needs_ocr) for p in result.pages] == [
        (PageVerdict.BORN_DIGITAL, False), (PageVerdict.BORN_DIGITAL, True), (PageVerdict.BORN_DIGITAL, False)]
    assert [s.text for s in result.segments if s.page == 2] == ["typed words beside a chart"], "its typed text is indexed as before"


def test_the_page_rule_thresholds_are_settings():
    strict = Settings(full_page_image_fraction=0.2, garbled_fraction=0.01)
    assert judge_page("a caption", True, 0.3, True, strict)[0] is PageVerdict.SCANNER_LAYER
    assert judge_page("mostly fine words here \ufffd", True, 0.0, True, strict)[0] is PageVerdict.GARBLED


@needs_pdf
def test_pdf_text_keeps_non_ascii_and_line_breaks(h):
    result = h.extractor.extract(h.write("mixed.pdf", build_pdf([PdfPage("born_digital", f"line one {MIXED}\nline two")])))
    assert texts(result) == [f"line one {MIXED}\nline two"]


@needs_pdf
def test_hidden_text_without_a_page_image_is_still_a_scanner_layer(h):
    result = h.extractor.extract(h.write("hidden.pdf", build_pdf([PdfPage("hidden", "invisible ocr words")])))
    assert [(p.verdict, p.needs_ocr) for p in result.pages] == [(PageVerdict.SCANNER_LAYER, True)]
    assert texts(result) == ["invisible ocr words"]


@needs_pdf
def test_a_long_pdf_page_is_chunked_within_its_page(tmp_path):
    h = RealHarness(tmp_path, Settings(chunk_chars=120, chunk_overlap=20))
    long_text = "\n".join(f"sentence number {i} on this page" for i in range(30))
    result = h.extractor.extract(h.write("long.pdf", build_pdf([PdfPage("born_digital", long_text), PdfPage("born_digital", "short")])))
    first = [s for s in result.segments if s.page == 1]
    assert len(first) > 3 and [s.order for s in first] == list(range(len(first)))
    assert [(s.page, s.order, s.text) for s in result.segments if s.page == 2] == [(2, 0, "short")]


@needs_pdf
def test_pages_over_the_limit_are_reported_unread(tmp_path):
    h = RealHarness(tmp_path, Settings(max_pdf_pages=3))
    result = h.extractor.extract(h.write("many.pdf", build_pdf([PdfPage("born_digital", f"page {i}") for i in range(1, 8)])))
    assert len(result.pages) == 3 and len(result.unread) == 1 and "4 to 7" in result.unread[0]


@needs_pdf
def test_an_empty_pdf_file_is_corrupt_not_empty(h):
    result = h.extractor.extract(h.write("zero.pdf", b""))
    assert isinstance(result, Failure) and result.reason is FailureReason.CORRUPT


def test_a_missing_pdf_reader_is_stated(h, monkeypatch):
    monkeypatch.setattr(pdf_module, "_pdfium", None)
    monkeypatch.setattr(pdf_module, "_IMPORT_ERROR", "ModuleNotFoundError: no pypdfium2")
    result = h.extractor.extract(h.write("any.pdf", build_pdf([PdfPage("born_digital", "x")])))
    assert isinstance(result, Failure) and result.reason is FailureReason.NOT_INSTALLED and "pip install" in result.detail


# ---- the set ---------------------------------------------------------------------------------------------

def test_the_set_is_an_extractor_and_families_can_be_swapped(h):
    extractors = default_extractors()
    assert isinstance(extractors, Extractor) and isinstance(ExtractorSet(), Extractor)

    class Shouting:
        def extract(self, record, stop=None):
            return Extraction()

    extractors.register(FileKind.TEXT, Shouting())
    assert extractors.extract(h.write("a.txt", b"words")) == Extraction()


def test_a_kind_with_no_reader_is_unsupported_with_a_reason(h):
    result = h.extractor.extract(h.write("blob.bin", b"\x00\x01"))
    assert isinstance(result, Failure) and result.reason is FailureReason.UNSUPPORTED


def test_every_family_honours_a_stop_set_before_it_starts(h):
    stop = threading.Event()
    stop.set()
    for name, data in (("a.txt", b"x"), ("a.png", b"x"), ("a.docx", ow.build_docx(["x"])), ("a.pdf", build_pdf([PdfPage("born_digital", "x")]))):
        result = h.extractor.extract(h.write(name, data), stop)
        assert isinstance(result, Failure) and result.reason is FailureReason.STOPPED, name


def test_an_old_office_file_says_how_to_read_it_and_is_read_through_libreoffice_when_it_is_there(h, monkeypatch):
    from dugup.extractors import office
    monkeypatch.setattr(office, "find_converter", lambda: None)
    record = h.write("old report.doc", ow.ole_file())
    result = h.extractor.extract(record)
    assert result.reason is FailureReason.UNSUPPORTED and "Install LibreOffice" in result.detail

    asked = []

    def convert(path, ext, soffice, folder, seconds):        # LibreOffice, as far as DugUp can tell
        asked.append((ext, soffice, seconds > 0))
        made = os.path.join(folder, "old report.docx")
        with open(made, "wb") as out:
            out.write(ow.build_docx(["written long ago in Word 97"]))
        return made

    monkeypatch.setattr(office, "find_converter", lambda: "soffice")
    monkeypatch.setattr(office, "convert", convert)
    text = whole(h.extractor.extract(record))
    assert "written long ago in Word 97" in text and asked == [("doc", "soffice", True)]
    monkeypatch.setattr(office, "convert", lambda *a: Failure(FailureReason.TIMED_OUT, "took too long"))
    assert h.extractor.extract(record).reason is FailureReason.TIMED_OUT


@pytest.mark.skipif(not __import__("shutil").which("soffice"), reason="LibreOffice is not installed here")
def test_a_real_rtf_file_is_read_through_a_real_libreoffice(h):
    text = whole(h.extractor.extract(h.write("letter.rtf", b"{\\rtf1\\ansi Dear reader, the harbour is calm.\\par}")))
    assert "the harbour is calm" in text


def test_a_saved_email_gives_its_people_subject_message_and_attachment_names(h):
    from email.message import EmailMessage
    message = EmailMessage()
    message["Subject"] = "Quarterly figures"
    message["From"] = "Ana <ana@example.com>"
    message["To"] = "team@example.com"
    message["Date"] = "Tue, 13 Oct 2026 09:30:00 +0000"
    message.set_content("The harbour project is on budget.")
    message.add_alternative("<p>The <b>harbour</b> project is on budget.</p>", subtype="html")
    message.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="figures Q3.pdf")
    record = h.write("figures.eml", message.as_bytes())
    assert record.kind is FileKind.TEXT
    text = whole(h.extractor.extract(record))
    assert "Subject: Quarterly figures" in text and "From: Ana <ana@example.com>" in text
    assert "The harbour project is on budget." in text and "<b>" not in text
    assert "Attachments: figures Q3.pdf" in text and "PDF-1.4" not in text, "attachment names, not their contents"
    html_only = EmailMessage()
    html_only["Subject"] = "Only HTML"
    html_only.set_content("<html><body><p>Meet at <i>noon</i></p></body></html>", subtype="html")
    assert "Meet at noon" in whole(h.extractor.extract(h.write("html.eml", html_only.as_bytes())))
