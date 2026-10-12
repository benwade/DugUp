"""LocalService: the Service contract, in-process (guide 5b, CONTRACTS.md part 9).

The command line and, later, the window and the HTTP adapter are written against the Service and
never touch the store. That keeps three promises in one place:
- Policy is enforced here, once (a names-only folder keeps no text, whoever asks).
- One writer: an indexing run holds the run lock and its own connection; everything else only reads.
- A result is honest: it carries Evidence, true counts, and flags for files that are missing or whose
  folder cannot be reached right now.

Threads: SQLite connections stay in the thread that opened them. The service keeps one connection for
the caller's thread and an indexing run opens its own, so a window can ask for status while a run works.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from . import doctor as doctor_module
from .datadir import DataLayout, is_within
from .dirscan import DirectoryScan
from .extractors import default_extractors
from .extractors.sandboxed import SandboxedExtractor
from .indexer import POLICY_NOTE, REFUSE, Indexer, IndexReport, Progress, only_waits_for_ocr
from .lockfile import RunLock
from .filesource import matches_rules
from .protocols import Extractor, FileSource, ScanRules, StopSignal, Store
from .sandbox import SubprocessSandbox
from .settings import Settings
from .shapes import (POLICIES, POLICY_NAMES_ONLY, Box, Evidence, Failure, FailureReason, FileRecord, FileState,
                     Passage, Root, SearchPage)
from .store import SqliteStore
from .suggest import Suggestion, suggest_excludes

log = logging.getLogger("dugup.service")
NOT_BUILT = "this part of the program is not built yet"


def _under(path: str, folder: str, fold: Callable[[str], str]) -> bool:
    base = fold(folder.rstrip("/\\"))
    path = fold(path)
    return path == base or path.startswith(base + "/") or path.startswith(base + "\\")


class LocalService:
    def __init__(self, layout: DataLayout, settings: Optional[Settings] = None,
                 source: Optional[FileSource] = None, extractor: Optional[Extractor] = None,
                 open_store: Optional[Callable[[], Store]] = None, clock: Callable[[], float] = time.time,
                 case_insensitive: Optional[bool] = None) -> None:
        self.layout = layout
        self.settings = settings or Settings()
        self.source = source or DirectoryScan()
        if extractor is not None:
            self.extractor = extractor
        elif self.settings.isolate_readers:      # the default: files are parsed in a worker process
            self.extractor = SandboxedExtractor(SubprocessSandbox(), self.settings)
        else:
            self.extractor = default_extractors(self.settings)
        self._open_store = open_store or (lambda: SqliteStore(layout.index))
        self.clock = clock
        self.case_insensitive = (os.name == "nt") if case_insensitive is None else case_insensitive
        self._fold: Callable[[str], str] = (lambda t: t.lower()) if self.case_insensitive else (lambda t: t)
        self._store: Optional[Store] = None
        self._store_thread: Optional[int] = None
        self._lock = RunLock(layout.root / "index.lock")
        self._run_guard = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._progress: Optional[Progress] = None
        self.last_report: "Optional[IndexReport | Failure]" = None

    # -- plumbing

    @property
    def store(self) -> Store:
        """The reading connection of the calling thread's service. One thread uses a service for reading."""
        if self._store is None:
            self._store = self._open_store()
            self._store_thread = threading.get_ident()
        return self._store

    def close(self) -> None:
        self.stop_indexing()
        if self._store is not None:
            self._store.close()
            self._store = None
        close_readers = getattr(self.extractor, "close", None)
        if close_readers is not None:
            close_readers()      # ends any worker process and everything it started

    def __enter__(self) -> "LocalService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- search

    def search(self, query: str, limit: int = 100, offset: int = 0, sort: str = "rank",
               descending: bool = False) -> "SearchPage | Failure":
        try:
            page = self.store.search_words(query, limit, offset, sort, descending)
        except ValueError as exc:
            return Failure(FailureReason.UNSUPPORTED, str(exc))
        away = [r.path for r in self.store.list_roots() if not r.reachable]
        if away:
            for row in page.rows:
                row.unavailable = any(_under(row.path, folder, self._fold) for folder in away)
        return page

    def get_evidence(self, evidence: Evidence, context: int = 80) -> "Passage | Failure":
        passage = self.store.get_passage(evidence, context)
        if passage is None:
            return Failure(FailureReason.OTHER, "that passage is no longer in the index (the file was read again); search again")
        return passage

    def page_crop(self, file_id: int, page: int, box: Optional[Box] = None) -> "bytes | Failure":
        return Failure(FailureReason.UNSUPPORTED, f"page pictures: {NOT_BUILT}")

    # -- roots and policy

    def list_roots(self) -> Sequence[Root]:
        return list(self.store.list_roots())

    def set_root(self, root: Root) -> "Root | Failure":
        if root.policy not in POLICIES:
            return Failure(FailureReason.UNSUPPORTED, f"unknown policy {root.policy!r}; choose one of {', '.join(POLICIES)}")
        path = os.path.abspath(os.path.expanduser(root.path))
        known = {self._fold(r.path): r for r in self.store.list_roots()}
        old = known.get(self._fold(path))
        if old is None:
            if not os.path.isdir(path):
                return Failure(FailureReason.UNREACHABLE, f"not a folder that can be reached right now: {path}")
            if is_within(path, self.layout.root):
                return Failure(FailureReason.UNSUPPORTED, "that folder is inside the program's own data folder")
            for other in known.values():
                if _under(path, other.path, self._fold) or _under(other.path, path, self._fold):
                    return Failure(FailureReason.UNSUPPORTED,
                                   f"that folder overlaps a folder that is already indexed ({other.path}); "
                                   "one file must belong to one root")
        else:
            path = old.path   # keep the spelling the files were stored under
        wanted = Root(path, root.policy, tuple(root.include), tuple(root.exclude))
        with self.store.transaction():
            self.store.set_root(wanted)
            if old is not None and old.policy != wanted.policy:
                self._apply_policy(wanted)
            if old is not None and (old.include, old.exclude) != (wanted.include, wanted.exclude):
                self._apply_rules(wanted)
        return next(r for r in self.store.list_roots() if r.path == path)

    def _apply_rules(self, root: Root) -> int:
        """Take out of the index what the root's new rules leave out. The user chose to skip these files:
        they are not "missing", so they are removed now rather than flagged and kept (and a large skip can
        never look like a folder that lost its files). Files the new rules let in are found by the next scan."""
        rules = ScanRules(tuple(root.include), tuple(root.exclude))
        base = len(root.path.rstrip("/\\")) + 1
        doomed = [record.row_id for record in self.store.list_files(prefix=root.path, case_insensitive=self.case_insensitive)
                  if not matches_rules(record.path[base:], rules)]
        return self.store.remove_files(doomed)

    def suggest_excludes(self, path: str, stop: Optional[StopSignal] = None) -> "List[Suggestion] | Failure":
        """Look through a folder (nothing is indexed or changed) and name what may not be worth indexing.
        For a folder that is already a root, only what its current rules still let in is considered."""
        wanted = os.path.abspath(os.path.expanduser(path))
        known = next((r for r in self.store.list_roots() if self._fold(r.path) == self._fold(wanted)), None)
        rules = ScanRules(tuple(known.include), tuple(known.exclude)) if known else ScanRules()
        target = known.path if known else wanted
        listing = self.source.scan(target, rules, stop)
        if isinstance(listing, Failure):
            return listing
        return suggest_excludes(target, ((e.path, e.size) for e in listing.entries), self.settings.extensions)

    def _apply_policy(self, root: Root) -> None:
        """Make what is stored fit the root's policy. Tightening purges; loosening queues the files for reading."""
        if root.policy == POLICY_NAMES_ONLY:
            for record in self.store.list_files(prefix=root.path, case_insensitive=self.case_insensitive):
                self.store.replace_segments(record.row_id, ())
                self.store.set_pages(record.row_id, ())
                self.store.set_state(record.row_id, FileState.SKIPPED, POLICY_NOTE)
        else:
            for record in self.store.list_files(prefix=root.path, state=FileState.SKIPPED,
                                                case_insensitive=self.case_insensitive):
                if record.error == POLICY_NOTE:
                    self.store.set_state(record.row_id, FileState.PENDING)

    def remove_root(self, path: str, purge: bool = True) -> "int | Failure":
        wanted = self._fold(os.path.abspath(os.path.expanduser(path)))
        match = next((r for r in self.store.list_roots() if self._fold(r.path) == wanted), None)
        if match is None:
            return Failure(FailureReason.OTHER, f"not one of the indexed folders: {path}")
        # purge first: if it is interrupted the folder is still listed, and removing it again finishes the job
        removed = self.store.purge_folder(match.path, self.case_insensitive, thorough=True) if purge else 0
        with self.store.transaction():
            self.store.remove_root(match.path)
        return removed

    # -- status

    def indexing_status(self) -> Mapping[str, object]:
        roots = []
        for root in self.store.list_roots():
            roots.append({"path": root.path, "policy": root.policy, "reachable": root.reachable,
                          "last_scan": root.last_scan, "warnings": list(root.warnings),
                          "include": list(root.include), "exclude": list(root.exclude),
                          "counts": dict(self.store.state_counts(root.path, self.case_insensitive))})
        progress = self._progress
        return {
            "running": self.is_indexing(),
            "files": self.store.file_count(),
            "segments": self.store.segment_count(),
            "counts": dict(self.store.state_counts()),
            "roots": roots,
            "progress": None if progress is None or not self.is_indexing() else {
                "phase": progress.phase, "root": progress.root, "done": progress.done, "total": progress.total,
                "failed": progress.failed, "percent": progress.percent, "rate": progress.rate,
                "remaining_seconds": progress.remaining_seconds},
        }

    def _troubled(self) -> List[FileRecord]:
        """Failed files first, then files read only in part. A file that merely waits for OCR is not trouble."""
        found = list(self.store.list_files(state=FileState.FAILED, missing=False))
        found += [r for r in self.store.list_files(state=FileState.PARTIAL, missing=False)
                  if not only_waits_for_ocr(r.error)]
        return found

    def needs_attention(self, limit: int = 100, offset: int = 0) -> Sequence[FileRecord]:
        return self._troubled()[offset:offset + limit]

    def attention_counts(self) -> Mapping[str, int]:
        """How many files need attention, and how many only wait for OCR (queued, nothing wrong)."""
        partial = self.store.list_files(state=FileState.PARTIAL, missing=False)
        waiting = sum(1 for r in partial if only_waits_for_ocr(r.error))
        failed = len(self.store.list_files(state=FileState.FAILED, missing=False))
        return {"attention": failed + len(partial) - waiting, "waiting_for_ocr": waiting}

    # -- indexing

    def run_indexing(self, stop: Optional[StopSignal] = None, progress: Optional[Callable[[Progress], None]] = None,
                     roots: Optional[Sequence[str]] = None, on_settings_change: str = REFUSE,
                     retry_failed: bool = False) -> "IndexReport | Failure":
        """Index now, in the calling thread, until done or stopped. Only one run at a time, across programs."""
        if not self._run_guard.acquire(blocking=False):
            return Failure(FailureReason.BUSY, "an indexing run is already going on in this program")
        try:
            if not self._lock.acquire():
                return Failure(FailureReason.BUSY, "another program is indexing into this data folder right now")
            try:
                store = self._open_store()   # the writer: its own connection, in this thread
                try:
                    def tell(p: Progress) -> None:
                        self._progress = p
                        if progress is not None:
                            progress(p)

                    indexer = Indexer(store, self.source, self.extractor, self.settings, tell, self.clock,
                                      extra_excludes=(str(self.layout.root),))
                    report = indexer.run(stop, roots, on_settings_change, retry_failed)
                    self.last_report = report
                    return report
                finally:
                    store.close()
            finally:
                self._lock.release()
        finally:
            self._run_guard.release()

    def is_indexing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_indexing(self, mode: str = "on_demand") -> "None | Failure":
        """Index in the background. Every mode does the same work for now; idle and schedule triggers come later."""
        if self.is_indexing():
            return Failure(FailureReason.BUSY, "indexing is already running")
        self._stop = threading.Event()

        def work() -> None:
            try:
                self.last_report = self.run_indexing(self._stop)
            except Exception as exc:   # never die silently in the background
                log.exception("background indexing crashed")
                self.last_report = Failure(FailureReason.CRASHED, f"indexing stopped on an error ({type(exc).__name__}: {exc})")

        self._thread = threading.Thread(target=work, name="dugup-indexing", daemon=True)
        self._thread.start()
        return None

    def stop_indexing(self) -> None:
        """Ask the run to stop after the file it is on, and wait for it. The work is resumable."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._thread = None

    def pause_indexing(self) -> None:
        self.stop_indexing()   # a pause is a stop that keeps everything waiting; start again to go on

    def scan_now(self) -> "None | Failure":
        return self.start_indexing("on_demand")

    def wait_for_indexing(self, timeout: Optional[float] = None) -> bool:
        """True when no background run is going on (any more)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.is_indexing()

    # -- not built yet: each says so

    def find_moved(self, file_id: int) -> "Optional[str] | Failure":
        return Failure(FailureReason.UNSUPPORTED, f"finding a moved file: {NOT_BUILT}")

    def record_usage(self, event: Mapping[str, object]) -> None:
        log.debug("usage event ignored: the usage log is not built yet")

    def doctor(self) -> Mapping[str, object]:
        checks = doctor_module.run_checks(None, self.layout.root)
        return {"ok": not doctor_module.has_failure(checks),
                "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks]}
