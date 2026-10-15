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
import datetime
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
from .indexer import POLICY_NOTE, REFUSE, Indexer, IndexReport, Progress, cannot_be_read, only_waits_for_ocr
from .lockfile import RunLock
from .ocr import SandboxOcrBackend
from .ocrqueue import OcrReport, OcrRunner
from .filesource import matches_rules
from .protocols import Extractor, FileSource, Limits, ScanRules, StopSignal, Store
from .sandbox import SubprocessSandbox
from .settings import Settings
from dataclasses import replace

from .shapes import (LEVEL_NAMES, LEVEL_OCR, LEVELS, OPTION_ALIKE, OPTION_MEANING, OPTION_WORDS, WHERE_BOTH, WHERE_LOOKS, WHERE_MEANING, level_for, level_reaches, level_words, normalize_level, wants, SearchFilters, POLICIES, POLICY_NAMES_ONLY, Box, Evidence, Failure, FailureReason, FileKind, FileRecord, FileState,
                     Passage, Root, SearchPage)
from .store import SqliteStore
from .suggest import Suggestion, suggest_excludes

log = logging.getLogger("dugup.service")
NOT_BUILT = "this part of the program is not built yet"


def _under(path: str, folder: str, fold: Callable[[str], str]) -> bool:
    base = fold(folder.rstrip("/\\"))
    path = fold(path)
    return path == base or path.startswith(base + "/") or path.startswith(base + "\\")


def _place(path: str) -> str:
    """A path as given, made absolute; a Windows drive path is left as it is wherever this runs."""
    path = os.path.expanduser(path.strip())
    if len(path) >= 2 and path[0].isalpha() and path[1] == ":":
        return path
    return os.path.abspath(path)


def main_place(settings: Settings, home: Optional[str] = None) -> str:
    """The folder or drive whose copy of a file a search shows first: the setting, or on Windows the drive
    the person's own folders are on ("C:\\"). Elsewhere there is no such thing as a drive, so none."""
    if settings.main_place.strip():
        return settings.main_place.strip()
    home = home if home is not None else os.path.expanduser("~")
    return home[:2] + "\\" if len(home) >= 2 and home[0].isalpha() and home[1] == ":" else ""


class LocalService:
    def __init__(self, layout: DataLayout, settings: Optional[Settings] = None,
                 source: Optional[FileSource] = None, extractor: Optional[Extractor] = None,
                 open_store: Optional[Callable[[], Store]] = None, clock: Callable[[], float] = time.time,
                 case_insensitive: Optional[bool] = None, ocr_backend=None, intent=None) -> None:
        self.layout = layout
        self._ocr_backend = ocr_backend      # made on first use: most commands never read a picture
        self._intent = intent
        self._asker = None               # the worker that sketches questions for searching by meaning, kept once made
        self._asker_lock = threading.Lock()   # one question at a time; any thread may ask
        self._looker = None              # the worker that reads sentences and example pictures for likeness, kept once made
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
                                                                compress_text=self.settings.text_compression != "none",
                                                                prefer_under=main_place(self.settings)))
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
        if self._asker is not None:
            self._asker[0].close()
            self._asker = None
        if self._looker is not None:
            self._looker[0].close()
            self._looker = None
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

    def _completed(self, filters: Optional[SearchFilters]) -> Optional[SearchFilters]:
        """Filters with what only the service knows: how paths compare here, and which folders are away."""
        if filters is None:
            return None
        if filters.case_insensitive is None and (filters.folder is not None or filters.present_only):
            filters = replace(filters, case_insensitive=self.case_insensitive)
        if filters.present_only:
            filters = replace(filters, away=tuple(r.path for r in self.store.list_roots() if not r.reachable))
        return filters

    def search(self, query: str, limit: int = 100, offset: int = 0, sort="rank",
               descending: bool = False, filters: Optional[SearchFilters] = None, question=None) -> "SearchPage | Failure":
        """`question`: the sketch of `query` already made (sketch_question), for a search by meaning whose
        sketching was done on another thread; None makes it here."""
        filters = self._completed(filters)
        try:
            if filters is not None and filters.where == WHERE_LOOKS:
                if question is None:
                    question = self.sketch_sentence(query)
                if isinstance(question, Failure):
                    return question
                page = self.store.search_looks(question, self.looks_model(), limit, offset, filters, sort=sort, descending=descending)
            elif filters is not None and filters.where in (WHERE_MEANING, WHERE_BOTH):
                if question is None:
                    question = self.sketch_question(query)
                if isinstance(question, Failure):
                    return question
                if filters.where == WHERE_BOTH:
                    page = self.store.search_both(query, question, self.meaning_model(), limit, offset, filters,
                                                  self.settings.meaning_word_weight, self.settings.meaning_word_pool,
                                                  self.settings.meaning_word_everywhere, sort, descending)
                else:
                    page = self.store.search_meaning(question, self.meaning_model(), limit, offset, filters,
                                                     sort=sort, descending=descending)
            else:
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

    def free_models(self) -> List[str]:
        """End every kept model worker (the question sketcher, the picture looker, the plain-words server, the
        OCR reader), giving back their memory and the graphics card. Nothing is lost: the next search by
        meaning or by looks starts one again (a few seconds). Returns what was ended, in words."""
        ended: List[str] = []
        with self._asker_lock:
            if self._asker is not None:
                self._asker[0].close()
                self._asker = None
                ended.append("the question model (searching by meaning)")
        if self._looker is not None:
            self._looker[0].close()
            self._looker = None
            ended.append("the picture model (pictures by likeness)")
        backend = getattr(self._intent, "backend", None)
        if backend is not None and getattr(backend, "_running", None) is not None:
            self._intent.close()          # ends the server; the parser stays and starts it again on next use
            ended.append("the plain-words model server")
        close_ocr = getattr(self._ocr_backend, "close", None)
        if self._ocr_backend is not None and close_ocr is not None:
            close_ocr()
            self._ocr_backend = None
            ended.append("the OCR reader")
        return ended

    def disk_usage(self) -> Dict[str, int]:
        """Bytes on disk: the index, the models, the records (removed and renames), the rest of the data
        folder, and the free space on that drive."""
        import shutil

        def size_of(folder: Path) -> int:
            total = 0
            for path in folder.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except OSError:
                    pass
            return total

        root = self.layout.root
        index = self.layout.index_bytes()
        models_bytes = size_of(self.layout.models) if self.layout.models.exists() else 0
        records = sum(size_of(root / name) for name in ("removed", "renames") if (root / name).exists())
        everything = size_of(root) if root.exists() else 0
        try:
            free = shutil.disk_usage(str(root)).free
        except OSError:
            free = -1
        return {"index": index, "models": models_bytes, "records": records,
                "other": max(0, everything - index - models_bytes - records), "total": everything, "free": free}

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

    def _lister(self):
        """A way to list a folder for looking only. The index wants each file's id, which on Windows costs an
        extra question per file (a whole drive took four times as long with them); a look does not."""
        from .dirscan import DirectoryScan
        source = self.source
        if isinstance(source, DirectoryScan) and source.collect_ids:
            source = DirectoryScan(collect_ids=False, scandir=source._scandir, stat=source._stat)
        self._looking = source
        return source

    def looking_at(self) -> Mapping[str, object]:
        """How far a look through a folder has got: {"files": seen so far, "sorting": True once the listing is
        complete and is being sorted into folders}. Safe to ask from the window's thread while the look runs."""
        now = getattr(self, "_looking", None)
        if isinstance(now, dict):
            return now
        stats = getattr(now, "last_stats", None) or {}
        return {"files": int(stats.get("files", 0)), "sorting": False}

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
        listing = self._lister().scan(target, ScanRules((), extra), stop)
        if isinstance(listing, Failure):
            return listing
        self._looking = {"files": len(listing.entries), "sorting": True}
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
        listing = self._lister().scan(target, ScanRules(), stop)
        if isinstance(listing, Failure):
            return listing
        counts = dict(self.store.state_counts())
        rates = surveying.learn_rates(self.layout.index_bytes(), self.store.file_count(),
                                      int(counts.get(FileState.DONE.value, 0)), self.store.segment_count())
        return surveying.survey(target, ((e.path, e.size, e.offline) for e in listing.entries),
                                self.settings.extensions, rates)

    def duplicates(self, drive: str, protected: Sequence[str] = (), preferred: Sequence[str] = ()):
        """The same file kept in several places below `drive`, from the content marks in the index (cleanup.Report).
        Changes nothing. `protected`: folders never proposed, and `preferred`: folders whose copy is the one
        to keep, both on top of the settings cleanup_protect and cleanup_prefer."""
        from . import cleanup
        kept = [p.strip() for p in self.settings.cleanup_protect.split(";") if p.strip()] + [p for p in protected if p.strip()]
        home = [p.strip() for p in self.settings.cleanup_prefer.split(";") if p.strip()] + [p for p in preferred if p.strip()]
        return cleanup.report(self.store, _place(drive), [_place(p) for p in kept], self.case_insensitive,
                              [_place(p) for p in home])

    def remove_duplicates(self, found, stop: Optional[StopSignal] = None, tell=None, mover=None, journal=None):
        """Move the report's "extra" copies to the trash (cleanup.remove_extras; trash.to_trash). The one thing
        in DugUp that changes a file, and it only ever moves an exact copy whose keeper is there now."""
        from . import cleanup, trash
        import time as clock

        def gone(path: str) -> None:
            record = self.store.get_file(path)
            if record is not None and record.row_id is not None:
                self.store.set_missing(record.row_id, clock.time())

        return cleanup.remove_extras(found, mover or trash.to_trash, gone, stop, tell, journal)

    def measure_meaning(self, pieces: int = 2000, choices: Sequence[str] = ("small", "larger"), gpu: Optional[bool] = None,
                        questions: Sequence[str] = (), progress: Optional[Callable[[Progress], None]] = None,
                        stop: Optional[StopSignal] = None):
        """Measure searching by meaning on a sample of this index (meaning.measure). Fetches the models on
        first use into the data folder. Stores nothing in the index."""
        from . import meaning
        from .sandbox import SubprocessSandbox
        return meaning.measure(self.store, SubprocessSandbox, self.layout.models / "meaning", choices, pieces, gpu,
                               questions, progress=progress, stop=stop)

    # -- pictures by likeness

    def looks_model(self) -> str:
        from . import looks
        return looks.model_name("base")

    def looks_coverage(self) -> Tuple[int, int, int]:
        """(pictures looked at, pictures there are, of those looked at the ones that could not be read)."""
        return self.store.look_counts(self.looks_model())

    def looks_waiting(self) -> int:
        done, could, _ = self.looks_coverage()
        return max(0, could - done)

    def looks_ready(self) -> "bool | str":
        """True when pictures can be looked at now; otherwise a sentence saying what is missing."""
        from . import embedding_models
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
            import PIL  # noqa: F401
        except ImportError:
            return "the runtime for pictures by likeness is not installed (pip install dugup[meaning] and pillow)"
        if not embedding_models.looks_present(self.layout.models / "looks"):
            size = sum(i.size for i in embedding_models.LOOKS_BY_CHOICE["base"]) / 1048576
            return f"the picture model is not fetched yet ({size:,.0f} MB); `dugup alike fill` fetches it"
        return True

    def looks_readers(self) -> int:
        import os as _os
        return self.settings.looks_readers or max(1, min(8, (_os.cpu_count() or 2) // 2))

    def run_looks(self, stop: Optional[StopSignal] = None, progress: Optional[Callable[[Progress], None]] = None,
                  budget_seconds: Optional[float] = None, gpu: Optional[bool] = None, readers: Optional[int] = None):
        """Make sketches of the pictures that have none (looks.fill), several readers side by side, until done,
        stopped or out of time. Fetches the picture model on first use. Takes the same lock as indexing.
        Pictures in folders whose level leaves out "alike" are left alone. A looks.FillReport, or a Failure."""
        from . import embedding_models, looks
        from .sandbox import SubprocessSandbox
        gpu = self.settings.looks_gpu if gpu is None else bool(gpu)
        count = max(1, readers or self.looks_readers())
        if not self._run_guard.acquire(blocking=False):
            return Failure(FailureReason.BUSY, "indexing or OCR is already going on in this program")
        try:
            if not self._lock.acquire():
                return Failure(FailureReason.BUSY, "another program is writing to this data folder right now")
            try:
                folder = self.layout.models / "looks"
                if not embedding_models.looks_present(folder):
                    fetched = looks.fetch_models(folder, "base", progress, stop)
                    if isinstance(fetched, Failure):
                        return fetched
                store = self._open_store()
                try:
                    roots = list(store.list_roots())

                    def asks_for_likeness(file_path: str) -> bool:
                        owner = next((r for r in roots if _under(file_path, r.path, self._fold)), None)
                        return owner is None or wants(level_for(owner, file_path, self.case_insensitive), OPTION_ALIKE)

                    def tell(p: Progress) -> None:
                        self._progress = p
                        if progress is not None:
                            progress(p)

                    sandboxes = [SubprocessSandbox() for _ in range(count)]
                    try:
                        runners = [looks._Runner(box, folder, gpu, threads=2 if count > 1 else 0) for box in sandboxes]
                        report = looks.fill(store, runners, asks_for_likeness, tell, stop, budget_seconds, room=self.room)
                    finally:
                        for box in sandboxes:
                            box.close()
                    if report.failure is None and not report.stopped and not report.out_of_time and not report.paused \
                            and self._store_thread in (None, threading.get_ident()):
                        report.compacted = self.compact_if_worthwhile()
                    return report
                finally:
                    store.close()
            finally:
                self._lock.release()
        finally:
            self._run_guard.release()

    def alike_pictures(self, threshold: float = 0.97, protected: Sequence[str] = (), preferred: Sequence[str] = (),
                       stop: Optional[StopSignal] = None, progress: Optional[Callable[[int, int], None]] = None):
        """Sets of pictures that look nearly the same (alikesets.report), with a keeper each. Changes nothing.
        `protected` and `preferred` come on top of cleanup_protect and cleanup_prefer."""
        from . import alikesets
        kept = [p.strip() for p in self.settings.cleanup_protect.split(";") if p.strip()] + [p for p in protected if p.strip()]
        liked = [p.strip() for p in self.settings.cleanup_prefer.split(";") if p.strip()] + [p for p in preferred if p.strip()]
        store = self._open_store() if threading.get_ident() != self._store_thread and self._store is not None else self.store
        try:
            return alikesets.report(store, self.looks_model(), threshold, [_place(p) for p in kept], [_place(p) for p in liked],
                                    self._fold, stop, progress)
        finally:
            if store is not self._store:
                store.close()

    def sketch_sentence(self, sentence: str):
        """A sketch of a sentence in the pictures' space, from a kept worker on the processor; or a Failure."""
        from . import looks
        ready = self.looks_ready()
        if ready is not True:
            return Failure(FailureReason.NOT_INSTALLED, ready)
        with self._asker_lock:
            if self._looker is None:
                from .sandbox import SubprocessSandbox
                sandbox = SubprocessSandbox()
                self._looker = (sandbox, looks._Runner(sandbox, self.layout.models / "looks", False))
            vectors = self._looker[1].read([sentence], None, looks.LOAD_SECONDS)
            if isinstance(vectors, Failure):
                self._looker[0].close()
                self._looker = None
                return vectors
        return vectors[0]

    def search_like(self, file_id: int, limit: int = 100, offset: int = 0, filters: Optional[SearchFilters] = None,
                    sort="rank", descending: bool = False) -> "SearchPage | Failure":
        """Pictures that look like one in the index (by its row id), closest first, the picture itself left out."""
        example = self.store.look_of(file_id)
        if example is None:
            return Failure(FailureReason.OTHER, "that picture has no sketch yet (dugup alike fill makes them)")
        filters = self._completed(filters)
        try:
            return self.store.search_looks(example, self.looks_model(), limit, offset, filters, sort=sort,
                                           descending=descending, not_file=file_id)
        except ValueError as exc:
            return Failure(FailureReason.UNSUPPORTED, str(exc))

    def search_like_file(self, path: str, limit: int = 100, offset: int = 0, filters: Optional[SearchFilters] = None):
        """Pictures that look like a picture file, indexed or not: it is read and sketched now."""
        from . import looks
        path = os.path.abspath(os.path.expanduser(path))
        record = self.store.get_file(path)
        if record is not None and record.row_id is not None and self.store.look_of(record.row_id) is not None:
            return self.search_like(record.row_id, limit, offset, filters)
        ready = self.looks_ready()
        if ready is not True:
            return Failure(FailureReason.NOT_INSTALLED, ready)
        with self._asker_lock:
            if self._looker is None:
                from .sandbox import SubprocessSandbox
                sandbox = SubprocessSandbox()
                self._looker = (sandbox, looks._Runner(sandbox, self.layout.models / "looks", False))
            made = self._looker[1].look([path], None, looks.LOAD_SECONDS)
        if isinstance(made, Failure):
            return made
        vectors, failed = made
        if failed:
            return Failure(FailureReason.OTHER, f"the example could not be read: {failed[0]}")
        if filters is not None and filters.folder is not None and filters.case_insensitive is None:
            filters = replace(filters, case_insensitive=self.case_insensitive)
        return self.store.search_looks(vectors[0], self.looks_model(), limit, offset, filters,
                                       not_file=record.row_id if record is not None else None)

    # -- the trash

    def to_trash(self, paths: Sequence[str], journal: Optional[Callable[[str], None]] = None, mover=None):
        """Move these files to the trash (trash.to_trash), one at a time: never a file in a protected folder
        (cleanup_protect), never one that is not there. The index marks each one gone. Returns
        cleanup.Removal: moved (path, "") and skipped (path, why). `journal(line)` is told of each as it goes."""
        from . import cleanup, trash
        protected = [p.strip() for p in self.settings.cleanup_protect.split(";") if p.strip()]
        done = cleanup.Removal()
        for path in paths:
            why = ""
            if any(_under(path, folder, self._fold) for folder in protected):
                why = "it is in a protected folder"
            elif not os.path.isfile(path):
                why = "it is not there now"
            else:
                why = (mover or trash.to_trash)(path)
            if why:
                done.skipped.append((path, why))
                if journal is not None:
                    journal(cleanup.record_line("left", path, why))
                continue
            done.moved.append((path, ""))
            record = self.store.get_file(path)
            if record is not None and record.row_id is not None:
                self.store.set_missing(record.row_id, time.time())
            if journal is not None:
                journal(cleanup.record_line("trashed", path))
        return done

    def rekeep_alike(self, same, keeper_row_id: int):
        """A near-duplicate set with another keeper (alikesets.rekeep): closeness measured against it afresh."""
        from . import alikesets
        return alikesets.rekeep(self.store, same, keeper_row_id)

    def trash_alike_extras(self, sets, journal: Optional[Callable[[str], None]] = None, mover=None):
        """Move the extras of these near-duplicate sets to the trash, set by set, checking at that moment what
        the preview assumed: the keeper is still there and is the file it was (size and time), each extra is
        the file it was, and the protections as they stand now. A set whose keeper is gone or changed is left
        whole, with the reason; an extra that changed is left, with the reason. Returns cleanup.Removal."""
        from . import cleanup
        done = cleanup.Removal()

        def unchanged(record) -> str:
            try:
                st = os.stat(record.path)
            except OSError:
                return "it is not there now"
            if st.st_size != record.size or abs(st.st_mtime - record.modified) > 1.0:
                return "it changed since the preview (size or time)"
            return ""

        for same in sets:
            keeper = same.keep.record
            why = unchanged(keeper)
            if why:
                for m in same.extra:
                    done.skipped.append((m.record.path, f"its keeper {os.path.basename(keeper.path)}: {why}"))
                    if journal is not None:
                        journal(cleanup.record_line("left", m.record.path, f"keeper {why}"))
                continue
            ready, held = [], []
            for m in same.extra:
                if m.closeness < same.threshold:
                    held.append((m.record.path, "it is below the threshold against the keeper"))
                else:
                    problem = unchanged(m.record)
                    (held.append((m.record.path, problem)) if problem else ready.append(m.record.path))
            for path, problem in held:
                done.skipped.append((path, problem))
                if journal is not None:
                    journal(cleanup.record_line("left", path, problem))
            if journal is not None and ready:
                journal(cleanup.record_line("keeping", keeper.path))
            part = self.to_trash(ready, journal, mover)      # the protections as they stand now are checked here
            done.moved.extend(part.moved)
            done.skipped.extend(part.skipped)
        return done

    # -- naming pictures

    def captions_ready(self) -> "bool | str":
        from . import embedding_models
        if self.settings.caption_source == "server":
            return "captions from a local model server are not built yet; set caption_source to \"model\""
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
            import PIL  # noqa: F401
        except ImportError:
            return "the runtime for captions is not installed (pip install dugup[meaning] and pillow)"
        if not embedding_models.captions_present(self.layout.models / "captions"):
            size = sum(i.size for i in embedding_models.CAPTIONS_BY_CHOICE["base"]) / 1048576
            return f"the captioning model is not fetched yet ({size:,.0f} MB)"
        return True

    def plan_names(self, folder: Optional[str] = None, pattern: Optional[str] = None, caption: bool = True, longer: bool = False,
                   progress: Optional[Callable[[Progress], None]] = None, stop: Optional[StopSignal] = None,
                   gpu: Optional[bool] = None, every: bool = False, paths: Optional[Sequence[str]] = None) -> "naming.Plan | Failure":
        """What the pictures below a folder, or the pictures at `paths` (a window's results, say), would be
        called (naming.plan). Captions are written now, by the
        captioning model in a worker (fetched on first use), when the pattern asks for {caption} and `caption`
        is on. Only pictures whose name is a camera's (IMG_..., 20220401_142335, Screenshot ...) unless `every`;
        the others are left alone, and no caption is written for them. Changes nothing."""
        import time as _time
        from . import embedding_models, naming
        pattern = pattern if pattern is not None else self.settings.rename_pattern
        problem = naming.check_pattern(pattern)
        if problem:
            return Failure(FailureReason.UNSUPPORTED, f"the pattern cannot be used: {problem}")
        store = self._open_store()           # its own connection: a window calls this from a background thread
        try:
            if paths is not None:
                records = []
                for path in paths:
                    record = store.get_file(path)
                    if record is not None and record.kind is FileKind.IMAGE and record.missing_since is None:
                        records.append(record)
                if not records and paths:
                    return Failure(FailureReason.OTHER, "none of those is a picture the index knows")
            else:
                if not folder:
                    return Failure(FailureReason.OTHER, "name a folder, or the pictures themselves")
                folder = os.path.abspath(os.path.expanduser(folder))
                if not any(_under(folder, r.path, self._fold) for r in store.list_roots()):
                    return Failure(FailureReason.OTHER, f"that folder is not inside an indexed folder: {folder}")
                records = [r for r in store.list_files(prefix=folder, case_insensitive=self.case_insensitive)
                           if r.kind is FileKind.IMAGE and r.missing_since is None]
        finally:
            store.close()
        started = _time.monotonic()
        found = naming.Plan(pattern)
        captions: Dict[str, str] = {}
        to_caption = records if every else [r for r in records if naming.is_meaningless(os.path.basename(r.path))]
        if caption and "{caption}" in pattern and to_caption:
            ready = self.captions_ready()
            if ready is not True and "not fetched" in str(ready):
                for item in embedding_models.CAPTIONS_BY_CHOICE["base"]:
                    got = models.fetch(item, self.layout.models / "captions", progress, stop)
                    if isinstance(got, Failure):
                        return got
                ready = self.captions_ready()
            if ready is not True:
                return Failure(FailureReason.NOT_INSTALLED, ready)
            from .jobs import CAPTION, path_to_data
            from .sandbox import SubprocessSandbox
            gpu = self.settings.caption_gpu if gpu is None else bool(gpu)
            sandbox = SubprocessSandbox()
            try:
                for at in range(0, len(to_caption), 8):
                    if stop is not None and stop.is_set():
                        found.stopped = True
                        break
                    batch = to_caption[at:at + 8]
                    if progress is not None:
                        progress(Progress("caption", "", at, len(to_caption), elapsed=_time.monotonic() - started, current=batch[0].path))
                    answer = sandbox.run(CAPTION, {"folder": str(self.layout.models / "captions"), "gpu": gpu,
                                                   "paths": [path_to_data(r.path) for r in batch], "longer": longer},
                                         Limits(1200.0), stop)
                    if isinstance(answer, Failure):
                        return answer
                    if "failure" in answer:
                        return Failure(FailureReason(answer["failure"]), str(answer["detail"]))
                    found.device = str(answer.get("device") or found.device)
                    for record, text in zip(batch, answer.get("captions") or []):
                        captions[record.path] = str(text or "")
                        found.captioned += 1 if text else 0
            finally:
                sandbox.close()
            if progress is not None:
                progress(Progress("caption", "", len(to_caption), len(to_caption), elapsed=_time.monotonic() - started))
        protected = [p.strip() for p in self.settings.cleanup_protect.split(";") if p.strip()]

        def clock(modified: float) -> str:
            return datetime.datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H-%M")

        found.changes = naming.plan(records, pattern, captions, clock, protected, self._fold, every)
        found.seconds = _time.monotonic() - started
        return found

    def apply_names(self, found, journal=None, stop: Optional[StopSignal] = None):
        """Rename as the plan says (naming.apply); the index follows."""
        from . import naming
        return naming.apply(found.to_rename, self.store, journal, stop)

    def undo_names(self, record_text: str, journal=None):
        from . import naming
        return naming.undo(naming.read_record(record_text), self.store, journal)

    def measure_looks(self, pictures: int = 300, gpu: Optional[bool] = None, questions: Sequence[str] = (),
                      like: Sequence[str] = (), progress: Optional[Callable[[Progress], None]] = None,
                      stop: Optional[StopSignal] = None):
        """Measure pictures by likeness on a sample of this index's pictures (looks.measure). Fetches the
        picture model on first use into the data folder. Stores nothing in the index."""
        from . import looks
        from .sandbox import SubprocessSandbox
        return looks.measure(self.store, SubprocessSandbox, self.layout.models / "looks", pictures, gpu, questions,
                             [os.path.abspath(os.path.expanduser(p)) for p in like], progress=progress, stop=stop)

    def meaning_model(self) -> str:
        """The tag of the sketches the settings ask for (meaning.model_name)."""
        from . import meaning
        return meaning.model_name(self.settings.meaning_model, meaning.dims_for(self.settings.meaning_model, self.settings.meaning_size))

    def meaning_coverage(self) -> Tuple[int, int]:
        """(files sketched with the chosen model and size, files with text that could be)."""
        return self.store.sketch_counts(self.meaning_model())

    def sketch_question(self, question: str):
        """A sketch of a question, from the model the sketches in the index were made with, as float32
        numbers; or a Failure when that cannot be done here. The worker that does it is made on first use
        and kept, on the processor: one question is quick, and a card may be busy sketching the index."""
        from . import meaning
        ready = self.meaning_ready()
        if ready is not True:
            return Failure(FailureReason.NOT_INSTALLED, ready)
        choice = self.settings.meaning_model
        dims = meaning.dims_for(choice, self.settings.meaning_size)
        with self._asker_lock:
            if self._asker is None or self._asker[1].choice != choice:
                from .sandbox import SubprocessSandbox
                if self._asker is not None:
                    self._asker[0].close()
                sandbox = SubprocessSandbox()
                self._asker = (sandbox, meaning._Runner(sandbox, self.layout.models / "meaning", choice, False))
            vectors = self._asker[1].embed([question], "query", dims, None, meaning.LOAD_SECONDS)
            if isinstance(vectors, Failure):
                self._asker[0].close()
                self._asker = None
                return vectors
        return vectors[0]

    def warm_meaning(self) -> "bool | Failure":
        """Load the model that sketches questions now, so the first search by meaning does not wait for it.
        True when it is ready; a Failure saying what is missing (the runtime, the model) otherwise. Safe to
        call from a background thread; the first search afterwards finds the worker warm."""
        made = self.sketch_question("ready")
        return made if isinstance(made, Failure) else True

    def wants_warm_meaning(self) -> bool:
        """Whether a window should warm the meaning model as it opens (setting meaning_warm_up: yes; never;
        auto, which is yes when the index holds sketches, since someone who filled them means to use them)."""
        choice = self.settings.meaning_warm_up
        if choice == "never":
            return False
        if choice == "yes":
            return True
        try:
            return self.meaning_coverage()[0] > 0
        except Exception:
            return False

    def meaning_ready(self) -> "bool | str":
        """True when sketches can be made now: the runtime is installed and the chosen model is fetched.
        Otherwise a line saying what is missing (an updating run does not fetch half a gigabyte by itself)."""
        from . import embedding_models
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
        except ImportError:
            return "the runtime for searching by meaning is not installed (pip install dugup[meaning])"
        if not embedding_models.present(self.layout.models / "meaning", self.settings.meaning_model):
            size = sum(i.size for i in embedding_models.BY_CHOICE[self.settings.meaning_model]) / 1048576
            return f"the {self.settings.meaning_model} model is not fetched yet ({size:,.0f} MB); `dugup meaning fill` fetches it"
        return True

    def meaning_waiting(self) -> int:
        """Files with text that have no sketch yet: what a `run_meaning` would take up (its folder may stop
        short of meaning; that is settled file by file as it runs)."""
        done, could = self.meaning_coverage()
        return max(0, could - done)

    def run_meaning(self, stop: Optional[StopSignal] = None, progress: Optional[Callable[[Progress], None]] = None,
                    budget_seconds: Optional[float] = None, gpu: Optional[bool] = None):
        """Make sketches of meaning (meaning.fill) for the files that have none, in the calling thread, until
        done, stopped or out of time. Fetches the model on first use into the data folder. Writes to the index,
        so it takes the same lock as indexing and OCR: one at a time. Files in folders whose level stops short
        of meaning are left alone. Returns a meaning.FillReport or a Failure."""
        from . import embedding_models, meaning
        from .sandbox import SubprocessSandbox
        gpu = self.settings.meaning_gpu if gpu is None else bool(gpu)
        choice = self.settings.meaning_model
        dims = meaning.dims_for(choice, self.settings.meaning_size)
        if not self._run_guard.acquire(blocking=False):
            return Failure(FailureReason.BUSY, "indexing or OCR is already going on in this program")
        try:
            if not self._lock.acquire():
                return Failure(FailureReason.BUSY, "another program is writing to this data folder right now")
            try:
                folder = self.layout.models / "meaning"
                if not embedding_models.present(folder, choice):
                    fetched = meaning.fetch_models(folder, choice, progress, stop)
                    if isinstance(fetched, Failure):
                        return fetched
                store = self._open_store()
                try:
                    roots = list(store.list_roots())

                    def asks_for_meaning(file_path: str) -> bool:
                        owner = next((r for r in roots if _under(file_path, r.path, self._fold)), None)
                        return owner is None or wants(level_for(owner, file_path, self.case_insensitive), OPTION_MEANING)

                    def tell(p: Progress) -> None:
                        self._progress = p
                        if progress is not None:
                            progress(p)

                    sandbox = SubprocessSandbox()
                    try:
                        runner = meaning._Runner(sandbox, folder, choice, gpu)
                        report = meaning.fill(store, runner, choice, dims, asks_for_meaning, tell, stop, budget_seconds,
                                              room=self.room)
                    finally:
                        sandbox.close()
                    if report.failure is None and not report.stopped and not report.out_of_time and not report.paused \
                            and self._store_thread in (None, threading.get_ident()):
                        report.compacted = self.compact_if_worthwhile()
                    return report
                finally:
                    store.close()
            finally:
                self._lock.release()
        finally:
            self._run_guard.release()

    def copies(self, path: str, stop: Optional[StopSignal] = None):
        """Folders below `path` that look like copies of each other (copies.Pair, most files first), or a
        Failure. From names and sizes alone: nothing is opened, indexed or changed, and skip rules are not
        applied, so everything is compared, except tool data (downloaded requirements and the like), which
        is alike everywhere and tells nobody anything."""
        from . import copies as finding
        target = os.path.abspath(os.path.expanduser(path))
        listing = self._lister().scan(target, ScanRules(), stop)
        if isinstance(listing, Failure):
            return listing
        from .suggest import safe_skips
        base = target.rstrip("/\\")
        tool, _ = safe_skips(target, [(e.path, e.size) for e in listing.entries], self.settings.extensions)
        return finding.find(((e.path[len(base) + 1:].replace("\\", "/"), e.size) for e in listing.entries),
                            stop=stop, skip=tool)

    def _apply_policy(self, root: Root, below: Optional[str] = None) -> Tuple[int, int]:
        """Make what is stored fit the level of every file below a folder (the whole root when `below` is None).
        Where the level is now "names", the text is removed; where it is no longer, the files wait to be read.
        Returns (files whose text was removed, files put in the queue)."""
        purged = queued = 0
        for record in self.store.list_files(prefix=below or root.path, case_insensitive=self.case_insensitive):
            names_only = not wants(level_for(root, record.path, self.case_insensitive), OPTION_WORDS)
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
        if level is not None:
            spelled = normalize_level(level)
            if spelled is None:
                return Failure(FailureReason.UNSUPPORTED,
                               f"{level!r} is not a choice. Use names, or any of words, ocr, meaning, alike joined by +, "
                               "for example words+ocr")
            level = spelled
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
        """Files worth a look: something went wrong that a setting or another try might put right."""
        return [r for r in self._troubled() if not cannot_be_read(r.error)][offset:offset + limit]

    def unreadable_files(self, limit: Optional[int] = None, offset: int = 0) -> Sequence[FileRecord]:
        """Files that are damaged or need a password. Their names are searchable; nothing here will read them."""
        found = [r for r in self._troubled() if cannot_be_read(r.error)]
        return found[offset:] if limit is None else found[offset:offset + limit]

    def attention_counts(self) -> Mapping[str, int]:
        """How many files are worth a look, how many cannot be read (damaged or protected), and how many only
        wait for OCR (queued, nothing wrong)."""
        partial = self.store.list_files(state=FileState.PARTIAL, missing=False)
        waiting = sum(1 for r in partial if only_waits_for_ocr(r.error))
        troubled = self._troubled()
        hopeless = sum(1 for r in troubled if cannot_be_read(r.error))
        return {"attention": len(troubled) - hopeless, "unreadable": hopeless, "waiting_for_ocr": waiting}

    # -- indexing

    def run_indexing(self, stop: Optional[StopSignal] = None, progress: Optional[Callable[[Progress], None]] = None,
                     roots: Optional[Sequence[str]] = None, on_settings_change: str = REFUSE,
                     retry_failed: bool = False, readers: Optional[int] = None) -> "IndexReport | Failure":
        """Index now, in the calling thread, until done or stopped. Only one run at a time, across programs.
        A run that finishes also compacts the index when enough of it is unused space. `readers`: files read
        side by side (None: as the machine and the setting say; a gentle background run passes fewer)."""
        return self._tidy(self._run_indexing(stop, progress, roots, on_settings_change, retry_failed, readers))

    def _run_indexing(self, stop, progress, roots, on_settings_change, retry_failed, readers=None) -> "IndexReport | Failure":
        if not self._run_guard.acquire(blocking=False):
            return Failure(FailureReason.BUSY, "an indexing run is already going on in this program")
        try:
            if not self._lock.acquire():
                return Failure(FailureReason.BUSY, "another program is indexing into this data folder right now")
            try:
                store = self._open_store()   # the writer: its own connection, in this thread
                more: list = []
                try:
                    def tell(p: Progress) -> None:
                        self._progress = p
                        if progress is not None:
                            progress(p)

                    more = self._more_index_readers((readers if readers else self.index_readers()) - 1)
                    indexer = Indexer(store, self.source, self.extractor, self.settings, tell, self.clock,
                                      extra_excludes=(str(self.layout.root),), room=self.room,
                                      size_now=self.layout.index_bytes, case_insensitive=self.case_insensitive,
                                      extra_extractors=more)
                    report = indexer.run(stop, roots, on_settings_change, retry_failed)
                    self.last_report = report
                    return report
                finally:
                    for reader in more:
                        try:
                            reader.close()
                        except Exception:
                            pass
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

    def index_readers(self) -> int:
        """How many files an indexing run reads side by side. More than one only when files are read in worker
        processes (the default): readers that run inside this program are not made to be shared."""
        from . import machine
        if not isinstance(self.extractor, SandboxedExtractor):
            return 1
        return machine.index_readers_for(self.settings.index_readers, free_bytes=machine.free_memory_bytes())

    def _more_index_readers(self, count: int) -> list:
        """`count` more document readers, each with a worker of its own. The caller closes them."""
        return [SandboxedExtractor(SubprocessSandbox(), self.settings) for _ in range(max(0, count))]

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
                fast: Optional[bool] = None, again: bool = False, gpu: Optional[bool] = None) -> "OcrReport | Failure":
        """Read the pages and pictures that wait for OCR, in the calling thread, until done or stopped.
        It writes to the index, so it takes the same lock as an indexing run: one or the other, never both.

        `folder`: only what lies in this folder (an indexed folder or any folder inside one).
        `fast`: several readers side by side (see fast_readers); the machine is busier while it runs. None: fast
        when at least `ocr_fast_pages` pages wait (and that is above 0), else gentle.
        `again`: with `folder`, read its pictures and scans afresh even where that was done before.
        `gpu`: read on a graphics card (None: as the setting ocr_gpu says). Where the OCR runtime cannot use
        one, the processor is used and the report says so."""
        return self._tidy(self._run_ocr(stop, progress, roots, budget_seconds, retry_failed, reread_old, folder, fast,
                                        again, gpu))

    def _run_ocr(self, stop, progress, roots, budget_seconds, retry_failed, reread_old, folder=None, fast=None,
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

                    chose_fast = 0
                    if fast is None:     # not told: a long wait deserves the fast way, a short one the gentle
                        threshold = self.settings.ocr_fast_pages
                        waiting = sum(1 for t in store.ocr_queue(folder, self.case_insensitive) if asks_for_ocr(t.path)
                                      and any(_under(t.path, r, self._fold) for r in here)) if threshold > 0 else 0
                        fast = threshold > 0 and waiting >= threshold
                        chose_fast = waiting if fast else 0
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
                        report.chose_fast = chose_fast
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
