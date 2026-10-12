"""Native text readers for Office and Open/LibreOffice files (guide 5d item 4, the last link of the chain).

They need nothing installed: these formats are zip files of XML, read here with the standard library.
They give text only (no page images), so segments are kind `native` on page 0.

Old binary formats (.doc, .xls, .ppt) and .rtf cannot be read this way. They are answered with
`unsupported` and a reason that names the fix (a converter), never with an empty result.

Safety: a part is read up to a size limit and is refused when it declares a DTD or entities, so a
crafted file cannot make the reader expand itself into gigabytes.
"""

from __future__ import annotations

import re
import zipfile
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple
from xml.etree import ElementTree

from ..kinds import extension
from ..protocols import Extraction, StopSignal
from ..settings import Settings
from ..shapes import Failure, FailureReason, FileRecord, SegmentIn, SegmentKind
from .chunking import chunk_text
from .common import STOPPED, open_file, stopped

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # old binary Office files, and encrypted new ones
WORD = ("docx", "docm", "dotx")
EXCEL = ("xlsx", "xlsm", "xltx")
POWERPOINT = ("pptx", "pptm", "ppsx", "potx")
OPEN_DOCUMENT = ("odt", "ott", "ods", "ots", "odp", "otp")
LEGACY = ("doc", "dot", "xls", "xlt", "ppt", "pps", "rtf")


class _PartError(Exception):
    """One part of the file could not be read (the message says why)."""


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _number(name: str) -> int:
    found = re.findall(r"(\d+)", name)
    return int(found[-1]) if found else 0


class _Zip:
    """A zip file opened for reading, with a size limit per part."""

    def __init__(self, archive: zipfile.ZipFile, limit: int) -> None:
        self.archive = archive
        self.limit = limit
        self.names = archive.namelist()

    def matching(self, pattern: str) -> List[str]:
        rx = re.compile(pattern)
        return sorted((n for n in self.names if rx.fullmatch(n)), key=lambda n: (_number(n), n))

    def root(self, name: str) -> ElementTree.Element:
        try:
            info = self.archive.getinfo(name)
        except KeyError:
            raise _PartError("is not in the file") from None
        if info.file_size > self.limit:
            raise _PartError(f"is larger than the limit of {self.limit:,} bytes")
        try:
            with self.archive.open(info) as handle:
                data = handle.read(self.limit + 1)
        except RuntimeError as exc:   # zipfile raises this for an encrypted member
            raise _PartError(f"is encrypted ({exc})") from None
        except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
            raise _PartError(f"is damaged ({type(exc).__name__})") from None
        if len(data) > self.limit:
            raise _PartError(f"is larger than the limit of {self.limit:,} bytes")
        head = data[:4096]
        if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
            raise _PartError("declares a DTD, which Office files never do; refused")
        try:
            return ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            raise _PartError(f"is not valid XML ({exc})") from None


# ---- Word and PowerPoint: paragraphs of runs ---------------------------------------------------------------

def _runs(element: ElementTree.Element, out: List[str]) -> None:
    """Text of one paragraph. Only `t` elements carry text; tabs and breaks become white space."""
    for child in element:
        name = _local(child.tag)
        if name == "t":
            out.append(child.text or "")
        elif name == "tab":
            out.append("\t")
        elif name in ("br", "cr"):
            out.append("\n")
        elif name not in ("p", "delText", "instrText"):
            _runs(child, out)


def _paragraphs(root: ElementTree.Element) -> Iterator[str]:
    for element in root.iter():
        if _local(element.tag) == "p":
            out: List[str] = []
            _runs(element, out)
            text = "".join(out).strip()
            if text:
                yield text


# ---- Excel -----------------------------------------------------------------------------------------------

def _shared_strings(root: ElementTree.Element) -> List[str]:
    strings = []
    for item in root:
        if _local(item.tag) != "si":
            continue
        out: List[str] = []
        _string_item(item, out)
        strings.append("".join(out))
    return strings


def _string_item(element: ElementTree.Element, out: List[str]) -> None:
    for child in element:
        name = _local(child.tag)
        if name == "t":
            out.append(child.text or "")
        elif name != "rPh":   # phonetic guides repeat the text in another script
            _string_item(child, out)


def _sheet_rows(root: ElementTree.Element, shared: List[str]) -> Iterator[str]:
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        cells = []
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            value = ""
            kind = cell.get("t", "n")
            for part in cell:
                name = _local(part.tag)
                if name == "v" and part.text is not None:
                    if kind == "s":
                        try:
                            value = shared[int(part.text)]
                        except (ValueError, IndexError):
                            value = ""
                    else:
                        value = part.text
                elif name == "is":
                    out: List[str] = []
                    _string_item(part, out)
                    value = "".join(out)
            if value.strip():
                cells.append(value.strip())
        if cells:
            yield "\t".join(cells)


def _sheet_names(root: ElementTree.Element) -> List[str]:
    return [e.get("name", "") for e in root.iter() if _local(e.tag) == "sheet" and e.get("name")]


# ---- Open/LibreOffice ------------------------------------------------------------------------------------

def _odf_text(element: ElementTree.Element, out: List[str]) -> None:
    if element.text:
        out.append(element.text)
    for child in element:
        name = _local(child.tag)
        if name == "s":
            try:
                out.append(" " * max(1, int(child.get(_attr(child, "c"), "1"))))
            except ValueError:
                out.append(" ")
        elif name == "tab":
            out.append("\t")
        elif name == "line-break":
            out.append("\n")
        elif name in ("p", "h"):
            pass   # a paragraph inside a paragraph (a frame, a note) is yielded on its own
        else:
            _odf_text(child, out)
        if child.tail:
            out.append(child.tail)


def _attr(element: ElementTree.Element, local: str) -> str:
    for key in element.attrib:
        if _local(key) == local:
            return key
    return local


def _odf_paragraphs(root: ElementTree.Element) -> Iterator[str]:
    for element in root.iter():
        if _local(element.tag) in ("p", "h"):
            out: List[str] = []
            _odf_text(element, out)
            text = "".join(out).strip()
            if text:
                yield text


# ---- the readers: each returns (blocks of text, parts that were not read) ---------------------------------

def _collect(z: _Zip, names: Iterable[str], reader: Callable[[ElementTree.Element], Iterable[str]],
             blocks: List[str], unread: List[str], stop: Optional[StopSignal]) -> bool:
    """Read each part into `blocks`. A part that fails is listed in `unread`. False when a stop was requested."""
    for name in names:
        if stopped(stop):
            return False
        try:
            blocks.extend(reader(z.root(name)))
        except _PartError as exc:
            unread.append(f"{name} {exc}")
    return True


def _read_word(z: _Zip, stop) -> "Tuple[List[str], List[str]] | Failure":
    main = z.matching(r"word/document\d*\.xml")
    if not main:
        return Failure(FailureReason.CORRUPT, "the document has no word/document.xml part")
    blocks: List[str] = []
    unread: List[str] = []
    try:
        blocks.extend(_paragraphs(z.root(main[0])))
    except _PartError as exc:
        return Failure(FailureReason.CORRUPT, f"word/document.xml {exc}")
    extra = z.matching(r"word/(header|footer)\d*\.xml") + z.matching(r"word/(footnotes|endnotes|comments)\.xml")
    if not _collect(z, extra, _paragraphs, blocks, unread, stop):
        return STOPPED
    return blocks, unread


def _read_excel(z: _Zip, stop) -> "Tuple[List[str], List[str]] | Failure":
    sheets = z.matching(r"xl/worksheets/sheet\d+\.xml")
    if "xl/workbook.xml" not in z.names:
        return Failure(FailureReason.CORRUPT, "the workbook has no xl/workbook.xml part")
    blocks: List[str] = []
    unread: List[str] = []
    try:
        blocks.extend(_sheet_names(z.root("xl/workbook.xml")))
    except _PartError as exc:
        return Failure(FailureReason.CORRUPT, f"xl/workbook.xml {exc}")
    shared: List[str] = []
    if "xl/sharedStrings.xml" in z.names:
        try:
            shared = _shared_strings(z.root("xl/sharedStrings.xml"))
        except _PartError as exc:
            # without the string table the sheets would be rows of meaningless index numbers
            return blocks, [f"xl/sharedStrings.xml {exc}"] + [f"{n} needs the string table" for n in sheets]
    if not _collect(z, sheets, lambda root: _sheet_rows(root, shared), blocks, unread, stop):
        return STOPPED
    return blocks, unread


def _read_powerpoint(z: _Zip, stop) -> "Tuple[List[str], List[str]] | Failure":
    slides = z.matching(r"ppt/slides/slide\d+\.xml")
    if "ppt/presentation.xml" not in z.names:
        return Failure(FailureReason.CORRUPT, "the presentation has no ppt/presentation.xml part")
    blocks: List[str] = []
    unread: List[str] = []
    if not _collect(z, slides + z.matching(r"ppt/notesSlides/notesSlide\d+\.xml"), _paragraphs, blocks, unread, stop):
        return STOPPED
    return blocks, unread


def _read_open_document(z: _Zip, stop) -> "Tuple[List[str], List[str]] | Failure":
    if "META-INF/manifest.xml" in z.names:
        try:
            manifest = z.root("META-INF/manifest.xml")
            if any(_local(e.tag) == "encryption-data" for e in manifest.iter()):
                return Failure(FailureReason.PASSWORD_PROTECTED, "the document is encrypted with a password")
        except _PartError:
            pass
    try:
        blocks = list(_odf_paragraphs(z.root("content.xml")))
    except _PartError as exc:
        return Failure(FailureReason.CORRUPT, f"content.xml {exc}")
    return blocks, []


_READERS: Dict[str, Callable] = {}
for _names, _reader in ((WORD, _read_word), (EXCEL, _read_excel), (POWERPOINT, _read_powerpoint),
                        (OPEN_DOCUMENT, _read_open_document)):
    for _name in _names:
        _READERS[_name] = _reader


class OfficeExtractor:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()

    def extract(self, record: FileRecord, stop: Optional[StopSignal] = None) -> "Extraction | Failure":
        if stopped(stop):
            return STOPPED
        ext = extension(record.path)
        if ext in LEGACY:
            return Failure(FailureReason.UNSUPPORTED,
                           f"a .{ext} file needs a converter (LibreOffice or Microsoft Office); the built-in readers "
                           "only read the newer formats")
        reader = _READERS.get(ext)
        if reader is None:
            return Failure(FailureReason.UNSUPPORTED, f"no built-in reader for .{ext} files")
        handle = open_file(record.path)
        if isinstance(handle, Failure):
            return handle
        with handle:
            try:
                magic = handle.read(8)
                handle.seek(0)
            except OSError as exc:
                return Failure(FailureReason.OTHER, f"the file could not be read ({type(exc).__name__})")
            if magic == OLE_MAGIC:
                return Failure(FailureReason.PASSWORD_PROTECTED,
                               "the file is encrypted with a password (or is an old-format file with a new extension)")
            try:
                archive = zipfile.ZipFile(handle)
            except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
                return Failure(FailureReason.CORRUPT, f"not a readable Office file ({type(exc).__name__}: {exc})")
            with archive:
                result = reader(_Zip(archive, self.settings.max_member_bytes), stop)
        if isinstance(result, Failure):
            return result
        blocks, unread = result
        segments = []
        pieces = chunk_text("\n".join(blocks), self.settings.chunk_chars, self.settings.chunk_overlap)
        for order, piece in enumerate(pieces):
            if stopped(stop):
                return STOPPED
            segments.append(SegmentIn(0, order, SegmentKind.NATIVE, piece))
        return Extraction(tuple(segments), (), tuple(unread))
