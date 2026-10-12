"""Plain text files (and HTML with its tags removed).

Encoding is the pitfall here, so nothing relies on a default: a byte-order mark decides when there is one,
then UTF-8 is tried strictly, and only then Windows-1252 (the usual legacy encoding on Windows), with
characters that cannot be decoded shown as the replacement character rather than dropped.
"""

from __future__ import annotations

import codecs
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from ..kinds import extension
from ..protocols import Extraction, StopSignal
from ..settings import Settings
from ..shapes import Failure, FailureReason, FileRecord, SegmentIn, SegmentKind
from .chunking import chunk_text
from .common import STOPPED, open_file, stopped

_BOMS: Tuple[Tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),   # before UTF-16: they share a prefix
    (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def decode_bytes(data: bytes, complete: bool = True) -> "Tuple[str, str] | Failure":
    """(text, encoding used). `complete` False means the data was cut off, so a split last character is fine."""
    for bom, name in _BOMS:
        if data.startswith(bom):
            return codecs.getincrementaldecoder(name)("replace").decode(data[len(bom):], final=complete), name
    if b"\x00" in data[:8192]:
        return Failure(FailureReason.UNSUPPORTED, "looks like binary data, not text")
    try:
        return codecs.getincrementaldecoder("utf-8")("strict").decode(data, final=complete), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace"), "cp1252"


class _Text(HTMLParser):
    """The visible text of an HTML page: tags dropped, script and style skipped, blocks kept on their own lines."""

    SKIP = {"script", "style", "template", "noscript"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:   # a page too broken for the parser: keep what was read so far
        pass
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line)


class TextFileExtractor:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()

    def extract(self, record: FileRecord, stop: Optional[StopSignal] = None) -> "Extraction | Failure":
        if stopped(stop):
            return STOPPED
        handle = open_file(record.path)
        if isinstance(handle, Failure):
            return handle
        limit = self.settings.max_text_bytes
        try:
            with handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            return Failure(FailureReason.OTHER, f"the file could not be read ({type(exc).__name__}: {exc.strerror or exc})")
        cut = len(data) > limit
        decoded = decode_bytes(data[:limit], complete=not cut)
        if isinstance(decoded, Failure):
            return decoded
        text = decoded[0]
        if extension(record.path) in ("htm", "html"):
            text = html_to_text(text)
        segments = []
        for order, piece in enumerate(chunk_text(text, self.settings.chunk_chars, self.settings.chunk_overlap)):
            if stopped(stop):
                return STOPPED
            segments.append(SegmentIn(0, order, SegmentKind.TEXT, piece))
        unread = (f"only the first {limit:,} bytes were read (the file is larger)",) if cut else ()
        return Extraction(tuple(segments), (), unread)
