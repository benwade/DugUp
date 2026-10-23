"""Which family a file belongs to, decided by its extension (guide 5d item 4).

The table is a default, not a law: Settings can add or change extensions. Source code is left out on purpose
(DigUp keeps code in a separate index; version 1 here does not read it), and so are audio and video.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from .shapes import FileKind

DEFAULT_EXTENSIONS: Dict[str, FileKind] = {}
for _kind, _names in (
    (FileKind.PDF, "pdf"),
    (FileKind.IMAGE, "png jpg jpeg jpe tif tiff bmp gif webp heic heif"),
    (FileKind.TEXT, "txt text md markdown rst csv tsv log htm html eml"),
    (FileKind.OFFICE, "docx docm dotx xlsx xlsm xltx pptx pptm ppsx potx odt ott ods ots odp otp doc dot xls xlt ppt pps rtf"),
):
    for _name in _names.split():
        DEFAULT_EXTENSIONS[_name] = _kind


def extension(path: str) -> str:
    """The extension in lower case without the dot ("" when there is none). Either separator is understood."""
    name = path[max(path.rfind("/"), path.rfind("\\")) + 1:]
    dot = name.rfind(".")
    if dot <= 0:   # no dot, or a name such as ".gitignore"
        return ""
    return name[dot + 1:].lower()


def is_lock_file(path: str) -> bool:
    """The small file an office program keeps beside a document while it is open ("~$Report.docx",
    ".~lock.Report.odt#"). It carries the document's ending and is not a document."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.startswith("~$") or name.startswith(".~lock.")


def classify(path: str, extensions: Optional[Mapping[str, FileKind]] = None) -> FileKind:
    if is_lock_file(path):
        return FileKind.OTHER
    table = DEFAULT_EXTENSIONS if extensions is None else extensions
    return table.get(extension(path), FileKind.OTHER)
