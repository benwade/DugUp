"""A whole folder tree of generated files, with what the index should say about each one.

`build_tree(folder)` writes the tree and returns the list of `Expect` rows. The words are invented and
unique per file, so a search for a file's word must find that file and no other.
Everything here is generated: no private text, nothing downloaded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import helpers

from . import officewriter as ow
from .images import build_png
from .pdfwriter import PdfPage, build_pdf, corrupt_pdf


@dataclass(frozen=True)
class Expect:
    relative: str                 # path below the tree, with "/" separators
    state: str                    # the state the file must end in
    words: Tuple[str, ...] = ()   # words that must find this file
    absent: Tuple[str, ...] = ()  # words in the file that must NOT be indexed (garbled text, text behind a password)
    note: str = ""                # a piece of the reason the file's note must contain


def _write(root: Path, relative: str, data: bytes) -> None:
    path = root / Path(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_tree(folder: os.PathLike) -> List[Expect]:
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    awkward = helpers.AWKWARD_FOLDER
    rows: List[Expect] = []

    def add(relative: str, data: bytes, state: str, words=(), absent=(), note="") -> None:
        _write(root, relative, data)
        rows.append(Expect(relative, state, tuple(words), tuple(absent), note))

    # plain text, in several encodings and with awkward names
    add("notes/plain.txt", b"zorblat appears in a plain note", "done", ["zorblat"])
    add(f"notes/{helpers.CAFE} {helpers.CJK}.txt", f"quibberwock {helpers.CAFE} {helpers.CJK}".encode("utf-8"), "done",
        ["quibberwock", helpers.CAFE, helpers.CJK])
    add("notes/utf16.txt", b"\xff\xfe" + f"flimwazzle {helpers.MUELLER}".encode("utf-16-le"), "done", ["flimwazzle", helpers.MUELLER])
    add("notes/legacy.txt", f"grobnatch {helpers.CAFE}".encode("cp1252"), "done", ["grobnatch"])
    add("notes/empty.txt", b"", "done")
    add("notes/page.html", b"<html><body><p>snarfblat in a page</p><script>var hiddenword = 1</script></body></html>", "done",
        ["snarfblat"], ["hiddenword"])
    add(f"{awkward}/deep/deeper/note with spaces.md", b"# wibblefrotz\nmarkdown body", "done", ["wibblefrotz"])

    # PDFs: every kind of page
    add("pdf/born digital.pdf", build_pdf([PdfPage("born_digital", "plimsoll first page"), PdfPage("born_digital", "vexnor second page")]),
        "done", ["plimsoll", "vexnor"])
    add("pdf/scan with layer.pdf", build_pdf([PdfPage("scanner", "drazzle hidden layer words")]), "partial", ["drazzle"], note="wait for OCR")
    add("pdf/scan no layer.pdf", build_pdf([PdfPage("none", ink=True), PdfPage("none", ink=True)]), "partial", note="2 of 2 page(s) wait for OCR")
    add("pdf/garbled.pdf", build_pdf([PdfPage("garbled", "krumpet must not be indexed")]), "partial", [], ["krumpet"], "wait for OCR")
    add("pdf/mixed.pdf", build_pdf([PdfPage("born_digital", "yondril digital page"), PdfPage("none", ink=True), PdfPage("none", ink=False)]),
        "partial", ["yondril"], note="1 of 3 page(s) wait for OCR")
    add("pdf/blank.pdf", build_pdf([PdfPage("none", ink=False)]), "done")
    add("pdf/broken page.pdf", build_pdf([PdfPage("born_digital", "tharnok readable page"), PdfPage("unreadable")]), "partial",
        ["tharnok"], note="could not be read")
    add("pdf/locked.pdf", build_pdf([PdfPage("born_digital", "sekretword behind a password")], password="pw"), "failed", [], ["sekretword"],
        "password_protected")
    add("pdf/corrupt.pdf", corrupt_pdf(), "failed", note="corrupt")
    add("pdf/zero bytes.pdf", b"", "failed", note="corrupt")
    add(f"pdf/{helpers.CJK} {helpers.EMOJI}.pdf", build_pdf([PdfPage("born_digital", f"mubblewink {helpers.GREEK}")]), "done",
        ["mubblewink", helpers.GREEK])

    # Office and Open/LibreOffice
    add("office/letter.docx", ow.build_docx([f"fandrel letter body {helpers.CAFE}"], header="hortleby header"), "done", ["fandrel", "hortleby"])
    add("office/budget.xlsx", ow.build_xlsx({"Budget": [["item", "cost"], ["glimmerspoon", 1250]]}), "done", ["glimmerspoon", "1250"])
    add("office/deck.pptx", ow.build_pptx([["jaxartes title"], ["second slide"]], notes=["ospreywhistle note"]), "done",
        ["jaxartes", "ospreywhistle"])
    add("office/letter.odt", ow.build_odt(["nimbleforth body"], heading="A heading"), "done", ["nimbleforth"])
    add("office/sheet.ods", ow.build_ods({"S": [["perrywhittle", 7]]}), "done", ["perrywhittle"])
    add("office/slides.odp", ow.build_odp([["quandelby slide"]]), "done", ["quandelby"])
    add("office/old format.doc", ow.ole_file(), "skipped", note="Install LibreOffice")
    add("office/locked.docx", ow.ole_file(), "failed", note="password_protected")
    add("office/broken.xlsx", b"PK\x03\x04 not really", "failed", note="corrupt")

    # images: nothing to read without OCR
    add("pictures/photo.png", build_png(seed=1), "partial", note="1 of 1 page(s) wait for OCR")
    add(f"pictures/{helpers.CAFE}.png", build_png(seed=2), "partial", note="wait for OCR")

    # other things a folder holds
    add("other/archive.zip", b"PK\x05\x06" + bytes(18), "skipped", note="no reader")
    add("other/no extension", b"just bytes", "skipped", note="no reader")
    add("other/binary.txt", bytes(range(256)) * 4, "skipped", note="binary")

    # duplicates and a version family (same content under other names)
    same = b"duplicatedword appears in every copy"
    for name in ("family/report v1.txt", "family/report (2).txt", "family/report final.txt", "family/sub/report v1.txt"):
        add(name, same, "done", ["duplicatedword"])
    return rows
