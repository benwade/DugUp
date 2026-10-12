"""The indexer: bring the index up to date with the folders, in two resumable steps.

1. Scan each root with the FileSource and record what changed (new, changed, moved, gone).
2. Read every file that is waiting, one file per transaction, through the Extractor.

Everything it knows it gets through three contracts (Store, FileSource, Extractor), so each can be swapped.

Promises (guide section 2):
- Resumable: a file waits in state `pending` until it has been read, so a run that stops or dies is
  continued by the next run. A re-run on an unchanged tree changes nothing.
- Interruptible: the stop signal is looked at between files and passed into every scan and read.
- Never silently empty: every file ends in a stated state. A file whose pages wait for OCR, or that was
  read only in part, is `partial` and says why; one that cannot be read is `failed` or `skipped` with the reason.
- Careful with loss: a root that cannot be reached changes nothing. A root that is suddenly empty, or lost
  most of its files at once, is reported and nothing is marked missing. Missing files keep their text and
  are removed only after the retention time, and only when the root is reachable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .filesource import excluded
from .kinds import classify
from .protocols import (OCR_FAILED, OCR_WAITING, Extraction, Extractor, FileEntry, FileSource, ScanRules, Skipped,
                        StopSignal, Store)
from .settings import Settings
from .shapes import (POLICY_NAMES_ONLY, Failure, FailureReason, FileRecord, FileState, Root)

log = logging.getLogger("dugup.indexer")

OFFLINE_NOTE = "cloud placeholder: the content is not on this machine, so it was not read"
POLICY_NOTE = "names only: this folder's policy keeps file names and no text"
READERS_GENERATION = 2   # raise when the extractors change what they produce, so old text is offered a re-read
APPLY_CHUNK = 500        # changes written per transaction while a scan is recorded

OCR_WAIT = "page(s) wait for OCR"   # the note of a page that needs OCR; see only_waits_for_ocr
OCR_FAILED_NOTE = "OCR failed on"     # how the note of a page OCR could not read begins
RETRY_NOTE = "ran out of time once; waits for a second try with more time"   # the note of a file that stays queued
KEEP, REREAD, REFUSE = "keep", "reread", "refuse"
ROOM_EVERY = 25          # the free space on the index's drive is looked at once every this many files
# Reasons that may be the reader's or the machine's fault and not the file's. Enough in a row pause the run.
SUSPECT = (FailureReason.CRASHED, FailureReason.TIMED_OUT, FailureReason.OUT_OF_MEMORY)


def is_ocr_note(part: str) -> bool:
    return part.endswith(OCR_WAIT) or part.startswith(OCR_FAILED_NOTE)


def ocr_notes(status: Sequence[Tuple[int, str, str]], total_pages: int) -> List[str]:
    """The OCR part of a file's note, from what became of its pages (see Store.ocr_status)."""
    waiting = [page for page, state, _ in status if state == OCR_WAITING]
    failed = [(page, reason) for page, state, reason in status if state == OCR_FAILED]
    notes = []
    if waiting:
        notes.append(f"{len(waiting)} of {total_pages} {OCR_WAIT}")
    if failed:
        page, reason = failed[0]
        notes.append(f"{OCR_FAILED_NOTE} {len(failed)} of {total_pages} page(s) (first: page {page}, {reason})")
    return notes


def note_with_ocr(store: Store, row_id: int, note: str) -> Tuple[FileState, str]:
    """A file's state and note with the OCR part rewritten from what the store knows about its pages."""
    others = [part for part in note.split("; ") if part and not is_ocr_note(part)]
    notes = ocr_notes(store.ocr_status(row_id), len(store.get_pages(row_id))) + others
    return (FileState.PARTIAL if notes else FileState.DONE), "; ".join(notes)


def only_waits_for_ocr(note: str) -> bool:
    """True when a partial file's whole note is "N of M page(s) wait for OCR": nothing went wrong with it,
    it is queued for a step (OCR) that has not run yet."""
    return note.endswith(OCR_WAIT) and ";" not in note


def safe_rate(count: float, seconds: float) -> float:
    return count / seconds if seconds > 0 else 0.0


@dataclass(frozen=True)
class Progress:
    phase: str            # "scan", "record" or "read"
    root: str
    done: int
    total: int
    failed: int = 0
    elapsed: float = 0.0
    current: str = ""     # the file being read; for display only, never logged

    @property
    def percent(self) -> float:
        return 100.0 * self.done / self.total if self.total > 0 else 0.0

    @property
    def rate(self) -> float:
        """Files a second (0 when no time has passed)."""
        return safe_rate(self.done, self.elapsed)

    @property
    def remaining_seconds(self) -> Optional[float]:
        """Estimated seconds left, or None when there is nothing to base an estimate on."""
        rate = self.rate
        if rate <= 0 or self.total <= self.done:
            return None if self.total > self.done else 0.0
        return (self.total - self.done) / rate


@dataclass
class RootReport:
    path: str
    reachable: bool = True
    detail: str = ""                 # why the root could not be scanned, when it could not
    added: int = 0
    changed: int = 0
    moved: int = 0
    missing: int = 0                 # newly found gone
    restored: int = 0                # were missing, are back unchanged
    purged: int = 0                  # missing for longer than the retention time
    held_back: int = 0               # disappearances not recorded because of a warning
    warnings: Tuple[str, ...] = ()
    skipped: Tuple[Skipped, ...] = ()   # folders the scan could not read


@dataclass
class IndexReport:
    roots: List[RootReport] = field(default_factory=list)
    read: int = 0                    # files that went through a reader this run
    done: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0
    vanished: int = 0                # gone between the scan and the read; left for the next scan
    second_tries: int = 0            # files that ran out of time and were read again, last, with more time
    paused: str = ""                 # why the run paused itself (a run of reader failures, or a full drive)
    stopped: bool = False
    settings_changed: Tuple[str, ...] = ()   # set, with no work done, when the caller must decide first
    seconds: float = 0.0

    @property
    def rate(self) -> float:
        return safe_rate(self.read, self.seconds)


def outcome_of(result: "Extraction | Failure") -> Tuple[Optional[FileState], str]:
    """The state and note a file gets from what its reader answered. (None, ...) means: leave it waiting."""
    if isinstance(result, Failure):
        if result.reason in (FailureReason.STOPPED, FailureReason.UNREACHABLE):
            return None, result.detail
        if result.reason is FailureReason.UNSUPPORTED:
            return FileState.SKIPPED, result.detail
        return FileState.FAILED, f"{result.reason.value}: {result.detail}"
    notes: List[str] = []
    total = len(result.pages)
    waiting = len(result.pages_needing_ocr)
    unreadable = [p for p in result.pages if p.unread_reason]
    if waiting:
        notes.append(f"{waiting} of {total} {OCR_WAIT}")
    if unreadable:
        notes.append(f"{len(unreadable)} page(s) could not be read (first: page {unreadable[0].page}, {unreadable[0].unread_reason})")
    notes.extend(result.unread)
    return (FileState.PARTIAL if notes else FileState.DONE), "; ".join(notes)


class Indexer:
    def __init__(self, store: Store, source: FileSource, extractor: Extractor, settings: Optional[Settings] = None,
                 progress: Optional[Callable[[Progress], None]] = None, clock: Callable[[], float] = time.time,
                 timer: Callable[[], float] = time.monotonic, extra_excludes: Sequence[str] = (),
                 room: Optional[Callable[[], str]] = None) -> None:
        self.room = room         # says why nothing more may be written (the drive is nearly full, or the
                                 # index reached its spending limit); "" when there is room
        self.store = store
        self.source = source
        self.extractor = extractor
        self.settings = settings or Settings()
        self.progress = progress
        self.clock = clock            # wall time, for "missing since" and "last scan"
        self.timer = timer            # elapsed time, for rates
        self.extra_excludes = tuple(extra_excludes)   # absolute folders never indexed (the data folder)

    # -- the fingerprint: what the stored text was made with

    def fingerprint(self) -> Dict[str, object]:
        current = dict(self.settings.fingerprint())
        current["readers"] = READERS_GENERATION
        return current

    def _settle_fingerprint(self, on_change: str) -> Tuple[str, ...]:
        """() when work may go on. Otherwise the changed keys, and nothing has been touched."""
        current = self.fingerprint()
        if self.store.get_fingerprint() is None:
            self.store.set_fingerprint(current)
            return ()
        changed = tuple(self.store.changed_fingerprint_keys(current))
        if not changed:
            return ()
        if on_change == REFUSE:
            return changed
        if on_change not in (KEEP, REREAD):
            raise ValueError(f"on_settings_change must be one of {KEEP}, {REREAD}, {REFUSE}")
        with self.store.transaction():
            if on_change == REREAD:
                self.requeue(states=(FileState.DONE, FileState.PARTIAL, FileState.FAILED, FileState.SKIPPED))
            self.store.set_fingerprint(current)
        return ()

    def requeue(self, states: Sequence[FileState], prefix: Optional[str] = None) -> int:
        """Put files back in the queue (not placeholders, not files held back by policy). Returns how many."""
        count = 0
        with self.store.transaction():
            for state in states:
                for record in self.store.list_files(prefix=prefix, state=state):
                    if record.state is FileState.SKIPPED and record.error in (OFFLINE_NOTE, POLICY_NOTE):
                        continue
                    self.store.set_state(record.row_id, FileState.PENDING)
                    count += 1
        return count

    # -- the run

    def run(self, stop: Optional[StopSignal] = None, roots: Optional[Sequence[str]] = None,
            on_settings_change: str = REFUSE, retry_failed: bool = False) -> IndexReport:
        started = self.timer()
        report = IndexReport()
        changed = self._settle_fingerprint(on_settings_change)
        if changed:
            report.settings_changed = changed
            return report
        report.paused = self._room()
        if report.paused:
            return report
        chosen = [r for r in self.store.list_roots() if roots is None or r.path in roots]
        if retry_failed:
            for root in chosen:
                self.requeue((FileState.FAILED,), root.path)
        readable: List[Root] = []
        for root in chosen:
            if _stopped(stop):
                report.stopped = True
                break
            root_report, finished = self.scan_root(root, stop)
            report.roots.append(root_report)
            if not finished:
                report.stopped = True
                break
            if root_report.reachable:
                readable.append(root)
        if not report.stopped:
            for root in readable:
                if not self.read_pending(root, report, stop):
                    report.stopped = not report.paused
                    break
        report.seconds = self.timer() - started
        log.info("run finished: read=%d done=%d partial=%d failed=%d skipped=%d stopped=%s seconds=%.1f",
                 report.read, report.done, report.partial, report.failed, report.skipped, report.stopped, report.seconds)
        return report

    # -- step 1: scan and record

    def rules_for(self, root: Root) -> ScanRules:
        """The root's own rules, plus the folders that are never indexed when they lie inside this root."""
        extra = []
        base = root.path.rstrip("/\\")
        for folder in self.extra_excludes:
            for sep in ("/", "\\"):
                if folder.startswith(base + sep):
                    extra.append(folder[len(base) + 1:].replace("\\", "/"))
                    break
        return ScanRules(tuple(root.include), tuple(root.exclude) + tuple(extra))

    def _fresh(self, root: Root, entry: FileEntry) -> Tuple[FileState, str]:
        if root.policy == POLICY_NAMES_ONLY:
            return FileState.SKIPPED, POLICY_NOTE
        if entry.offline:
            return FileState.SKIPPED, OFFLINE_NOTE
        return FileState.PENDING, ""

    def _record_for(self, root: Root, entry: FileEntry) -> FileRecord:
        state, note = self._fresh(root, entry)
        return FileRecord(path=entry.path, size=entry.size, modified=entry.modified,
                          kind=classify(entry.path, self.settings.extensions), file_id=entry.file_id,
                          state=state, error=note, policy=root.policy)

    def scan_root(self, root: Root, stop: Optional[StopSignal] = None) -> Tuple[RootReport, bool]:
        """(report, finished). `finished` is False when a stop request ended the scan early."""
        report = RootReport(root.path)
        self._tell(Progress("scan", root.path, 0, 0))
        previous: Dict[str, FileEntry] = {}
        for record in self.store.list_files(prefix=root.path, missing=False):
            offline = record.state is FileState.SKIPPED and record.error == OFFLINE_NOTE
            previous[record.path] = FileEntry(record.path, record.size, record.modified, record.file_id, offline)
        changes = self.source.changes(root.path, previous, self.rules_for(root), stop)
        now = self.clock()
        if isinstance(changes, Failure):
            if changes.reason is FailureReason.STOPPED:
                return report, False
            report.reachable = False
            report.detail = changes.detail
            self.store.note_scan(root.path, False, now)
            log.info("root not scanned: reason=%s", changes.reason.value)
            return report, True

        report.warnings = tuple(changes.warnings)
        report.skipped = tuple(changes.skipped)
        hold_removals = bool(changes.warnings)
        changed_paths = {e.path for e in changes.changed}
        steps: List[Callable[[], None]] = []

        def add(entry: FileEntry) -> None:
            existing = self.store.get_file(entry.path)
            if (existing is not None and existing.missing_since is not None and existing.state is not FileState.PENDING
                    and existing.size == entry.size and existing.modified == entry.modified
                    and self._fresh(root, entry)[0] is FileState.PENDING):
                self.store.set_missing(existing.row_id, None)   # back, unchanged: its text is still right
                report.restored += 1
                return
            self._new_content(root, entry)
            report.added += 1

        def move(old: str, entry: FileEntry) -> None:
            target = self.store.get_file(entry.path)
            if target is not None and target.missing_since is not None:
                # the new path is where a file went missing earlier: that old record makes way
                self.store.purge_missing(entry.path, float("inf"))
                target = None
            if target is not None or self.store.get_file(old) is None:
                gone(old)     # should not happen; if it does, fall back to "one gone, one new"
                add(entry)
                return
            self.store.move_file(old, entry.path)
            report.moved += 1
            if entry.path not in changed_paths and self._fresh(root, entry)[0] is not FileState.PENDING:
                self.store.upsert_file(self._record_for(root, entry))

        def change(entry: FileEntry) -> None:
            self._new_content(root, entry)   # the old text stays until the file is re-read
            report.changed += 1

        def gone(path: str) -> None:
            if hold_removals:
                report.held_back += 1
                return
            existing = self.store.get_file(path)
            if existing is not None and existing.missing_since is None:
                self.store.set_missing(existing.row_id, now)
                report.missing += 1

        steps += [lambda m=m: move(*m) for m in changes.moved]
        steps += [lambda e=e: add(e) for e in changes.added]
        steps += [lambda e=e: change(e) for e in changes.changed]
        steps += [lambda p=p: gone(p) for p in changes.removed]
        started = self.timer()
        for at in range(0, len(steps), APPLY_CHUNK):
            if _stopped(stop):
                return report, False   # what was recorded so far is kept; the next scan finds the rest again
            with self.store.transaction():
                for step in steps[at:at + APPLY_CHUNK]:
                    step()
            self._tell(Progress("record", root.path, min(at + APPLY_CHUNK, len(steps)), len(steps),
                                elapsed=self.timer() - started))
        with self.store.transaction():
            if not hold_removals:
                before = now - self.settings.missing_retention_days * 86400.0
                report.purged = self.store.purge_missing(root.path, before)
            self.store.note_scan(root.path, True, now, report.warnings)
        log.info("root scanned: added=%d changed=%d moved=%d missing=%d restored=%d purged=%d held_back=%d "
                 "warnings=%s unreadable_folders=%d", report.added, report.changed, report.moved, report.missing,
                 report.restored, report.purged, report.held_back, ",".join(report.warnings) or "-", len(report.skipped))
        return report, True

    # -- step 2: read what is waiting

    def _new_content(self, root: Root, entry: FileEntry) -> None:
        """Record a file whose content is new or different. What OCR read from the old content is let go:
        with its page records cleared, the next read keeps none of it (see Store.replace_text)."""
        row_id = self.store.upsert_file(self._record_for(root, entry))
        self.store.set_pages(row_id, ())

    def _room(self) -> str:
        return "" if self.room is None else str(self.room() or "")

    def _patient(self):
        """The extractor's more patient read, when it has one and second tries are on."""
        if self.settings.retry_patience <= 1.0:
            return None
        return getattr(self.extractor, "extract_patiently", None)

    def read_pending(self, root: Root, report: IndexReport, stop: Optional[StopSignal] = None) -> bool:
        """Read every waiting file below a root. False when a stop request, or a pause, ended it early.

        A file that runs out of time is not failed at once. It keeps its place in the queue with a note,
        and is read again after everything else, with more time; only a second timeout fails it. The note
        is stored, so a run that stops before that last pass still knows which files have had their first try.
        """
        total = int(dict(self.store.state_counts(root.path)).get(FileState.PENDING.value, 0))
        started = self.timer()
        done = failed = in_a_row = 0
        later: List[FileRecord] = []
        cursor = 0

        def one(record: FileRecord, patient: bool) -> Optional[bool]:
            """None to carry on, else what read_pending returns."""
            nonlocal done, failed, in_a_row
            if _stopped(stop):
                return False
            if (done + len(later)) % ROOM_EVERY == 0:
                report.paused = self._room()
                if report.paused:
                    return False
            self._tell(Progress("read", root.path, done, total, failed, self.timer() - started, record.path))
            state = self.read_one(record, report, stop, patient)
            if state is None and _stopped(stop):
                return False
            if state is FileState.PENDING:      # out of time once: it waits for the last pass
                later.append(record)
            else:
                done += 1
                failed += state is FileState.FAILED
            in_a_row = in_a_row + 1 if self._last_reason in SUSPECT else 0
            limit = self.settings.reader_failures_in_a_row
            if limit and in_a_row >= limit:
                report.paused = (f"{in_a_row} files in a row crashed their reader or ran out of time (the last one: "
                                 f"{self._last_reason.value.replace('_', ' ')}), which looks like trouble with the "
                                 "program or the machine and not with the files. The rest stays queued")
                return False
            return None

        while True:
            batch = self.store.list_files(prefix=root.path, state=FileState.PENDING, missing=False,
                                          limit=self.settings.batch_files, after_row=cursor)
            if not batch:
                break
            for record in batch:
                cursor = record.row_id
                if record.error == RETRY_NOTE and self._patient() is not None:
                    later.append(record)       # had its first try in an earlier run
                    continue
                answer = one(record, False)
                if answer is not None:
                    return answer
            self._tell(Progress("read", root.path, done, max(total, done), failed, self.timer() - started))
        second = list(later)
        del later[:]
        for record in second:
            answer = one(record, True)
            if answer is not None:
                return answer
        if second:
            self._tell(Progress("read", root.path, done, max(total, done), failed, self.timer() - started))
        return True

    _last_reason: Optional[FailureReason] = None

    def read_one(self, record: FileRecord, report: IndexReport, stop: Optional[StopSignal] = None,
                 patient: bool = False) -> Optional[FileState]:
        """Read one file and store the outcome. Returns its new state; None when it was left waiting untouched;
        PENDING when it ran out of time for the first time and now waits for a more patient second try."""
        self._last_reason = None
        patiently = self._patient()
        try:
            if patient and patiently is not None:
                report.second_tries += 1
                result = patiently(record, stop, self.settings.retry_patience)
            else:
                result = self.extractor.extract(record, stop)
        except Exception as exc:   # a reader bug must cost one file, not the run
            result = Failure(FailureReason.CRASHED, f"the reader raised {type(exc).__name__}: {exc}")
        if result is None:
            result = Failure(FailureReason.OTHER, "the reader returned nothing")
        if isinstance(result, Failure):
            self._last_reason = result.reason
            if result.reason is FailureReason.TIMED_OUT and not patient and patiently is not None:
                self.store.set_state(record.row_id, FileState.PENDING, RETRY_NOTE)
                return FileState.PENDING
        state, note = outcome_of(result)
        if state is None:
            if isinstance(result, Failure) and result.reason is FailureReason.UNREACHABLE:
                report.vanished += 1
            return None
        with self.store.transaction():
            # A file that could not be read keeps no text from an earlier version of itself: stale text
            # would be found and shown as if it were still in the file. Its name stays searchable.
            if isinstance(result, Extraction):
                # what OCR already read from this same content is kept (a re-read after a settings change)
                self.store.replace_text(record.row_id, result.segments, result.pages)
                state, note = note_with_ocr(self.store, record.row_id, note)
            else:
                self.store.replace_segments(record.row_id, ())
                self.store.set_pages(record.row_id, ())
            self.store.set_state(record.row_id, state, note)
        report.read += 1
        if state is FileState.DONE:
            report.done += 1
        elif state is FileState.PARTIAL:
            report.partial += 1
        elif state is FileState.SKIPPED:
            report.skipped += 1
        else:
            report.failed += 1
            log.info("file not read: row=%d reason=%s", record.row_id, note.split(":", 1)[0])
        return state

    def _tell(self, progress: Progress) -> None:
        if self.progress is not None:
            self.progress(progress)


def _stopped(stop: Optional[StopSignal]) -> bool:
    return stop is not None and stop.is_set()
