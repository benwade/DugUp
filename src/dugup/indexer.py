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
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .filesource import excluded
from .kinds import classify, is_lock_file
from .protocols import (OCR_FAILED, OCR_WAITING, Extraction, Extractor, FileEntry, FileSource, ScanRules, Skipped,
                        StopSignal, Store)
from .settings import Settings
from .shapes import (LEVEL_NAMES, OPTION_WORDS, POLICY_NAMES_ONLY, level_for, wants, Failure, FailureReason, FileRecord, FileState, Root)

log = logging.getLogger("dugup.indexer")

ESTIMATE_FIRST = 2000    # after this many files of a large run, say where the index is heading ...
ESTIMATE_STEP = 5        # ... and again at 5 times as many, and so on
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


def cannot_be_read(note: str) -> bool:
    """True when everything a file's note says is "damaged" or "needs a password": the file is known, its
    name is searchable, and nothing done here will read it. A limit that was reached, a timeout or a crash
    is not that: a setting or another try may help, so those stay worth a look."""
    parts = [part for part in note.split("; ") if part and not (part.endswith(OCR_WAIT))]
    if not parts:
        return False
    for part in parts:
        hopeless = ("corrupt:" in part or "password_protected:" in part) and "larger than the limit" not in part
        if not hopeless:
            return False
    return True


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
    projected_bytes: float = 0.0   # phase "estimate": where the index would end up if the rest is like the start

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
    copied: int = 0                  # files whose content was already read in another place: its text was taken
                                     # from there, and the file itself was not read again
    marked: int = 0                  # files read before content marks existed that were given one this run
    paused: str = ""                 # why the run paused itself (a run of reader failures, or a full drive)
    compacted: Optional[Dict[str, int]] = None   # the index's size before and after, when it was compacted at the end
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
                 room: Optional[Callable[[], str]] = None,
                 size_now: Optional[Callable[[], int]] = None, case_insensitive: Optional[bool] = None,
                 extra_extractors: Sequence[Extractor] = ()) -> None:
        # more readers, each with a worker of its own: files are then read side by side. Reading is all they
        # do; everything is stored by the one thread that runs the indexer.
        self.extra_extractors = tuple(extra_extractors)
        self.case_insensitive = (os.name == "nt") if case_insensitive is None else case_insensitive
        self.size_now = size_now   # the index's size on disk in bytes, for the early estimate; None: no estimate
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
        if not report.stopped and not report.paused and self.settings.content_marks:
            for root in readable:
                if not self.mark_pending(root, report, stop):
                    report.stopped = True
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
        if not wants(level_for(root, entry.path, self.case_insensitive), OPTION_WORDS):   # nothing here asks for the text
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
        self.store.set_content(row_id, None)      # the old mark was of the old content

    def _size(self) -> int:
        """The index's size now, or -1 when it cannot be told."""
        if self.size_now is None:
            return -1
        try:
            return int(self.size_now())
        except OSError:
            return -1

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

        With more readers (extra_extractors) files are read side by side: a slow file keeps one reader busy
        while the others go on. Whatever is read is stored here, by this one thread, as each reader finishes.
        """
        total = int(dict(self.store.state_counts(root.path)).get(FileState.PENDING.value, 0))
        started = self.timer()
        size_at_start = self._size()
        estimate_at = ESTIMATE_FIRST if total >= ESTIMATE_FIRST * 2 else 0
        done = failed = in_a_row = 0
        later: List[FileRecord] = []
        cursor = 0
        # The marks of content that has been read already. A reader that meets one of them does not read the
        # file: its text is taken from the copy. (Readers only look in this set; this thread alone adds to it.)
        self._known_marks = set(self.store.read_marks()) if self.settings.content_marks and self.settings.read_once else set()
        readers = _Readers((self.extractor, *self.extra_extractors)) if self.extra_extractors else None
        ended: List[bool] = []            # what ended the run early, when something did

        def before(record: FileRecord) -> Optional[bool]:
            """None to carry on, else what read_pending returns."""
            if _stopped(stop):
                return False
            if (done + len(later)) % ROOM_EVERY == 0:
                report.paused = self._room()
                if report.paused:
                    return False
            self._tell(Progress("read", root.path, done, total, failed, self.timer() - started, record.path))
            return None

        def after(record: FileRecord, state: Optional[FileState]) -> Optional[bool]:
            nonlocal done, failed, in_a_row, estimate_at
            if state is None and _stopped(stop):
                return False
            if state is FileState.PENDING:      # out of time once: it waits for the last pass
                later.append(record)
            else:
                done += 1
                if estimate_at and done >= estimate_at:
                    grown = self._size() - size_at_start
                    if size_at_start >= 0 and grown > 0:
                        projected = self._size() + grown / done * (total - done)
                        self._tell(Progress("estimate", root.path, done, total, failed, self.timer() - started,
                                            projected_bytes=projected))
                    estimate_at = estimate_at * ESTIMATE_STEP if estimate_at * ESTIMATE_STEP < total else 0
                failed += state is FileState.FAILED
            in_a_row = in_a_row + 1 if self._last_reason in SUSPECT else 0
            # Side by side, slow files come in clusters (a folder of large files keeps every reader busy at
            # once), so "in a row" is counted for each reader: the setting times the number of readers.
            limit = self.settings.reader_failures_in_a_row * (1 + len(self.extra_extractors))
            if limit and in_a_row >= limit:
                report.paused = (f"{in_a_row} files in a row crashed their reader or ran out of time (the last one: "
                                 f"{self._last_reason.value.replace('_', ' ')}), which looks like trouble with the "
                                 "program or the machine and not with the files. The rest stays queued")
                return False
            return None

        def take(wait: bool) -> None:
            """Store what the readers have finished. `wait`: until at least one has."""
            for record, patient, result in readers.finished(wait):
                if patient and self._patient() is not None:
                    report.second_tries += 1
                answer = after(record, self._keep(record, result, report, patient))
                if answer is not None:
                    ended.append(answer)
            if wait:        # the line stays alive while files are slow, and names the one that has taken longest
                self._tell(Progress("read", root.path, done, max(total, done), failed, self.timer() - started,
                                    readers.longest()))

        def one(record: FileRecord, patient: bool) -> Optional[bool]:
            answer = before(record)
            if answer is not None:
                return answer
            if readers is None:
                return after(record, self.read_one(record, report, stop, patient))
            while not readers.free() and not ended:
                take(True)
            if ended:
                return ended[0]
            readers.start(lambda extractor: self._read(extractor, record, stop, patient), record, patient)
            take(False)
            return ended[0] if ended else None

        def settle() -> Optional[bool]:
            """Wait for the readers still at work and store what they read."""
            while readers is not None and readers.busy():
                take(True)
            return ended[0] if ended else None

        try:
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
                        settle()
                        return answer
                self._tell(Progress("read", root.path, done, max(total, done), failed, self.timer() - started))
            answer = settle()                      # the first tries are all in before the second ones start
            if answer is not None:
                return answer
            second = list(later)
            del later[:]
            for record in second:
                answer = one(record, True)
                if answer is not None:
                    settle()
                    return answer
            answer = settle()
            if answer is not None:
                return answer
            if second:
                self._tell(Progress("read", root.path, done, max(total, done), failed, self.timer() - started))
            return True
        finally:
            if readers is not None:
                readers.close()

    def mark_pending(self, root: Root, report: IndexReport, stop: Optional[StopSignal] = None) -> bool:
        """Give a content mark to the files below a root that were read before marks existed. Each file is
        read once from start to end and nothing else about it changes. False when a stop ended it early;
        what was marked is kept and the next run goes on from there."""
        first = self.store.unmarked(prefix=root.path, limit=1, case_insensitive=self.case_insensitive)
        if not first or getattr(self.extractor, "content_mark", None) is None:
            return True
        waiting = self.store.unmarked(prefix=root.path, case_insensitive=self.case_insensitive)
        total, done, started = len(waiting), 0, self.timer()
        extractors = (self.extractor, *(e for e in self.extra_extractors if getattr(e, "content_mark", None) is not None))
        readers = _Readers(extractors) if len(extractors) > 1 else None

        def keep(record: FileRecord, mark) -> None:
            nonlocal done
            if isinstance(mark, Failure) and mark.reason is FailureReason.STOPPED:
                return
            # one that could not be marked (gone, unreadable, out of time) gets a mark of its own: not asked again
            self.store.set_content(record.row_id, mark if isinstance(mark, str) and mark else f"!{record.row_id}")
            done += 1
            report.marked += 1

        def take(wait: bool) -> None:
            for record, _, mark in readers.finished(wait):
                keep(record, mark)
            if wait:
                self._tell(Progress("mark", root.path, done, total, 0, self.timer() - started, readers.longest()))

        try:
            for record in waiting:
                if _stopped(stop):
                    break
                self._tell(Progress("mark", root.path, done, total, 0, self.timer() - started, record.path))
                if record.size <= 0:
                    keep(record, None)
                    continue
                if readers is None:
                    try:
                        keep(record, self.extractor.content_mark(record, stop))
                    except Exception:
                        keep(record, None)
                    continue
                while not readers.free():
                    take(True)
                readers.start(lambda extractor, record=record: _safely(extractor.content_mark, record, stop), record, False)
                take(False)
            while readers is not None and readers.busy():
                take(True)
        finally:
            if readers is not None:
                readers.close()
        self._tell(Progress("mark", root.path, done, total, 0, self.timer() - started))
        return not _stopped(stop)

    _last_reason: Optional[FailureReason] = None
    _known_marks: set = frozenset()    # type: ignore[assignment]

    def read_one(self, record: FileRecord, report: IndexReport, stop: Optional[StopSignal] = None,
                 patient: bool = False) -> Optional[FileState]:
        """Read one file and store the outcome. Returns its new state; None when it was left waiting untouched;
        PENDING when it ran out of time for the first time and now waits for a more patient second try."""
        if patient and self._patient() is not None:
            report.second_tries += 1
        return self._keep(record, self._read(self.extractor, record, stop, patient), report, patient)

    def _read(self, extractor, record: FileRecord, stop: Optional[StopSignal], patient: bool):
        """The reading half: no index, no counters, so several of these may run side by side. Returns what the
        reader answered and the mark of the file's content (None when it could not be taken)."""
        if is_lock_file(record.path):      # one recorded as a document before lock files were known
            return Failure(FailureReason.UNSUPPORTED, "an editing lock file, not a document"), None
        mark = self._mark(extractor, record, stop)
        if isinstance(mark, Failure):      # stopped, or the file is gone: there is nothing to read either
            return mark, None
        if mark is not None and (mark, record.kind.value) in self._known_marks:
            return SAME_AS_ANOTHER, mark
        return self._read_itself(extractor, record, stop, patient, mark)

    def _read_itself(self, extractor, record: FileRecord, stop: Optional[StopSignal], patient: bool, mark=None):
        """The file's own reader at work, whatever is known of its content."""
        patiently = getattr(extractor, "extract_patiently", None) if self.settings.retry_patience > 1.0 else None
        try:
            if patient and patiently is not None:
                result = patiently(record, stop, self.settings.retry_patience)
            else:
                result = extractor.extract(record, stop)
        except Exception as exc:   # a reader bug must cost one file, not the run
            result = Failure(FailureReason.CRASHED, f"the reader raised {type(exc).__name__}: {exc}")
        if result is None:
            result = Failure(FailureReason.OTHER, "the reader returned nothing")
        return result, mark

    def _mark(self, extractor, record: FileRecord, stop: Optional[StopSignal]):
        """The content mark of a file, None when there is none to be had, or a Failure that ends the read
        (a stop, or a file that is no longer there)."""
        marker = getattr(extractor, "content_mark", None) if self.settings.content_marks else None
        if marker is None or record.size <= 0:      # empty files are all alike, and are not copies of each other
            return None
        try:
            mark = marker(record, stop)
        except Exception:                  # a mark is a help, never a reason to lose the file
            return None
        if isinstance(mark, Failure):
            return mark if mark.reason in (FailureReason.STOPPED, FailureReason.UNREACHABLE) else None
        return mark or None

    def _still_wants_text(self, path: str) -> bool:
        """The folder's level as it is NOW: a level lowered to "names" while a reader was busy with the file
        means the text it brought back must not be kept."""
        fold = (lambda p: p.casefold()) if self.case_insensitive else (lambda p: p)
        for root in self.store.list_roots():
            base = fold(root.path.rstrip("/\\"))
            folded = fold(path)
            if folded == base or folded.startswith(base + "/") or folded.startswith(base + "\\"):
                return wants(level_for(root, path, self.case_insensitive), OPTION_WORDS)
        return True

    def _keep(self, record: FileRecord, result, report: IndexReport, patient: bool) -> Optional[FileState]:
        """The storing half: always on the indexer's own thread. `result` is what _read returned."""
        result, mark = result if isinstance(result, tuple) else (result, None)
        if not self._still_wants_text(record.path):
            with self.store.transaction():
                self.store.replace_segments(record.row_id, ())
                self.store.set_pages(record.row_id, ())
                self.store.set_state(record.row_id, FileState.SKIPPED, POLICY_NOTE)
            report.skipped += 1
            return FileState.SKIPPED
        if result is SAME_AS_ANOTHER:
            state = self._keep_copy(record, mark, report)
            if state is None:              # the copy it was to be taken from is gone since: read it after all
                result, _ = self._read_itself(self.extractor, record, None, patient)
                state = self._keep_result(record, result, report, patient)
        else:
            state = self._keep_result(record, result, report, patient)
        if mark and state in (FileState.DONE, FileState.PARTIAL):
            self._known_marks.add((mark, record.kind.value))
        if state is not None and state is not FileState.PENDING and self.settings.content_marks:
            # a file that was read and gave no mark gets one of its own, so it is not asked again every run
            self.store.set_content(record.row_id, mark or f"!{record.row_id}")
        return state

    def _keep_copy(self, record: FileRecord, mark: str, report: IndexReport) -> Optional[FileState]:
        """Take a file's text from another file with the same content. None when there is no such file now."""
        self._last_reason = None
        twin = self.store.read_twin(mark, record.row_id, record.kind)
        if twin is None:
            return None
        with self.store.transaction():
            self.store.copy_reading(twin.row_id, record.row_id)
            state, note = note_with_ocr(self.store, record.row_id, twin.error)
            self.store.set_state(record.row_id, state, note)
        report.read += 1
        report.copied += 1
        if state is FileState.DONE:
            report.done += 1
        else:
            report.partial += 1
        return state

    def _keep_result(self, record: FileRecord, result, report: IndexReport, patient: bool) -> Optional[FileState]:
        self._last_reason = None
        patiently = self._patient()
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


SAME_AS_ANOTHER = object()     # what a reader answers for a file whose content has been read in another place


def _safely(call, *args):
    try:
        return call(*args)
    except Exception:
        return None


def _stopped(stop: Optional[StopSignal]) -> bool:
    return stop is not None and stop.is_set()


WAIT_TICK = 1.0         # seconds between two progress lines while every reader is busy


class _Readers:
    """Several extractors at work side by side. Each reads one file at a time on a thread of its own; what
    they read is handed back to the thread that asked, which is the only one that touches the index."""

    def __init__(self, extractors: Sequence[Extractor]) -> None:
        import queue
        from concurrent.futures import ThreadPoolExecutor
        self._idle = list(reversed(extractors))    # taken from the end: the first reader given is used first
        self._at_work: List[FileRecord] = []       # the files being read now, the longest-running first
        self._count = len(self._idle)
        self._done: "queue.Queue[tuple]" = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=self._count, thread_name_prefix="dugup-reader")

    def free(self) -> bool:
        return bool(self._idle)

    def busy(self) -> bool:
        return len(self._idle) < self._count

    def start(self, read: Callable[[Extractor], object], record: FileRecord, patient: bool) -> None:
        extractor = self._idle.pop()
        self._at_work.append(record)

        def work() -> None:
            try:
                result = read(extractor)
            except BaseException as exc:      # the reading half catches its own; this is the last line of defence
                result = Failure(FailureReason.CRASHED, f"the reader raised {type(exc).__name__}: {exc}")
            self._done.put((extractor, record, patient, result))

        self._pool.submit(work)

    def finished(self, wait: bool) -> List[tuple]:
        """(record, patient, result) for every read that has ended. With `wait`, up to a moment is spent
        waiting for the first; an empty answer then means every reader is still at work."""
        import queue
        out = []
        while self.busy() or not self._done.empty():
            try:
                extractor, record, patient, result = self._done.get(timeout=WAIT_TICK) if wait and not out else self._done.get_nowait()
            except queue.Empty:
                break                          # nothing yet: the caller says so and asks again
            self._idle.append(extractor)
            self._at_work = [r for r in self._at_work if r is not record]
            out.append((record, patient, result))
        return out

    def longest(self) -> str:
        """The file that has been in a reader the longest, for the progress line."""
        return self._at_work[0].path if self._at_work else ""

    def close(self) -> None:
        self._pool.shutdown(wait=True)
