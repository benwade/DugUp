"""The ten contracts as Protocol classes (CONTRACTS.md, draft 2).

Nothing here does any work. These are the shapes a part must have so that it can be swapped
(a different OCR engine, a different embedder) without touching the rest.

The common promise: every method that can fail returns either its result or a `Failure`
with a stated reason (see shapes.Failure). Methods do not raise for expected trouble such as a
missing tool, a timeout, a password or a damaged file. They raise only for programming errors
(a wrong argument type, a closed store).

Long operations take an optional `stop` (a StopSignal; threading.Event satisfies it). They look at it
between small units of work and return Failure(STOPPED) when it is set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable)

from .shapes import (Box, Evidence, Failure, FileKind, FileRecord, FileState, Passage, Root, SearchFilters, SearchPage,
                     SegmentIn)


@runtime_checkable
class StopSignal(Protocol):
    """What a long operation watches. `threading.Event` already fits."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: Optional[float] = None) -> bool: ...


# ---- 1. FileSource -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FileEntry:
    path: str                      # as the source reports it; the key for comparing listings
    size: int
    modified: float
    file_id: Optional[str] = None  # platform file id ("device:index") as text, when known
    offline: bool = False          # a cloud placeholder: listed without being downloaded; reading it would download it


@dataclass(frozen=True)
class ScanRules:
    """Include/exclude settings, written as shell-style patterns on the path below the root.

    Slash is the separator in patterns on every system, and case is ignored.
    A pattern matches the whole relative path, the file name, or any folder above the file.
    A pattern that starts with a slash ("/Trash") matches only that path below the root, so it does not
    also skip every other folder that happens to share the name.
    With no include patterns, everything not excluded is in.
    """

    include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()


# Warning codes a ChangeSet may carry. The thresholds behind them are guesses (see filesource.py).
WARN_ROOT_EMPTY = "root_empty"   # the folder is reachable but now lists nothing, and it used to
WARN_MASS_LOSS = "mass_loss"     # at least half of a sizeable earlier listing is gone at once


@dataclass(frozen=True)
class Skipped:
    """A folder or file the source could not read this time. Whatever was known below it is kept, not removed."""

    path: str
    reason: str


@dataclass(frozen=True)
class Listing:
    entries: Tuple[FileEntry, ...] = ()
    skipped: Tuple[Skipped, ...] = ()


@dataclass(frozen=True)
class ChangeSet:
    added: Tuple[FileEntry, ...] = ()
    changed: Tuple[FileEntry, ...] = ()
    removed: Tuple[str, ...] = ()                       # old paths; never a path below a skipped folder
    moved: Tuple[Tuple[str, FileEntry], ...] = ()       # (old path, entry at the new path)
    warnings: Tuple[str, ...] = ()
    skipped: Tuple[Skipped, ...] = ()


@runtime_checkable
class FileSource(Protocol):
    """Finds files and notices changes. Never writes to the folders it reads."""

    def reachable(self, root: str) -> bool: ...

    def scan(self, root: str, rules: ScanRules, stop: Optional[StopSignal] = None
             ) -> "Listing | Failure":
        """Full listing of files (not folders) below root. An unreachable root is Failure(UNREACHABLE).
        A folder that cannot be read is named in `skipped`; it is never silently left out."""

    def changes(self, root: str, previous: Mapping[str, FileEntry], rules: ScanRules,
                stop: Optional[StopSignal] = None) -> "ChangeSet | Failure":
        """What differs from `previous` (keyed by path). An unreachable root is never 'everything removed'."""


# ---- 2. Converter --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Converted:
    pdf: Path
    converter: str
    version: str


@runtime_checkable
class Converter(Protocol):
    def convert(self, source: Path, time_limit: float, stop: Optional[StopSignal] = None
                ) -> "Converted | Failure": ...


# ---- 3. Extractor --------------------------------------------------------------------------------------

class PageVerdict(str, Enum):
    BORN_DIGITAL = "born_digital"      # a text layer that can be trusted
    SCANNER_LAYER = "scanner_layer"    # a text layer over a scanned image
    GARBLED = "garbled"                # a layer whose text is not usable
    NO_LAYER = "no_layer"              # nothing to read (a picture, or a blank page)
    UNREADABLE = "unreadable"          # the page could not be examined at all


@dataclass(frozen=True)
class PageInfo:
    page: int                  # 1-based
    verdict: PageVerdict
    needs_ocr: bool
    unread_reason: str = ""    # required for UNREADABLE

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("pages are numbered from 1")
        if self.verdict is PageVerdict.UNREADABLE and not self.unread_reason.strip():
            raise ValueError("an unreadable page must say why")


@dataclass(frozen=True)
class Extraction:
    segments: Tuple[SegmentIn, ...] = ()
    pages: Tuple[PageInfo, ...] = ()   # PDFs and images; empty for plain text and native Office
    unread: Tuple[str, ...] = ()       # parts that were not read, each with its reason (never silently dropped)

    @property
    def pages_needing_ocr(self) -> Tuple[int, ...]:
        return tuple(p.page for p in self.pages if p.needs_ocr)


@runtime_checkable
class Extractor(Protocol):
    def extract(self, record: FileRecord, stop: Optional[StopSignal] = None) -> "Extraction | Failure": ...


# ---- 4. OcrBackend -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OcrLine:
    text: str
    box: Box                         # whole-page coordinates, 0..1000, even when a region was asked for
    confidence: Optional[float] = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1 when given")


@dataclass(frozen=True)
class OcrResult:
    lines: Tuple[OcrLine, ...]
    engine: str
    version: str


@runtime_checkable
class OcrBackend(Protocol):
    def read(self, image: Path, language: str = "en", time_limit: float = 60.0,
             region: Optional[Box] = None, stop: Optional[StopSignal] = None
             ) -> "OcrResult | Failure":
        """Read one picture. A blank picture is a successful result with no lines."""


# ---- 5. Embedder ---------------------------------------------------------------------------------------

Vector = Sequence[float]


@dataclass(frozen=True)
class EmbedderInfo:
    model: str
    revision: str
    dimension: int
    route: str = ""                      # for example the llama.cpp build or the Python route
    picture_tokens: Optional[int] = None


@runtime_checkable
class Embedder(Protocol):
    def info(self) -> "EmbedderInfo | Failure": ...

    def embed_texts(self, texts: Sequence[str], role: str = "document",
                    stop: Optional[StopSignal] = None) -> "Sequence[Vector | Failure] | Failure":
        """One answer per text, in order. role is 'query' or 'document'."""

    def embed_images(self, images: Sequence[Path], budget: int = 280,
                     stop: Optional[StopSignal] = None) -> "Sequence[Vector | Failure] | Failure": ...


# ---- 6. Store ------------------------------------------------------------------------------------------
# Built so far: files, segments, pages, roots, word search with Evidence, purge, fingerprint, transactions.
# Regions, vectors, meaning search and the word list come in later steps.

@runtime_checkable
class Store(Protocol):
    def transaction(self): ...                       # context manager; nested uses join the outer one

    def set_fingerprint(self, fingerprint: Mapping[str, object]) -> None: ...

    def get_fingerprint(self) -> Optional[Mapping[str, object]]: ...

    def changed_fingerprint_keys(self, current: Mapping[str, object]) -> Sequence[str]: ...

    def upsert_file(self, record: FileRecord) -> int: ...

    def get_file(self, path: str) -> Optional[FileRecord]: ...

    def list_files(self, prefix: Optional[str] = None, state: Optional[FileState] = None,
                   limit: Optional[int] = None, after_row: int = 0, missing: Optional[bool] = None,
                   case_insensitive: Optional[bool] = None) -> Sequence[FileRecord]:
        """Files in row order. `prefix` is a folder (whole path parts). `missing` None means both kinds."""

    def move_file(self, old_path: str, new_path: str) -> int:
        """The same file at a new path: its text, pages and state are kept. Returns the row id."""

    def set_state(self, row_id: int, state, error: str = "") -> None: ...

    def set_content(self, row_id: int, mark: Optional[str]) -> None:
        """The mark of the file's exact content (contentmark.py), or None when it is not known."""

    def content_of(self, row_id: int) -> Optional[str]: ...

    def copies_of(self, row_ids: Sequence[int]) -> Mapping[int, Sequence[str]]:
        """For each file, the paths of the other files with the same content (not missing), shortest first."""

    def unmarked(self, prefix: Optional[str] = None, limit: Optional[int] = None, after_row: int = 0,
                 case_insensitive: Optional[bool] = None) -> Sequence[FileRecord]:
        """Files that were read and have no content mark yet, in row order."""

    def copy_counts(self) -> Tuple[int, int, int]:
        """(files with a mark, sets of files with the same content, files that are an extra copy)."""

    def read_marks(self) -> Sequence[Tuple[str, str]]:
        """(content mark, kind) for the files that have been read and were there at the last scan."""

    def read_twin(self, mark: str, not_row: int, kind: FileKind) -> Optional[FileRecord]:
        """A file of this kind with this content that has been read, other than `not_row`; a fully read one first."""

    def copy_reading(self, from_row: int, to_row: int) -> None:
        """Give one file everything read from another with the same content (text, pages, OCR)."""

    def copy_page(self, from_row: int, to_row: int, page: int) -> None:
        """Give one file what OCR made of a page of another with the same content."""

    def remove_files(self, row_ids: Sequence[int]) -> int:
        """Take these files out of the index (text and page records too). Returns how many were there."""

    def set_missing(self, row_id: int, since: Optional[float]) -> None:
        """Flag a file as gone at the last scan (or clear the flag with None). Its text is kept."""

    def purge_missing(self, prefix: str, before: float, case_insensitive: Optional[bool] = None) -> int:
        """Remove files below `prefix` that have been missing since before the given time."""

    def replace_segments(self, row_id: int, segments: Sequence[SegmentIn]) -> None: ...

    def set_pages(self, row_id: int, pages: Sequence[PageInfo]) -> None: ...

    def get_pages(self, row_id: int) -> Sequence[PageInfo]: ...

    def ocr_queue(self, prefix: Optional[str] = None, case_insensitive: Optional[bool] = None
                  ) -> Sequence[OcrTask]:
        """Pages that wait for OCR, of files that are not missing, in file then page order."""

    def set_page_shape(self, row_id: int, page: int, width: int, height: int, camera: bool) -> None: ...

    def save_ocr(self, row_id: int, page: int, result: "OcrResult") -> None:
        """Store what OCR read on a page (searchable, with a box per line) and mark the page read.
        Safe to repeat. A result with no lines marks the page read and stores no text."""

    def fail_ocr(self, row_id: int, page: int, reason: str) -> None:
        """Mark a page as one OCR could not read, with the reason. It leaves the queue until retried."""

    def defer_ocr(self, row_id: int, page: int, reason: str) -> None:
        """Keep a page in the queue but mark it for a second, more patient try (see OcrTask.retry)."""

    def replace_text(self, row_id: int, segments: Sequence[SegmentIn], pages: Sequence[PageInfo]) -> None:
        """A file was read again. Its text and page records become these, but what OCR already read is kept
        for every page that still needs OCR (the OCR text, the page marked read, a picture's shape).
        OCR text of pages that no longer need it, or no longer exist, is dropped."""

    def ocr_engines(self) -> Mapping[str, int]:
        """How many pages each OCR engine (name and version) has read."""

    def requeue_ocr_not_by(self, engine: str, prefix: Optional[str] = None,
                           case_insensitive: Optional[bool] = None) -> Sequence[int]:
        """Put pages read by any other engine or version back in the queue. Their old text stays until
        they are read again. Returns the row ids of their files."""

    def retry_ocr(self, prefix: Optional[str] = None, case_insensitive: Optional[bool] = None) -> Sequence[int]:
        """Put pages OCR failed on back in the queue. Returns the row ids of their files."""

    def ocr_status(self, row_id: int) -> Sequence[Tuple[int, str, str]]:
        """(page, OCR_WAITING | OCR_DONE | OCR_FAILED, reason) for each page that needed OCR, in page order."""

    def file_count(self) -> int: ...

    def segment_count(self, include_names: bool = False) -> int: ...

    def state_counts(self, prefix: Optional[str] = None, case_insensitive: Optional[bool] = None
                     ) -> Mapping[str, int]:
        """How many files are in each state. Missing files are counted under "missing", not their old state."""

    def search_words(self, query: str, limit: int = 100, offset: int = 0,
                     sort="rank", descending: bool = False, filters: Optional[SearchFilters] = None) -> SearchPage:
        """`sort` is one key (with `descending`), or up to three (key, descending) pairs applied in order.
        Filtering and sorting happen before the page is cut, so the total and the order are honest."""

    def file_evidence(self, file_id: int, query: str, filters: Optional[SearchFilters] = None,
                      limit: int = 2000) -> Sequence[Evidence]:
        """Every place in one file where the words were found, in reading order (by page, then position;
        a match in the file name last). A search row carries only the best few; this is the rest."""

    def get_passage(self, evidence: Evidence, context: int = 80) -> Optional[Passage]:
        """The text around a piece of Evidence, or None when that segment no longer exists."""

    def list_roots(self) -> Sequence[Root]: ...

    def set_root(self, root: Root) -> None:
        """Add a root or change its policy and rules. Its scan status is kept."""

    def note_scan(self, path: str, reachable: bool, when: float, warnings: Sequence[str] = ()) -> None: ...

    def remove_root(self, path: str) -> bool:
        """Forget a root (not its files; purge_folder does that). False when it was not a root."""

    def purge_folder(self, prefix: str, case_insensitive: Optional[bool] = None, thorough: bool = False) -> int: ...

    def reclaimable_fraction(self) -> float:
        """The share of the index's size that compacting would give back, as far as the store can tell (0 to 1)."""

    def compact(self) -> None:
        """Give back the disk space that removed and rewritten text left behind. Changes nothing that is stored."""

    def close(self) -> None: ...


@dataclass(frozen=True)
class OcrTask:
    """One page that waits for OCR. For a picture the page is 1 and the shape is its size in pixels."""

    row_id: int
    path: str
    kind: FileKind
    page: int
    width: Optional[int] = None    # None: not looked at yet; 0: could not be measured
    height: Optional[int] = None
    camera: bool = False           # the picture says which camera took it (a photo, not a scan or a screenshot)
    verdict: str = ""              # the page's verdict (a PageVerdict value): a scan, or a typed page with a picture
    retry: bool = False            # it ran out of time once and waits for a second try with more time


OCR_WAITING, OCR_DONE, OCR_FAILED = "waiting", "done", "failed"


# ---- 7. Sandbox ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Limits:
    seconds: float
    memory_mb: Optional[int] = None


@runtime_checkable
class Sandbox(Protocol):
    """Runs risky work in a separate worker with no network."""

    def run(self, job: str, payload: Mapping[str, object], limits: Limits,
            stop: Optional[StopSignal] = None) -> "object | Failure":
        """Failure(TIMED_OUT), Failure(OUT_OF_MEMORY) or Failure(CRASHED) when the worker was killed."""


# ---- 8. Annotator (level 2, later) ---------------------------------------------------------------------

@dataclass(frozen=True)
class Annotation:
    label: str                       # for example "date of birth"
    value: str
    evidence: Evidence
    confidence: Optional[float] = None
    verified: bool = False


@runtime_checkable
class Annotator(Protocol):
    def annotate(self, file_id: int, segment: SegmentIn) -> "Tuple[Annotation, ...] | Failure": ...


# ---- 9. Service ----------------------------------------------------------------------------------------
# What every client talks to. LocalService (service.py) is the in-process implementation; the HTTP client
# comes later and must fit the same shape. Calls that are not built yet answer Failure(UNSUPPORTED).

@runtime_checkable
class Service(Protocol):
    def search(self, query: str, limit: int = 100, offset: int = 0, sort="rank",
               descending: bool = False, filters: Optional[SearchFilters] = None) -> "SearchPage | Failure": ...

    def get_evidence(self, evidence: Evidence, context: int = 80) -> "Passage | Failure": ...

    def page_crop(self, file_id: int, page: int, box: Optional[Box] = None) -> "bytes | Failure": ...

    def list_roots(self) -> Sequence[Root]: ...

    def set_root(self, root: Root) -> "Root | Failure":
        """Add a folder, or change its policy and rules. A stricter policy removes what was stored."""

    def suggest_excludes(self, path: str, stop: Optional[StopSignal] = None) -> "Sequence[object] | Failure":
        """Folders that may not be worth indexing, each with its reason and size. Changes nothing."""

    def remove_root(self, path: str, purge: bool = True) -> "int | Failure":
        """Forget a folder. With `purge` its files leave the index; the answer is how many did."""

    def indexing_status(self) -> Mapping[str, object]: ...

    def needs_attention(self, limit: int = 100, offset: int = 0) -> Sequence[FileRecord]:
        """Files that failed or were read only in part (failed first), so a missing result is not mistaken for a
        missing file. Files that only wait for OCR are queued work, not trouble, and are counted separately."""

    def attention_counts(self) -> Mapping[str, int]: ...

    def start_indexing(self, mode: str = "on_demand") -> "None | Failure": ...   # idle, scheduled, on_demand or full_power

    def pause_indexing(self) -> None: ...

    def stop_indexing(self) -> None: ...

    def scan_now(self) -> "None | Failure": ...

    def find_moved(self, file_id: int) -> "Optional[str] | Failure": ...

    def record_usage(self, event: Mapping[str, object]) -> None: ...

    def doctor(self) -> Mapping[str, object]: ...


# ---- 10. IdleSource ------------------------------------------------------------------------------------

class PowerState(str, Enum):
    MAINS = "mains"
    BATTERY = "battery"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PowerReading:
    state: PowerState
    charge_percent: Optional[int] = None
    battery_saver: Optional[bool] = None


def is_plugged_in(reading: PowerReading) -> bool:
    """Unknown is not a pause (review): a desktop, or a reading that failed, counts as plugged in."""
    return reading.state is not PowerState.BATTERY


@runtime_checkable
class IdleSource(Protocol):
    def is_idle(self, seconds: float) -> bool: ...

    def power(self) -> PowerReading: ...

    def gpu_in_use_by_other(self) -> bool: ...
