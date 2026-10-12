"""The Extractor harness for the real extractors: it writes real small files into a temporary folder
from the same descriptions the fake uses, so the shared checks run unchanged."""

from __future__ import annotations

import os
from pathlib import Path

import helpers
from conformance.extractor_checks import DocSpec
from corpus.images import build_png
from corpus.officewriter import build_docx, ole_file
from corpus.pdfwriter import PdfPage, build_pdf, corrupt_pdf
from dugup.extractors import default_extractors
from dugup.kinds import classify
from dugup.settings import Settings
from dugup.shapes import FileRecord


class RealHarness:
    def __init__(self, folder: Path, settings: Settings = None) -> None:
        # an awkward folder name on purpose: spaces, an accent, CJK and a dash
        self.folder = Path(folder) / helpers.AWKWARD_FOLDER
        self.folder.mkdir(parents=True, exist_ok=True)
        self.extractor = default_extractors(settings or Settings())
        self._count = 0

    def write(self, name: str, data: bytes) -> FileRecord:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        info = os.stat(path)
        return FileRecord(path=str(path), size=info.st_size, modified=info.st_mtime, kind=classify(str(path)))

    def make(self, spec: DocSpec) -> FileRecord:
        self._count += 1
        stem = f"{helpers.CAFE} file {self._count}"
        if spec.kind == "pdf":
            if spec.corrupt:
                return self.write(stem + ".pdf", corrupt_pdf())
            pages = [PdfPage(p.layer, p.text, p.ink) for p in spec.pages]
            return self.write(stem + ".pdf", build_pdf(pages, password="open sesame" if spec.password else None))
        if spec.kind == "text":
            return self.write(stem + ".txt", spec.text.encode("utf-8"))
        if spec.kind == "office":
            if spec.password:
                return self.write(stem + ".docx", ole_file())
            if spec.corrupt:
                return self.write(stem + ".docx", b"PK\x03\x04 this is not a zip file at all")
            return self.write(stem + ".docx", build_docx([spec.text]))
        if spec.kind == "image":
            return self.write(stem + ".png", build_png(seed=self._count))
        raise ValueError(f"unknown kind {spec.kind!r}")
