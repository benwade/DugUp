"""The OCR queue: read the pages and pictures that wait for OCR, most useful first.

The indexer leaves a page that needs OCR marked in the store. This runner takes those pages in this order:

  0  scanned pages of documents (a PDF page with no usable text layer)
  1  typed pages that hold a sizeable picture (the typed text is found already; this is for the figure)
  2  pictures that look like documents (no camera named: scans, screenshots, saved graphics)
  3  photos (the picture names the camera that took it), and pictures whose shape is unknown
  4  tiny pictures (longest side under `ocr_tiny_pixels`): icons and buttons, never skipped
  5  second tries: a page that ran out of time is read again here, last of all, with more time

A page that runs out of time is not failed at once: it moves to class 5 (the store remembers, so this
holds across runs) and only a second timeout fails it.

Promises, the same as indexing:
- Resumable: each page is stored as it is read, in its own transaction. A run that stops or dies loses
  at most the page it was on, and the next run carries on.
- Interruptible: the stop signal is looked at between pages and handed to every read.
- Never silent: a page ends as read (with or without words), failed with its reason, or still waiting.
  The file's note says which, and the file becomes `done` once nothing is left to say.
- Careful: a missing engine, or a run of failures that looks like the engine and not the pictures,
  stops the run without writing off the queue.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .indexer import (OCR_FAILED_NOTE, OCR_WAIT, ROOM_EVERY, Progress, is_ocr_note, note_with_ocr, ocr_notes,
                      safe_rate)
from .protocols import OcrResult, OcrTask, PageVerdict, StopSignal, Store
from .settings import Settings
from .shapes import Failure, FailureReason, FileKind, FileState

log = logging.getLogger("dugup.ocr")

__all__ = ["OcrRunner", "OcrReport", "priority", "is_ocr_note", "ocr_notes", "OCR_FAILED_NOTE", "OCR_WAIT"]
DOCUMENT_PAGE, FIGURE_PAGE, DOCUMENT_PICTURE, PHOTO, TINY, SECOND_TRY = 0, 1, 2, 3, 4, 5
PROBE_CHUNK = 100
# Reasons that say something about the run, not about the page: nothing is marked failed for these.
NOT_READY = (FailureReason.NOT_INSTALLED, FailureReason.BUSY, FailureReason.UNSUPPORTED)
# Reasons that may be the engine's fault. Enough of them in a row pause the run.
SUSPECT = (FailureReason.CRASHED, FailureReason.TIMED_OUT, FailureReason.OUT_OF_MEMORY, FailureReason.OTHER)


def priority(task: OcrTask, tiny_pixels: int) -> int:
    if task.retry:
        return SECOND_TRY
    if task.kind is not FileKind.IMAGE:
        return FIGURE_PAGE if task.verdict == PageVerdict.BORN_DIGITAL.value else DOCUMENT_PAGE
    if not task.width or not task.height:
        return PHOTO                     # unknown shape: not first, not last
    if max(task.width, task.height) < tiny_pixels:
        return TINY
    return PHOTO if task.camera else DOCUMENT_PICTURE


@dataclass
class OcrReport:
    total: int = 0                 # pages waiting when the run began
    read: int = 0                  # pages read (with or without words on them)
    with_words: int = 0
    failed: int = 0                # pages marked as failed, each with its reason
    second_tries: int = 0          # pages that ran out of time and were given a second, longer try
    requeued: int = 0              # files with pages put back because another engine version read them
    left: int = 0                  # pages whose file could not be reached; they stay queued
    copied: int = 0                # pages of files that hold the same content as another: read once, used for each
    held_back: int = 0             # pages in folders whose level stops short of OCR; not looked at
    readers: int = 1               # how many readers worked side by side
    device: str = ""               # what the pages were read on, when the reader says ("processor", "graphics card (...)")
    gpu_asked: bool = False        # a graphics card was asked for (it may not have been there to use)
    stopped: bool = False
    out_of_time: bool = False
    paused: str = ""               # why the run paused itself (a run of failures)
    not_ready: Optional[Failure] = None    # the engine cannot be used at all (for example it is not installed)
    seconds: float = 0.0
    compacted: Optional[Dict[str, int]] = None   # the index's size before and after, when it was compacted at the end
    away: Tuple[str, ...] = ()     # folders that could not be reached and were left alone

    @property
    def rate(self) -> float:
        return safe_rate(self.read + self.failed, self.seconds)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.read - self.failed)


class OcrRunner:
    """`backend` needs read(image, language=, stop=), read_page(pdf, page, language=, stop=) and, optionally,
    probe(paths, stop). SandboxOcrBackend has all three; a test hands in a fake."""

    def __init__(self, store: Store, backend, settings: Optional[Settings] = None,
                 progress: Optional[Callable[[Progress], None]] = None,
                 timer: Optional[Callable[[], float]] = None, case_insensitive: Optional[bool] = None,
                 room: Optional[Callable[[], str]] = None,
                 wanted: Optional[Callable[[str], bool]] = None, extra_backends: Sequence = (),
                 first: str = "") -> None:
        self.extra_backends = tuple(extra_backends)   # more readers working side by side, each with its own engine
        self.first = first          # a folder whose pages are read before any others
        self.wanted = wanted        # says whether a file's folder asks for OCR at all; None: every folder does
        self.room = room            # says why nothing more may be written; "" when there is room
        self.store = store
        self.backend = backend
        self.settings = settings or Settings()
        self.progress = progress
        self.timer = timer or time.monotonic
        self.case_insensitive = case_insensitive

    # -- the file's state follows its pages

    def refresh_file(self, path: str) -> None:
        """Rewrite the OCR part of a file's note, and its state, from what became of its pages."""
        record = self.store.get_file(path)
        if record is None or record.row_id is None or record.state not in (FileState.PARTIAL, FileState.DONE):
            return
        state, note = note_with_ocr(self.store, record.row_id, record.error)
        if (state, note) != (record.state, record.error):
            self.store.set_state(record.row_id, state, note)

    # -- the queue

    def _queue(self, roots: Optional[Sequence[str]]) -> List[OcrTask]:
        if roots is None:
            return list(self.store.ocr_queue())
        tasks: Dict[Tuple[int, int], OcrTask] = {}
        for root in roots:
            for task in self.store.ocr_queue(root, self.case_insensitive):
                tasks[(task.row_id, task.page)] = task
        return list(tasks.values())

    def _measure(self, tasks: List[OcrTask], stop: Optional[StopSignal]) -> "List[OcrTask] | Failure":
        """Record the shape of pictures not yet looked at. A shape that cannot be had is recorded as 0 x 0."""
        probe = getattr(self.backend, "probe", None)
        unknown = [t for t in tasks if t.kind is FileKind.IMAGE and t.width is None]
        if probe is None or not unknown:
            return tasks
        measured: Dict[Tuple[int, int], OcrTask] = {}
        for at in range(0, len(unknown), PROBE_CHUNK):
            if _stopped(stop):
                return Failure(FailureReason.STOPPED, "stopped while pictures were measured")
            chunk = unknown[at:at + PROBE_CHUNK]
            self._tell(Progress("measure", "", at, len(unknown)))
            shapes = probe([t.path for t in chunk], stop)
            if isinstance(shapes, Failure):
                if shapes.reason is FailureReason.STOPPED:
                    return shapes
                log.warning("pictures could not be measured (%s); the queue keeps its plain order", shapes.reason.value)
                break
            with self.store.transaction():
                for task, shape in zip(chunk, shapes):
                    width, height, camera = shape or (0, 0, False)
                    self.store.set_page_shape(task.row_id, task.page, width, height, camera)
                    measured[(task.row_id, task.page)] = OcrTask(task.row_id, task.path, task.kind, task.page,
                                                                 width, height, camera)
        return [measured.get((t.row_id, t.page), t) for t in tasks]

    def _room(self) -> str:
        return "" if self.room is None else str(self.room() or "")

    def _read(self, task: OcrTask, stop: Optional[StopSignal], backend=None):
        backend = backend or self.backend
        more = {}
        if task.retry:      # the second try: more time, when the backend can say how much that is
            patient = getattr(backend, "patient_limit", None)
            more["time_limit"] = (patient(self.settings.retry_patience) if patient is not None
                                  else self.settings.ocr_seconds * self.settings.retry_patience)
        if task.kind is FileKind.PDF:
            return backend.read_page(task.path, task.page, language=self.settings.language, stop=stop, **more)
        return backend.read(task.path, language=self.settings.language, stop=stop, **more)

    def run(self, stop: Optional[StopSignal] = None, roots: Optional[Sequence[str]] = None,
            budget_seconds: Optional[float] = None, retry_failed: bool = False, reread_old: bool = False) -> OcrReport:
        started = self.timer()
        report = OcrReport()
        report.paused = self._room()
        if report.paused:
            return report
        if retry_failed:
            self._retry(roots)
        if reread_old:
            report.requeued = self._requeue_old(roots)
        waiting = self._queue(roots)
        if self.wanted is not None:
            asked = [task for task in waiting if self.wanted(task.path)]
            report.held_back = len(waiting) - len(asked)
            waiting = asked
        # The same page of the same content, in several files, is read once: the first file's page goes to a
        # reader, and what comes of it is given to the others ("followers") when it is stored.
        followers: Dict[Tuple[int, int], List[OcrTask]] = {}
        if self.settings.content_marks and self.settings.read_once:
            leaders: Dict[tuple, OcrTask] = {}
            marks: Dict[int, Optional[str]] = {}
            first_of_each = []
            for task in waiting:
                if task.row_id not in marks:
                    marks[task.row_id] = self.store.content_of(task.row_id)
                mark = marks[task.row_id]
                key = (mark, task.kind, task.page)
                leader = leaders.get(key) if mark else None
                if leader is None:
                    if mark:
                        leaders[key] = task
                    first_of_each.append(task)
                else:
                    followers.setdefault((leader.row_id, leader.page), []).append(task)
            everything = len(waiting)
            waiting = first_of_each
        else:
            everything = len(waiting)
        tasks = self._measure(waiting, stop)
        if isinstance(tasks, Failure):
            report.stopped = True
            report.seconds = self.timer() - started
            return report
        tiny = self.settings.ocr_tiny_pixels
        tasks.sort(key=lambda t: (priority(t, tiny), t.row_id, t.page))
        report.total = everything
        in_a_row = 0
        queue = list(tasks)
        if self.first:      # one folder was asked for first: its pages go ahead of the rest, each part in its own order
            queue.sort(key=lambda t: 0 if _below(t.path, self.first, self.case_insensitive) else 1)
        readers = [self.backend] + list(self.extra_backends)
        report.readers = len(readers)
        pool = ThreadPoolExecutor(len(readers), thread_name_prefix="dugup-ocr") if len(readers) > 1 else None
        halt = threading.Event()                 # ends the reads still going on when the run ends early
        both = _Either(stop, halt)
        idle = list(reversed(readers))
        flying: Dict[Future, Tuple[OcrTask, object]] = {}
        at = 0
        ending = False

        def finished(task: OcrTask, result) -> bool:
            """Take in one answer. True when the run must end."""
            nonlocal in_a_row
            if isinstance(result, OcrResult):
                with self.store.transaction():
                    self.store.save_ocr(task.row_id, task.page, result)
                    self.refresh_file(task.path)
                    for other in followers.pop((task.row_id, task.page), ()):
                        self.store.copy_page(task.row_id, other.row_id, task.page)
                        self.refresh_file(other.path)
                        report.read += 1
                        report.copied += 1
                        report.with_words += 1 if result.lines else 0
                report.read += 1
                report.with_words += 1 if result.lines else 0
                in_a_row = 0
                return False
            if not isinstance(result, Failure):
                result = Failure(FailureReason.CRASHED, "the OCR engine returned nothing")
            if result.reason is FailureReason.STOPPED:
                report.stopped = True
                return True
            if result.reason in NOT_READY:
                report.not_ready = result
                return True
            if result.reason is FailureReason.UNREACHABLE:
                report.left += 1          # the file or its drive is away: the page keeps its place in the queue
                others = followers.pop((task.row_id, task.page), [])
                if others:                # another file with the same content may be within reach: it is tried instead
                    queue.append(others[0])
                    if others[1:]:
                        followers[(others[0].row_id, others[0].page)] = others[1:]
                return False
            reason = f"{result.reason.value}: {result.detail}".replace(";", ",")
            if result.reason is FailureReason.TIMED_OUT and not task.retry and self.settings.retry_patience > 1.0:
                self.store.defer_ocr(task.row_id, task.page, reason)     # not failed yet: one more try, last of all
                queue.append(OcrTask(task.row_id, task.path, task.kind, task.page, task.width, task.height,
                                     task.camera, task.verdict, True))
                log.info("ocr deferred: row %s page %s", task.row_id, task.page)
            else:
                with self.store.transaction():
                    self.store.fail_ocr(task.row_id, task.page, reason)
                    self.refresh_file(task.path)
                    for other in followers.pop((task.row_id, task.page), ()):     # the same bytes fail the same way
                        self.store.fail_ocr(other.row_id, other.page, reason)
                        self.refresh_file(other.path)
                        report.failed += 1
                report.failed += 1
                log.info("ocr failed: row %s page %s reason %s", task.row_id, task.page, result.reason.value)
            if result.reason in SUSPECT:
                in_a_row += 1
                if in_a_row >= self.settings.ocr_failures_in_a_row:
                    report.paused = (f"{in_a_row} pages in a row failed or ran out of time (the last one: "
                                     f"{result.reason.value.replace('_', ' ')}), "
                                     "which looks like trouble with the OCR engine and not with the pictures")
                    return True
            return False

        try:
            while True:
                while idle and at < len(queue) and not ending:   # a page that runs out of time is put at the end,
                    task = queue[at]                             # so the list can grow
                    if at % ROOM_EVERY == 0 and at:
                        report.paused = self._room()
                        if report.paused:
                            ending = True
                            break
                    if _stopped(stop):
                        report.stopped = True
                        ending = True
                        break
                    if budget_seconds is not None and self.timer() - started >= budget_seconds:
                        report.out_of_time = True
                        ending = True
                        break
                    at += 1
                    self._tell(Progress("ocr", "", report.read + report.failed, report.total, report.failed,
                                        self.timer() - started, task.path))
                    if task.retry:
                        report.second_tries += 1
                    reader = idle.pop()
                    if pool is None:
                        answer: Future = Future()
                        answer.set_result(self._read(task, stop, reader))
                    else:
                        answer = pool.submit(self._read, task, both, reader)
                    flying[answer] = (task, reader)
                if not flying:
                    break
                done, _ = wait(list(flying), return_when=FIRST_COMPLETED)
                for answer in done:
                    task, reader = flying.pop(answer)
                    idle.append(reader)
                    try:
                        result = answer.result()
                    except Exception as exc:          # a reader must never take the run down
                        result = Failure(FailureReason.CRASHED, f"{type(exc).__name__}: {exc}")
                    if ending:
                        # the run is ending: a page that was read all the same is kept, anything else stays queued
                        if isinstance(result, OcrResult):
                            finished(task, result)
                    elif finished(task, result):
                        ending = True
                        halt.set()
                if ending:
                    halt.set()
        finally:
            halt.set()
            if pool is not None:
                pool.shutdown(wait=True)
        report.seconds = self.timer() - started
        self._tell(Progress("ocr", "", report.read + report.failed, report.total, report.failed, report.seconds))
        log.info("ocr run: total %s read %s failed %s left %s stopped %s", report.total, report.read, report.failed,
                 report.left, report.stopped)
        return report

    def _retry(self, roots: Optional[Sequence[str]]) -> None:
        wanted = set()
        if roots is None:
            wanted.update(self.store.retry_ocr())
        else:
            for root in roots:
                wanted.update(self.store.retry_ocr(root, self.case_insensitive))
        self._refresh_rows(wanted)

    def _requeue_old(self, roots: Optional[Sequence[str]]) -> int:
        """Pages read by another engine or version wait again. Their old text stays until they are read."""
        engine_id = getattr(self.backend, "engine_id", None)
        current = engine_id() if engine_id is not None else None
        if not current:
            return 0          # no engine to compare with: nothing is put back
        wanted = set()
        if roots is None:
            wanted.update(self.store.requeue_ocr_not_by(current))
        else:
            for root in roots:
                wanted.update(self.store.requeue_ocr_not_by(current, root, self.case_insensitive))
        self._refresh_rows(wanted)
        return len(wanted)

    def _refresh_rows(self, wanted) -> None:
        if not wanted:
            return
        for record in self.store.list_files(missing=None):
            if record.row_id in wanted:
                self.refresh_file(record.path)

    def _tell(self, progress: Progress) -> None:
        if self.progress is not None:
            self.progress(progress)


class _Either:
    """A stop signal that is set when either of two is."""

    def __init__(self, first: Optional[StopSignal], second: threading.Event) -> None:
        self.first, self.second = first, second

    def is_set(self) -> bool:
        return self.second.is_set() or (self.first is not None and self.first.is_set())


def _below(path: str, folder: str, case_insensitive: Optional[bool]) -> bool:
    fold = (lambda t: t.casefold()) if case_insensitive else (lambda t: t)
    base = fold(folder.rstrip("/\\"))
    path = fold(path)
    return path.startswith(base + "/") or path.startswith(base + "\\")


def _stopped(stop: Optional[StopSignal]) -> bool:
    return stop is not None and stop.is_set()
