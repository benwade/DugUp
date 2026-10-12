"""Shared data shapes (CONTRACTS.md, "Shared words").

Plain data only. Nothing here touches the disk or any tool.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Tuple

BOX_SCALE = 1000  # boxes are stored on a 0..1000 scale on both axes


class FileState(str, Enum):
    PENDING = "pending"
    DONE = "done"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"
    MISSING = "missing"          # gone at the last scan (kept, not purged)
    UNAVAILABLE = "unavailable"  # its folder is not reachable right now


class FileKind(str, Enum):
    PDF = "pdf"
    IMAGE = "image"
    TEXT = "text"
    OFFICE = "office"
    OTHER = "other"


class SegmentKind(str, Enum):
    NAME = "name"      # the file name itself, so names are searchable
    TEXT = "text"      # a PDF or plain-text layer
    OCR = "ocr"        # read from pixels
    NATIVE = "native"  # text pulled straight out of an Office file
    IMAGE = "image"    # an image segment (meaning search, no words)


class BoxError(ValueError):
    """A box could not be turned into a valid 0..1000 box (the message says why)."""


@dataclass(frozen=True)
class Box:
    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self) -> None:
        for name in ("x0", "y0", "x1", "y1"):
            value = getattr(self, name)
            if not isinstance(value, int) or not 0 <= value <= BOX_SCALE:
                raise BoxError(f"{name}={value!r} is outside 0..{BOX_SCALE}")
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise BoxError("box corners are in the wrong order")


def _finite(*values: float) -> None:
    for value in values:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise BoxError(f"coordinate {value!r} is not a finite number")


def to_unit_box(x0: float, y0: float, x1: float, y1: float, width: float, height: float) -> Box:
    """Convert a pixel box on a width x height page to the 0..1000 scale.

    Corners given in the wrong order are swapped. A box that sticks out of the
    page is clamped to it. A box wholly outside the page, or a page with no
    size, is an error with a stated reason (never a silent empty box).
    """
    _finite(x0, y0, x1, y1, width, height)
    if width <= 0 or height <= 0:
        raise BoxError("page size must be positive")
    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    if right < 0 or bottom < 0 or left > width or top > height:
        raise BoxError("box lies outside the page")
    left, right = max(left, 0.0), min(right, float(width))
    top, bottom = max(top, 0.0), min(bottom, float(height))

    def scale(value: float, size: float) -> int:
        return int(round(value / size * BOX_SCALE))

    return Box(scale(left, width), scale(top, height), scale(right, width), scale(bottom, height))


def box_from_polygon(points: Iterable[Sequence[float]], width: float, height: float) -> Box:
    """The bounding box of a polygon (a rotated line from an OCR engine)."""
    pts = [tuple(p) for p in points]
    if not pts:
        raise BoxError("polygon has no points")
    for p in pts:
        if len(p) != 2:
            raise BoxError("polygon points must be (x, y) pairs")
        _finite(*p)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return to_unit_box(min(xs), min(ys), max(xs), max(ys), width, height)


@dataclass(frozen=True)
class Evidence:
    """Where a result came from: file, page, character span, optional region box."""

    file_id: int
    page: int
    start: int
    end: int
    box: Optional[Box] = None
    segment_id: Optional[int] = None   # the store's handle for the segment; valid until the file is read again

    def __post_init__(self) -> None:
        if self.page < 0:
            raise ValueError("page must be 0 or more (0 means not page-bound)")
        if self.start < 0 or self.end < self.start:
            raise ValueError("character span must satisfy 0 <= start <= end")


@dataclass
class FileRecord:
    path: str
    size: int
    modified: float
    kind: FileKind = FileKind.OTHER
    file_id: Optional[str] = None  # platform file id (inode / NTFS index), as text
    state: FileState = FileState.PENDING
    error: str = ""
    policy: str = ""
    row_id: Optional[int] = None   # filled in by the store
    missing_since: Optional[float] = None   # when a scan first found it gone (kept, flagged, purged later)


@dataclass(frozen=True)
class SegmentIn:
    """A chunk of text to store for a file."""

    page: int
    order: int
    kind: SegmentKind
    text: str


Pair = Tuple[int, int]


# ---- search results --------------------------------------------------------------------------------------

WHERE_ANYWHERE, WHERE_TEXT, WHERE_NAME, WHERE_OCR = "anywhere", "text", "name", "ocr"
WHERE_CHOICES = (WHERE_ANYWHERE, WHERE_TEXT, WHERE_NAME, WHERE_OCR)
MAX_SORT_KEYS = 3


@dataclass(frozen=True)
class SearchFilters:
    """Narrowing a search. Every part is optional; an empty one narrows nothing."""

    folder: Optional[str] = None            # only files in this folder or below it (whole path parts)
    kinds: Tuple[str, ...] = ()             # FileKind values; empty means every kind
    where: str = WHERE_ANYWHERE             # where the words must be: anywhere, in the text (OCR text included),
                                            # in the file name, or in text read by OCR
    modified_after: Optional[float] = None  # at or after this time
    modified_before: Optional[float] = None  # before this time
    min_size: Optional[int] = None          # bytes, inclusive
    max_size: Optional[int] = None          # bytes, inclusive
    case_insensitive: Optional[bool] = None  # how `folder` is compared; None means the platform's rule

    def __post_init__(self) -> None:
        if self.where not in WHERE_CHOICES:
            raise ValueError(f"where must be one of {', '.join(WHERE_CHOICES)}")
        known = {k.value for k in FileKind}
        for kind in self.kinds:
            if kind not in known:
                raise ValueError(f"{kind!r} is not a kind of file ({', '.join(sorted(known))})")


def sort_keys(sort, descending: bool, allowed) -> List[Tuple[str, bool]]:
    """Normalise a sort request: one key with a direction, or up to three (key, descending) pairs.
    A key given twice counts once, where it first stands."""
    pairs = [(sort, bool(descending))] if isinstance(sort, str) else [(k, bool(d)) for k, d in sort]
    out: List[Tuple[str, bool]] = []
    for key, down in pairs:
        if key not in allowed:
            raise ValueError(f"cannot sort by {key!r}; choose one of {sorted(allowed)}")
        if key not in [k for k, _ in out]:
            out.append((key, down))
    if not out:
        raise ValueError("at least one sort key is needed")
    if len(out) > MAX_SORT_KEYS:
        raise ValueError(f"at most {MAX_SORT_KEYS} sort keys")
    return out


@dataclass
class SearchRow:
    file_id: int
    path: str
    name: str
    folder: str
    size: int
    modified: float
    kind: str
    state: str
    matches: int
    rank: float
    evidence: Tuple[Evidence, ...] = ()        # where the words were found, best first
    missing_since: Optional[float] = None      # set when the file was gone at the last scan
    unavailable: bool = False                  # set by the Service when the file's folder cannot be reached now


@dataclass
class SearchPage:
    rows: List[SearchRow]
    total: int       # all matching files, not just this page
    offset: int
    limit: int

    @property
    def truncated(self) -> bool:
        return self.offset + len(self.rows) < self.total


@dataclass(frozen=True)
class Passage:
    """The text around one piece of Evidence. `start` and `end` mark the matched words inside `text`."""

    text: str
    start: int
    end: int
    page: int
    kind: SegmentKind


# ---- roots (the folders the user chose) ------------------------------------------------------------------

POLICY_OPEN = "open"              # names and text (the household default)
POLICY_NAMES_ONLY = "names_only"  # file names only; no text is read or kept
POLICIES = (POLICY_OPEN, POLICY_NAMES_ONLY)


@dataclass(frozen=True)
class Root:
    path: str
    policy: str = POLICY_OPEN
    include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()
    reachable: bool = True                 # as of the last scan
    last_scan: Optional[float] = None      # when a scan last finished (reachable or not)
    warnings: Tuple[str, ...] = ()         # warning codes from the last scan (root_empty, mass_loss)


# ---- the common promise: a result or a stated reason ----------------------------------------------------

class FailureReason(str, Enum):
    """Why a part could not produce a result (CONTRACTS.md, "The common promise")."""

    NOT_INSTALLED = "not_installed"
    TIMED_OUT = "timed_out"
    PASSWORD_PROTECTED = "password_protected"
    CORRUPT = "corrupt"
    CRASHED = "crashed"
    UNREACHABLE = "unreachable"   # a folder or drive that cannot be reached right now
    UNSUPPORTED = "unsupported"   # a language, format or option the part does not handle
    STOPPED = "stopped"           # a stop request arrived before the work finished
    BUSY = "busy"                 # a resource such as the GPU is in use by something else
    OUT_OF_MEMORY = "out_of_memory"  # a worker was ended for using too much memory
    OTHER = "other"


@dataclass(frozen=True)
class Failure:
    """A stated reason instead of a result. A part never returns nothing without one of these."""

    reason: FailureReason
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, FailureReason):
            raise TypeError("reason must be a FailureReason")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("a failure must say why (detail may not be empty)")


def failed(value: object) -> bool:
    """True when a part's answer is a Failure rather than a result."""
    return isinstance(value, Failure)
