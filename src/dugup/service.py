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
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import doctor as doctor_module
from . import models
from .datadir import DataLayout, is_within
from .dirscan import DirectoryScan
from .extractors import default_extractors
from .extractors.sandboxed import SandboxedExtractor
from .indexer import POLICY_NOTE, REFUSE, Indexer, IndexReport, Progress, only_waits_for_ocr
from .lockfile import RunLock
from .ocr import SandboxOcrBackend
from .ocrqueue import OcrReport, OcrRunner
from .filesource import matches_rules
from .protocols import Extractor, FileSource, ScanRules, StopSignal, Store
from .sandbox import SubprocessSandbox
from .settings import Settings
from dataclasses import replace

from .shapes import (LEVEL_NAMES, LEVEL_OCR, LEVELS, level_for, level_reaches, SearchFilters, POLICIES, POLICY_NAMES_ONLY, Box, Evidence, Failure, FailureReason, FileRecord, FileState,
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
                 case_insensitive: Optional[bool] = None, ocr_backend=None, intent=None) -> None:
        self.layout = layout
        self._ocr_backend = ocr_backend      # made on first use: most commands never read a picture
        self._intent = intent
        self._intent_looked = intent is not None
        self.settings = settings or Settings()
        self.source = source or DirectoryScan()
        if extractor is not None:
            self.extractor = extractor
        elif self.settings.isolate_readers:      # the default: files are parsed in a worker process
            self.extractor = SandboxedExtractor(SubprocessSandbox(), self.settings)
        else:
            self.extractor = default_extractors(self.settings)
        self._open_store = open_store or (lambda: SqliteStore(layout.index, near_words=self.settings.near_words,
                                                                compress_text=self.settings.text_compression != "none"))
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

    @property
    def ocr_backend(self):
        """Made on first use. Making it starts no worker; the first picture read does."""
        if self._ocr_backend is None:
            self._ocr_backend = SandboxOcrBackend(SubprocessSandbox(), self.settings)
        return self._ocr_backend

    def old_ocr_pages(self) -> Mapping[str, int]:
        """Pages read by an OCR engine or version other than the one installed now, by engine. Empty when
        there are none, or when no engine is installed to compare with."""
        engine_id = getattr(self.ocr_backend, "engine_id", None)
        current = engine_id() if engine_id is not None else None
        if not current:
            return {}
        return {name: count for name, count in self.store.ocr_engines().items() if name != current}

    def close(self) -> None:
        self.stop_indexing()
        if self._store is not None:
            self._store.close()
            self._store = None
        close_readers = getattr(self.extractor, "close", None)
        if close_readers is not None:
            close_readers()      # ends any worker process and everything it started
        if self._intent is not None:
            self._intent.close()         # ends a model server this program started
        close_ocr = getattr(self._ocr_backend, "close", None)
        if close_ocr is not None:
            close_ocr()

    def __enter__(self) -> "LocalService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def room(self) -> str:
        """Why nothing more may be written to the index, or "" when there is room. Two guards: the drive must
        keep `min_free_mb` free, and the index must stay under the user's spending limit, `max_index_mb`."""
        megabyte = 1024 * 1024
        limit = self.settings.max_index_mb
        if limit and self.layout.index_bytes() > limit * megabyte:
            return (f"the index has reached its spending limit of {limit:,} MB (it is "
                    f"{self.layout.index_bytes() / megabyte:,.0f} MB). Nothing more was written. "
                    "Raise the limit with `dugup space --limit MB`, or index fewer folders")
        floor = self.settings.min_free_mb
        free = self.layout.free_bytes()
        if floor and free is not None and free < floor * megabyte:
            return (f"the drive that holds the index has less than {floor:,} MB free. Nothing more was written. "
                    "Free some space and run the command again")
        return ""

    # -- search

    def search(self, query: str, limit: int = 100, offset: int = 0, sort="rank",
               descending: bool = False, filters: Optional[SearchFilters] = None) -> "SearchPage | Failure":
        if filters is not None and filters.folder is not None and filters.case_insensitive is None:
            filters = replace(filters, case_insensitive=self.case_insensitive)
        try:
            page = self.store.search_words(query, limit, offset, sort, descending, filters)
        except ValueError as exc:
            return Failure(FailureReason.UNSUPPORTED, str(exc))
        away = [r.path for r in self.store.list_roots() if not r.reachable]
        if away:
            for row in page.rows:
                row.unavailable = any(_under(row.path, folder, self._fold) for folder in away)
        return page

    @property
    def intent(self):
        """The plain-words reader, or None when it is not set up. Made on first use; making it starts nothing."""
        if self._intent is None and not self._intent_looked:
            from .intent import from_settings
            self._intent = from_settings(self.settings, models.read_record(self.models_folder))
            self._intent_looked = True
        return self._intent

    # -- the optional model and its runtime

    @property
    def models_folder(self):
        return self.layout.models / "intent"

    def models_plan(self, choice: str = models.SMALL_CHOICE) -> "models.Plan | Failure":
        """What setting up plain-words search would download on this machine, for the small or the larger model."""
        return models.plan_for(self.models_folder, choice=choice)

    def models_installed(self) -> Optional[Mapping[str, str]]:
        return models.read_record(self.models_folder)

    def install_models(self, stop: Optional[StopSignal] = None, progress: Optional[Callable[[Progress], None]] = None,
                       opener=None, plan=None, choice: str = models.SMALL_CHOICE) -> "Mapping[str, str] | Failure":
        """Download and set up the runtime and the model (the only thing in the program that uses the internet).
        Call it only when the person has asked for it, having seen models_plan().describe()."""
        more = {} if opener is None else {"opener": opener}
        if plan is None:
            plan = self.models_plan(choice)
        result = models.install(self.models_folder, progress, stop, plan=plan, free_bytes=self.layout.free_bytes,
                                keep_free=self.settings.min_free_mb * 1024 * 1024, **more)
        if not isinstance(result, Failure):
            self._forget_intent()
        return result

    def remove_models(self) -> int:
        """End a running model server and delete what install_models put in place. Returns the bytes given back."""
        self._forget_intent()
        return models.remove(self.models_folder)

    def _forget_intent(self) -> None:
        if self._intent is not None:
            self._intent.close()
        self._intent, self._intent_looked = None, False

    def reader_named(self, reader: str):
        """A plain-words reader other than the configured one ("rules" or "model"), for trying one out.
        The caller closes it. None when that reader has nothing to run with."""
        import dataclasses
        from .intent import from_settings
        return from_settings(dataclasses.replace(self.settings, intent_reader=reader), models.read_record(self.models_folder))

    def understand(self, text: str):
        """A sentence as search-form fields (intent.Understood), or a Failure saying why not.
        May take seconds, and on first use as long as loading a model takes: call it off the window's thread."""
        reader = self.intent
        if reader is None:
            return Failure(FailureReason.NOT_INSTALLED,
                           "plain-words search is not set up (intent_url, or intent_server and intent_model_file, in config.toml)")
        return reader.understand(text, [r.path for r in self._open_roots()])

    def _open_roots(self):
        """The indexed folders, read on a connection of the calling thread's own."""
        store = self._open_store()
        try:
            return list(store.list_roots())
        finally:
            store.close()

    def more_evidence(self, file_id: int, query: str, filters: Optional[SearchFilters] = None) -> Sequence[Evidence]:
        """Every place in one file where the words were found, in reading order."""
        return list(self.store.file_evidence(file_id, query, filters))

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

    def plan_folder(self, path: str, stop: Optional[StopSignal] = None) -> "Mapping[str, object] | Failure":
        """Look through a folder (no file is opened, nothing is changed) and return the picker's material:
        {"plan": picker.Plan, "rates": survey.Rates, "known": is it already indexed, "fresh": a function that
        gives the starting guesses again}. For an indexed folder the plan shows its current choices.
        Safe to call off the window's thread: it reads the index on a connection of its own."""
        from . import picker, survey as surveying
        target = os.path.abspath(os.path.expanduser(path))
        store = self._open_store()
        try:
            roots = list(store.list_roots())
            counts = dict(store.state_counts())
            rates = surveying.learn_rates(self.layout.index_bytes(), store.file_count(),
                                          int(counts.get(FileState.DONE.value, 0)), store.segment_count())
        finally:
            store.close()
        known = next((r for r in roots if self._fold(r.path) == self._fold(target)), None)
        if known is None:
            for other in roots:
                if _under(target, other.path, self._fold) or _under(other.path, target, self._fold):
                    return Failure(FailureReason.UNSUPPORTED,
                                   f"that folder overlaps a folder that is already indexed ({other.path}). "
                                   "Choose what to index inside that one instead")
        target = known.path if known else target
        own_data = (str(self.layout.root),)
        base = target.rstrip("/\\")
        extra = tuple(f[len(base) + 1:].replace("\\", "/") for f in own_data if f.startswith(base + "/") or f.startswith(base + "\\"))
        listing = self.source.scan(target, ScanRules((), extra), stop)
        if isinstance(listing, Failure):
            return listing
        tree = picker.build(target, ((e.path, e.size, e.offline) for e in listing.entries), self.settings.extensions)
        settings = self.settings
        plan = picker.plan_from_root(tree, known) if known else picker.plan_from_guess(tree, settings)
        return {"plan": plan, "rates": rates, "known": known is not None,
                "fresh": lambda: picker.plan_from_guess(tree, settings)}

    def apply_plan(self, plan) -> "Root | Failure":
        """Take the picker's choices: add the folder or change it. What is now left out leaves the index at
        once; what is now only listed by name loses its text at once; the rest is read by the next run."""
        return self.set_root(plan.to_root(), keep_levels=False)

    def set_root(self, root: Root, keep_levels: bool = True) -> "Root | Failure":
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
        # a change of rules or policy alone keeps the levels; the picker states them all, so there they are replaced
        levels = tuple(root.levels) if root.levels or old is None or not keep_levels else old.levels
        wanted = Root(path, root.policy, tuple(root.include), tuple(root.exclude), levels=levels)
        with self.store.transaction():
            self.store.set_root(wanted)
            if old is not None and (old.policy != wanted.policy or old.levels != wanted.levels):
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

    def survey(self, path: str, stop: Optional[StopSignal] = None):
        """What a folder holds, one row for each folder directly below it, and what indexing it would cost
        (survey.Survey, or a Failure). Nothing is opened, indexed or changed. Rules of a folder that is
        already indexed are NOT applied: the point is to see everything, skipped or not."""
        from . import survey as surveying
        target = os.path.abspath(os.path.expanduser(path))
        listing = self.source.scan(target, ScanRules(), stop)
        if isinstance(listing, Failure):
            return listing
        counts = dict(self.store.state_counts())
        rates = surveying.learn_rates(self.layout.index_bytes(), self.store.file_count(),
                                      int(counts.get(FileState.DONE.value, 0)), self.store.segment_count())
        return surveying.survey(target, ((e.path, e.size, e.offline) for e in listing.entries),
                                self.settings.extensions, rates)

    def _apply_policy(self, root: Root, below: Optional[str] = None) -> Tuple[int, int]:
        """Make what is stored fit the level of every file below a folder (the whole root when `below` is None).
        Where the level is now "names", the text is removed; where it is no longer, the files wait to be read.
        Returns (files whose text was removed, files put in the queue)."""
        purged = queued = 0
        for record in self.store.list_files(prefix=below or root.path, case_insensitive=self.case_insensitive):
            names_only = level_for(root, record.path, self.case_insensitive) == LEVEL_NAMES
            held = record.state is FileState.SKIPPED and record.error == POLICY_NOTE
            if names_only and not held:
                self.store.replace_segments(record.row_id, ())
                self.store.set_pages(record.row_id, ())
                self.store.set_state(record.row_id, FileState.SKIPPED, POLICY_NOTE)
                purged += 1
            elif held and not names_only:
                self.store.set_state(record.row_id, FileState.PENDING)
                queued += 1
        return purged, queued

    def set_level(self, path: str, level: Optional[str]) -> "Mapping[str, object] | Failure":
        """Say how far indexing goes in one folder: a root or any folder inside one (shapes.LEVELS).
        None takes the folder's own level away, so it follows the folder above it again.
        Lowering to "names" removes the text kept for that folder now; raising from it queues the files
        for the next `index` run. "words" keeps OCR text that was already read and reads no more.
        Returns {"root", "folder", "level", "purged", "queued"}."""
        if level is not None and level not in LEVELS:
            return Failure(FailureReason.UNSUPPORTED, f"unknown level {level!r}; choose one of {', '.join(LEVELS)}")
        wanted = os.path.abspath(os.path.expanduser(path))
        root = next((r for r in self.store.list_roots()
                     if self._fold(r.path) == self._fold(wanted) or _under(wanted, r.path, self._fold)), None)
        if root is None:
            return Failure(FailureReason.OTHER, f"that folder is not inside an indexed folder: {wanted}. "
                                                "Add it (or a folder above it) first: dugup roots add")
        folder = wanted[len(root.path.rstrip("/\\")) + 1:].replace("\\", "/").strip("/")
        kept = [(f, l) for f, l in root.levels if self._fold(f) != self._fold(folder)]
        if level is not None:
            kept.append((folder, level))
        changed = replace(root, levels=tuple(sorted(kept)))
        with self.store.transaction():
            self.store.set_root(changed)
            purged, queued = self._apply_policy(changed, wanted)
        return {"root": root.path, "folder": folder, "level": level_for(changed, os.path.join(wanted, "x"), self.case_insensitive),
                "purged": purged, "queued": queued}

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
                          "levels": [list(pair) for pair in root.levels], "base_level": root.base_level,
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
        """Index now, in the calling thread, until done or stopped. Only one run at a time, across programs.
        A run that finishes also compacts the index when enough of it is unused space."""
        return self._tidy(self._run_indexing(stop, progress, roots, on_settings_change, retry_failed))

    def _run_indexing(self, stop, progress, roots, on_settings_change, retry_failed) -> "IndexReport | Failure":
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
                                      extra_excludes=(str(self.layout.root),), room=self.room,
                                      size_now=self.layout.index_bytes, case_insensitive=self.case_insensitive)
                    report = indexer.run(stop, roots, on_settings_change, retry_failed)
                    self.last_report = report
                    return report
                finally:
                    store.close()
            finally:
                self._lock.release()
        finally:
            self._run_guard.release()

    def ocr_waiting(self) -> int:
        """Pages and pictures that wait for OCR in folders whose level asks for it (what an OCR run would take up)."""
        roots = list(self.store.list_roots())
        count = 0
        for task in self.store.ocr_queue():
            owner = next((r for r in roots if _under(task.path, r.path, self._fold)), None)
            if owner is None or level_reaches(level_for(owner, task.path, self.case_insensitive), LEVEL_OCR):
                count += 1
        return count

    def fast_readers(self) -> int:
        """How many readers fast mode would use on this machine now."""
        from . import machine
        s = self.settings
        return machine.readers_for(s.ocr_fast_readers, s.ocr_reader_threads, s.ocr_reader_memory_mb,
                                   free_bytes=machine.free_memory_bytes())

    def look_at_machine(self, stop: Optional[StopSignal] = None, test_card: bool = True):
        """What this computer has (catalogue.Machine): cores, free memory, free disk, whether the picture reader
        can use a graphics card and, when `test_card` is on, how much faster the card read a sample page.
        The test takes some seconds and loads the reader twice; run it off the window's thread."""
        from . import machine
        from .catalogue import Machine
        backend = self.ocr_backend
        card, speedup, page_seconds = "", None, None
        if test_card and hasattr(backend, "speed_test"):
            timed = backend.speed_test(stop)
            if isinstance(timed, dict):
                card = str(timed.get("card_name") or "")
                slow, quick = timed.get("processor"), timed.get("card")
                page_seconds = float(slow) if slow else None
                if card and slow and quick and quick > 0:
                    speedup = float(slow) / float(quick)
        elif hasattr(backend, "card"):
            card = backend.card(stop)
        return Machine(machine.cores(), machine.free_memory_bytes(), self.layout.free_bytes(), card, speedup, page_seconds)

    def apply_settings(self, changes: Mapping[str, object]) -> "Settings | Failure":
        """Check these settings, save them in config.toml and use them from now on. Nothing is saved when one
        of them will not do. Workers started under the old settings are ended, so the new ones take effect."""
        from . import catalogue
        new = catalogue.apply(self.settings, changes)
        if isinstance(new, Failure):
            return new
        catalogue.write(self.layout.config, {name: getattr(new, name) for name in changes})
        self.settings = new
        self._forget_intent()
        close = getattr(self._ocr_backend, "close", None)
        if close is not None and isinstance(self._ocr_backend, SandboxOcrBackend):
            close()
            self._ocr_backend = None
        return new

    def card_readers(self) -> int:
        """How many readers share the graphics card in fast mode. Each still does its drawing and tidying on
        the processor, so the count follows the cores, and eight is as far as it has been measured."""
        return self.settings.ocr_gpu_readers or min(8, self.fast_readers())

    def _more_readers(self, count: int, gpu: bool = False) -> list:
        """`count` more OCR readers, each with its own worker. The caller closes them."""
        return [SandboxOcrBackend(SubprocessSandbox(), self.settings, threads=self.settings.ocr_reader_threads, gpu=gpu)
                for _ in range(max(0, count))]

    def run_ocr(self, stop: Optional[StopSignal] = None, progress: Optional[Callable[[Progress], None]] = None,
                roots: Optional[Sequence[str]] = None, budget_seconds: Optional[float] = None,
                retry_failed: bool = False, reread_old: bool = False, folder: Optional[str] = None,
                fast: bool = False, again: bool = False, gpu: Optional[bool] = None) -> "OcrReport | Failure":
        """Read the pages and pictures that wait for OCR, in the calling thread, until done or stopped.
        It writes to the index, so it takes the same lock as an indexing run: one or the other, never both.

        `folder`: only what lies in this folder (an indexed folder or any folder inside one).
        `fast`: several readers side by side (see fast_readers); the machine is busier while it runs.
        `again`: with `folder`, read its pictures and scans afresh even where that was done before.
        `gpu`: read on a graphics card (None: as the setting ocr_gpu says). Where the OCR runtime cannot use
        one, the processor is used and the report says so."""
        return self._tidy(self._run_ocr(stop, progress, roots, budget_seconds, retry_failed, reread_old, folder, fast,
                                        again, gpu))

    def _run_ocr(self, stop, progress, roots, budget_seconds, retry_failed, reread_old, folder=None, fast=False,
                 again=False, gpu=None) -> "OcrReport | Failure":
        gpu = self.settings.ocr_gpu if gpu is None else bool(gpu)
        if folder is not None:
            folder = os.path.abspath(os.path.expanduser(folder))
        if again and folder is None:
            return Failure(FailureReason.UNSUPPORTED, "reading again needs a folder to be named")
        if not self._run_guard.acquire(blocking=False):
            return Failure(FailureReason.BUSY, "indexing or OCR is already going on in this program")
        try:
            if not self._lock.acquire():
                return Failure(FailureReason.BUSY, "another program is writing to this data folder right now")
            try:
                store = self._open_store()
                try:
                    known = [r.path for r in store.list_roots()]
                    wanted = known if roots is None else [r for r in known if any(self._fold(r) == self._fold(w) for w in roots)]
                    if folder is not None:
                        if not any(_under(folder, r, self._fold) for r in known):
                            return Failure(FailureReason.OTHER, f"that folder is not inside an indexed folder: {folder}")
                        wanted = [folder]
                    here = [r for r in wanted if self.source.reachable(r)]
                    if again and here:
                        # no engine has this name, so every page below the folder counts as read by another one
                        rows = store.requeue_ocr_not_by("(read again)", folder, self.case_insensitive)
                        log.info("ocr again: %d file(s) put back in the queue", len(rows))

                    def tell(p: Progress) -> None:
                        self._progress = p
                        if progress is not None:
                            progress(p)

                    all_roots = list(store.list_roots())

                    def asks_for_ocr(file_path: str) -> bool:
                        owner = next((r for r in all_roots if _under(file_path, r.path, self._fold)), None)
                        return owner is None or level_reaches(level_for(owner, file_path, self.case_insensitive), LEVEL_OCR)

                    # in fast mode every reader is one of the limited ones: the usual reader lets its engine take
                    # every core, and one such reader beside the others would crowd them all (seen on Windows)
                    # On a graphics card a few readers are enough: the card does the heavy part one page after another.
                    if gpu:
                        extra = self._more_readers(self.card_readers() if fast else 1, gpu=True)
                    else:
                        extra = self._more_readers(self.fast_readers()) if fast else []
                    lead = extra.pop(0) if extra else self.ocr_backend
                    try:
                        runner = OcrRunner(store, lead, self.settings, tell,
                                           case_insensitive=self.case_insensitive, room=self.room, wanted=asks_for_ocr,
                                           extra_backends=extra)
                        report = runner.run(stop, here, budget_seconds, retry_failed, reread_old)
                        report.gpu_asked = gpu
                        report.device = next((d for d in (getattr(r, "last_device", "") for r in [lead] + extra) if d), "")
                    finally:
                        for reader in extra + ([lead] if lead is not self.ocr_backend else []):
                            close = getattr(reader, "close", None)
                            if close is not None:
                                close()
                    report.away = tuple(r for r in wanted if r not in here)
                    return report
                finally:
                    store.close()
            finally:
                self._lock.release()
        finally:
            self._run_guard.release()

    def compact_if_worthwhile(self) -> Optional[Mapping[str, int]]:
        """Compact when enough of the index is unused space to be worth rewriting the file (two settings).
        None when it was not worth it, is turned off, or could not be done (no room for the copy, or busy)."""
        share = self.settings.compact_at_fraction
        if share <= 0:
            return None
        try:
            unused = self.store.reclaimable_fraction()
        except Exception:
            return None
        megabytes = unused * self.layout.index_bytes() / (1024 * 1024)
        if unused < share or megabytes < self.settings.compact_min_mb:
            return None
        result = self.compact()
        if isinstance(result, Failure):
            log.info("index not compacted: %s", result.reason.value)
            return None
        return dict(result)

    def _tidy(self, report):
        """After a run that finished: give back unused space when there is enough of it."""
        if not isinstance(report, Failure) and not report.stopped and not report.paused \
                and not getattr(report, "settings_changed", ()) and self._store_thread in (None, threading.get_ident()):
            report.compacted = self.compact_if_worthwhile()
        return report

    def compact(self) -> "Mapping[str, int] | Failure":
        """Give back the space that removed and rewritten text left in the index. Returns its size before
        and after. It writes, so it takes the run lock; it needs room for a full copy while it works."""
        if not self._run_guard.acquire(blocking=False):
            return Failure(FailureReason.BUSY, "indexing or OCR is going on in this program")
        try:
            if not self._lock.acquire():
                return Failure(FailureReason.BUSY, "another program is writing to this data folder right now")
            try:
                before = self.layout.index_bytes()
                free = self.layout.free_bytes()
                floor = self.settings.min_free_mb * 1024 * 1024
                if free is not None and free < before + floor:
                    return Failure(FailureReason.OTHER,
                                   f"compacting needs room for a full copy of the index while it works "
                                   f"(about {before / 1048576:,.0f} MB) and the drive has {free / 1048576:,.0f} MB free")
                if self._store is not None:      # one connection only while the file is rebuilt
                    self._store.close()
                    self._store = None
                store = self._open_store()
                try:
                    store.compact()
                finally:
                    store.close()
                return {"before": before, "after": self.layout.index_bytes()}
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
