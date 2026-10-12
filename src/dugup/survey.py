"""A look at what a folder holds and what indexing it would cost, before anything is indexed.

Pure: it takes a listing (paths, sizes, whether each file is a cloud placeholder) and touches no disk.
Nothing is opened, so nothing is downloaded from a cloud drive and nothing is read.

The answer is one row for each folder directly below the one asked about, so a person can decide folder
by folder how far to go: names only, the words in readable files, the words in pictures too (OCR), or
meaning as well. To look closer, survey one of those folders.

The sizes are estimates. They come from `Rates`, which the service fills from the person's own index when
there is enough of one to learn from, and otherwise from starting guesses. They are for choosing between
"a few MB" and "many GB", not for planning to the megabyte.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .kinds import classify, extension
from .shapes import FileKind
from .suggest import safe_folders

# Kinds a person recognises. Code, sound and video are counted so their weight is visible; DugUp does not read them yet.
DOCUMENTS, TEXT, PICTURES, CODE, MEDIA, OTHER = "documents", "text", "pictures", "code", "sound and video", "other"
GROUPS = (DOCUMENTS, TEXT, PICTURES, CODE, MEDIA, OTHER)
CODE_EXTENSIONS = frozenset(
    "py pyw ipynb js jsx ts tsx mjs cjs java kt kts c h cc cpp cxx hpp cs go rs rb php swift m mm scala sh bash zsh ps1 "
    "psm1 bat cmd sql r lua pl pm dart vue svelte css scss less json yaml yml toml ini cfg xml gradle cmake".split())
MEDIA_EXTENSIONS = frozenset(
    "mp3 wav flac m4a aac ogg oga opus wma aiff mp4 m4v mov avi mkv webm wmv mpg mpeg flv 3gp".split())
HERE = "(files directly in this folder)"
LEVELS = ("names", "words", "words and OCR", "meaning as well")


@dataclass(frozen=True)
class Rates:
    """What the index grows by, in bytes. Starting guesses unless `learned` says they came from a real index."""

    name: float = 500.0          # every file: its name and path
    read: float = 25_000.0       # a readable file: its text and the word index over it
    picture: float = 2_000.0     # a picture: the words OCR finds in it
    vector: float = 4_500.0      # a readable file or picture: its compact vectors
    learned: bool = False


@dataclass
class Row:
    folder: str                  # the folder's name, or HERE
    files: int = 0
    bytes: int = 0
    groups: Dict[str, int] = field(default_factory=lambda: {g: 0 for g in GROUPS})
    cloud_only: int = 0          # placeholders: listed, never opened
    not_worth: int = 0           # inside downloaded requirements, tool data and the like
    readable: int = 0            # documents and text that are on this machine and worth reading
    to_ocr: int = 0              # pictures that are on this machine and worth reading

    def cost(self, rates: Rates) -> Tuple[float, float, float, float]:
        """Estimated index bytes at each level, in the order of LEVELS (each includes the one before)."""
        names = (self.files - self.not_worth) * rates.name
        words = names + self.readable * rates.read
        ocr = words + self.to_ocr * rates.picture
        meaning = ocr + (self.readable + self.to_ocr) * rates.vector
        return names, words, ocr, meaning


@dataclass(frozen=True)
class Survey:
    root: str
    rows: Tuple[Row, ...]        # largest first
    total: Row
    rates: Rates


def group_of(path: str, extensions: Optional[Mapping[str, FileKind]] = None) -> str:
    kind = classify(path, extensions)
    if kind in (FileKind.PDF, FileKind.OFFICE):
        return DOCUMENTS
    if kind is FileKind.TEXT:
        return TEXT
    if kind is FileKind.IMAGE:
        return PICTURES
    ext = extension(path)
    if ext in CODE_EXTENSIONS:
        return CODE
    if ext in MEDIA_EXTENSIONS:
        return MEDIA
    return OTHER


def survey(root: str, files: Iterable[Tuple[str, int, bool]], extensions: Optional[Mapping[str, FileKind]] = None,
           rates: Optional[Rates] = None) -> Survey:
    """`files` is (path, size, is a cloud placeholder) for every file below root."""
    files = list(files)
    base = root.rstrip("/\\")
    safe = {key.casefold() for key in safe_folders(root, ((path, size) for path, size, _ in files), extensions)}
    rows: Dict[str, Row] = {}
    total = Row("everything")
    for path, size, offline in files:
        relative = path[len(base) + 1:].replace("\\", "/")
        top = relative.split("/", 1)[0] if "/" in relative else HERE
        row = rows.get(top.casefold())
        if row is None:
            row = rows[top.casefold()] = Row(top)
        group = group_of(path, extensions)
        skip = _inside(relative.casefold(), safe)
        for target in (row, total):
            target.files += 1
            target.bytes += max(0, size)
            target.groups[group] += 1
            if skip:
                target.not_worth += 1
                continue
            if offline:
                target.cloud_only += 1
                continue
            if group in (DOCUMENTS, TEXT):
                target.readable += 1
            elif group == PICTURES:
                target.to_ocr += 1
    ordered = sorted(rows.values(), key=lambda r: (-r.bytes, r.folder.casefold()))
    return Survey(root, tuple(ordered), total, rates or Rates())


def _inside(relative: str, folders) -> bool:
    """True when one of the folders above this file is in the set (looked up level by level: no scan of the set)."""
    if not folders:
        return False
    cut = relative.find("/")
    while cut != -1:
        if relative[:cut] in folders:
            return True
        cut = relative.find("/", cut + 1)
    return False


def learn_rates(index_bytes: int, files: int, read_files: int, pieces: int, enough: int = 200) -> Rates:
    """Rates from a real index: what it weighs for each file it has read. Starting guesses when it is too small to tell.
    Division is guarded: an empty index teaches nothing."""
    start = Rates()
    if read_files < enough or index_bytes <= 0:
        return start
    read = max(1_000.0, (index_bytes - files * start.name) / read_files)
    per_file = pieces / read_files if pieces > 0 else start.vector / 350.0
    return Rates(start.name, read, start.picture, max(350.0, per_file * 350.0), learned=True)
