"""Small writers for Office and Open/LibreOffice test files, using only the standard library.

They produce the parts the native readers read, with the package files a real document has. Word and
LibreOffice files written here are meant to be real documents; the PowerPoint file has slides and notes but
no slide master, so PowerPoint itself may refuse it (our reader does not need one).
"""

from __future__ import annotations

import io
import zipfile
from typing import Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006"
HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _zip(parts: Dict[str, str], stored_first: Optional[str] = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if stored_first:
            archive.writestr(zipfile.ZipInfo(stored_first), parts[stored_first].encode("utf-8"), zipfile.ZIP_STORED)
        for name, text in parts.items():
            if name != stored_first:
                archive.writestr(name, text.encode("utf-8"))
    return buffer.getvalue()


def _types(overrides: Dict[str, str]) -> str:
    rows = "".join(f'<Override PartName="{name}" ContentType="{kind}"/>' for name, kind in overrides.items())
    return (HEAD + f'<Types xmlns="{PKG}/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>' + rows + "</Types>")


def _rels(targets: Sequence[tuple]) -> str:
    rows = "".join(f'<Relationship Id="rId{i}" Type="{kind}" Target="{target}"/>' for i, (kind, target) in enumerate(targets, 1))
    return HEAD + f'<Relationships xmlns="{PKG}/relationships">{rows}</Relationships>'


def _w_paragraph(text: str) -> str:
    """One Word paragraph. A tab character becomes a tab element, a line feed a line break."""
    runs = []
    for line_index, line in enumerate(text.split("\n")):
        if line_index:
            runs.append("<w:r><w:br/></w:r>")
        for tab_index, piece in enumerate(line.split("\t")):
            if tab_index:
                runs.append("<w:r><w:tab/></w:r>")
            if piece:
                runs.append(f'<w:r><w:t xml:space="preserve">{escape(piece)}</w:t></w:r>')
    return "<w:p>" + "".join(runs) + "</w:p>"


def build_docx(paragraphs: Sequence[str], header: str = "", footnote: str = "", deleted: str = "") -> bytes:
    body = "".join(_w_paragraph(p) for p in paragraphs)
    if deleted:   # tracked deletion: this text is no longer part of the document
        body += f"<w:p><w:del><w:r><w:delText>{escape(deleted)}</w:delText></w:r></w:del></w:p>"
    parts = {
        "[Content_Types].xml": _types({
            "/word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"}),
        "_rels/.rels": _rels([(f"{R}/officeDocument", "word/document.xml")]),
        "word/document.xml": HEAD + f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
    }
    if header:
        parts["word/header1.xml"] = HEAD + f'<w:hdr xmlns:w="{W}">{_w_paragraph(header)}</w:hdr>'
    if footnote:
        parts["word/footnotes.xml"] = HEAD + f'<w:footnotes xmlns:w="{W}"><w:footnote>{_w_paragraph(footnote)}</w:footnote></w:footnotes>'
    return _zip(parts)


def _column(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def build_xlsx(sheets: Dict[str, Sequence[Sequence[object]]]) -> bytes:
    """sheets: name -> rows of cells. Text goes to the shared string table; numbers are written as numbers."""
    shared: List[str] = []
    index: Dict[str, int] = {}
    parts: Dict[str, str] = {}
    sheet_tags, rels, overrides = [], [], {
        "/xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "/xl/sharedStrings.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"}
    for number, (name, rows) in enumerate(sheets.items(), 1):
        out = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, value in enumerate(row):
                ref = f"{_column(c)}{r}"
                if value is None or value == "":
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    text = str(value)
                    if text not in index:
                        index[text] = len(shared)
                        shared.append(text)
                    cells.append(f'<c r="{ref}" t="s"><v>{index[text]}</v></c>')
            out.append(f'<row r="{r}">' + "".join(cells) + "</row>")
        parts[f"xl/worksheets/sheet{number}.xml"] = HEAD + f'<worksheet xmlns="{S}"><sheetData>{"".join(out)}</sheetData></worksheet>'
        sheet_tags.append(f'<sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="{number}" r:id="rId{number}"/>')
        rels.append((f"{R}/worksheet", f"worksheets/sheet{number}.xml"))
        overrides[f"/xl/worksheets/sheet{number}.xml"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    rels.append((f"{R}/sharedStrings", "sharedStrings.xml"))
    strings = "".join(f'<si><t xml:space="preserve">{escape(t)}</t></si>' for t in shared)
    parts.update({
        "[Content_Types].xml": _types(overrides),
        "_rels/.rels": _rels([(f"{R}/officeDocument", "xl/workbook.xml")]),
        "xl/workbook.xml": HEAD + f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>{"".join(sheet_tags)}</sheets></workbook>',
        "xl/_rels/workbook.xml.rels": _rels(rels),
        "xl/sharedStrings.xml": HEAD + f'<sst xmlns="{S}" count="{len(shared)}" uniqueCount="{len(shared)}">{strings}</sst>',
    })
    return _zip(parts)


def _a_paragraphs(lines: Sequence[str]) -> str:
    return "".join(f"<a:p><a:r><a:t>{escape(line)}</a:t></a:r></a:p>" for line in lines)


def build_pptx(slides: Sequence[Sequence[str]], notes: Sequence[str] = ()) -> bytes:
    """slides: each slide is a list of text lines. notes: speaker notes, one string per slide (may be shorter)."""
    parts: Dict[str, str] = {}
    overrides = {"/ppt/presentation.xml": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"}
    rels = []
    for number, lines in enumerate(slides, 1):
        parts[f"ppt/slides/slide{number}.xml"] = (
            HEAD + f'<p:sld xmlns:a="{A}" xmlns:p="{P}"><p:cSld><p:spTree><p:sp><p:txBody><a:bodyPr/>'
            f"{_a_paragraphs(lines)}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
        overrides[f"/ppt/slides/slide{number}.xml"] = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
        rels.append((f"{R}/slide", f"slides/slide{number}.xml"))
    for number, text in enumerate(notes, 1):
        parts[f"ppt/notesSlides/notesSlide{number}.xml"] = (
            HEAD + f'<p:notes xmlns:a="{A}" xmlns:p="{P}"><p:cSld><p:spTree><p:sp><p:txBody><a:bodyPr/>'
            f"{_a_paragraphs([text])}</p:txBody></p:sp></p:spTree></p:cSld></p:notes>")
    ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i}"/>' for i in range(1, len(slides) + 1))
    parts.update({
        "[Content_Types].xml": _types(overrides),
        "_rels/.rels": _rels([(f"{R}/officeDocument", "ppt/presentation.xml")]),
        "ppt/presentation.xml": HEAD + f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": _rels(rels),
    })
    return _zip(parts)


OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW_NS = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
MANIFEST_NS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
ODF_TYPES = {"odt": "application/vnd.oasis.opendocument.text", "ods": "application/vnd.oasis.opendocument.spreadsheet",
             "odp": "application/vnd.oasis.opendocument.presentation"}


def _odf_inline(text: str) -> str:
    """Text for one ODF paragraph: runs of spaces, tabs and line feeds use their own elements."""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\t":
            out.append("<text:tab/>")
        elif ch == "\n":
            out.append("<text:line-break/>")
        elif ch == " " and i + 1 < len(text) and text[i + 1] == " ":
            j = i
            while j < len(text) and text[j] == " ":
                j += 1
            out.append(f' <text:s text:c="{j - i - 1}"/>')
            i = j
            continue
        else:
            out.append(escape(ch))
        i += 1
    return "".join(out)


def _odf(kind: str, body: str, encrypted: bool = False) -> bytes:
    lock = ('<manifest:encryption-data manifest:checksum-type="SHA1/1K" manifest:checksum="AAAA">'
            '<manifest:algorithm manifest:algorithm-name="Blowfish CFB"/></manifest:encryption-data>') if encrypted else ""
    manifest = (HEAD + f'<manifest:manifest xmlns:manifest="{MANIFEST_NS}">'
                f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{ODF_TYPES[kind]}"/>'
                f'<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml">{lock}</manifest:file-entry>'
                "</manifest:manifest>")
    content = (HEAD + f'<office:document-content xmlns:office="{OFFICE_NS}" xmlns:text="{TEXT_NS}" '
               f'xmlns:table="{TABLE_NS}" xmlns:draw="{DRAW_NS}"><office:body>{body}</office:body></office:document-content>')
    if encrypted:
        content = "\x8f\x03 not XML: this stands in for encrypted bytes"
    return _zip({"mimetype": ODF_TYPES[kind], "META-INF/manifest.xml": manifest, "content.xml": content}, "mimetype")


def build_odt(paragraphs: Sequence[str], heading: str = "", encrypted: bool = False) -> bytes:
    body = f"<text:h>{_odf_inline(heading)}</text:h>" if heading else ""
    body += "".join(f"<text:p>{_odf_inline(p)}</text:p>" for p in paragraphs)
    return _odf("odt", f"<office:text>{body}</office:text>", encrypted)


def build_ods(sheets: Dict[str, Sequence[Sequence[object]]]) -> bytes:
    tables = []
    for name, rows in sheets.items():
        out = []
        for row in rows:
            cells = "".join(f"<table:table-cell><text:p>{_odf_inline(str(v))}</text:p></table:table-cell>" for v in row)
            out.append(f"<table:table-row>{cells}</table:table-row>")
        tables.append(f'<table:table table:name="{escape(name, {chr(34): "&quot;"})}">{"".join(out)}</table:table>')
    return _odf("ods", f"<office:spreadsheet>{''.join(tables)}</office:spreadsheet>")


def build_odp(slides: Sequence[Sequence[str]]) -> bytes:
    pages = []
    for lines in slides:
        boxes = "".join(f"<draw:frame><draw:text-box><text:p>{_odf_inline(l)}</text:p></draw:text-box></draw:frame>" for l in lines)
        pages.append(f"<draw:page>{boxes}</draw:page>")
    return _odf("odp", f"<office:presentation>{''.join(pages)}</office:presentation>")


def ole_file() -> bytes:
    """The first bytes of an old binary Office file (also what a password-protected docx/xlsx looks like)."""
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504)
