"""Pictures by likeness: one sketch a picture, filled in the background by readers side by side, and found
by a sentence or by another picture.

`fill()` makes sketches (sketches.py) of the pictures that have none yet, the most recently changed first,
with several workers side by side (opening and scaling pictures is the slow part, not the model), and keeps
them as bytes; a picture that cannot be read is noted, so it is not tried again every run. `model_name()`
tags what was stored. `measure()` takes a sample of the index's own pictures, makes sketches of them with the pinned picture model
(embedding.Looker) on the processor and, when the runtime can, on a graphics card, and reports speed, the
disk a full index of pictures would take, and what a few sentences, or a picture given as an example, find in
the sample. Nothing is stored. The model is fetched on request into the data folder (models/looks).
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import embedding, embedding_models, models
from .indexer import Progress
from .jobs import LOOK, path_to_data
from .protocols import Limits, StopSignal
from .shapes import Failure, FailureReason, FileRecord as FileRecord_

BATCH = 32
LOAD_SECONDS = 600.0
BATCH_SECONDS = 300.0
FILES_PER_STEP = 256
ROOM_EVERY = 8             # batches between looks at whether the index may still grow


def model_name(choice: str = "base") -> str:
    """What a stored look is tagged with: the model choice and the first letters of a hash over its pinned
    files, e.g. "base:3a9f1c0e". A changed pin makes every picture want a new look."""
    pins = embedding_models.LOOKS_BY_CHOICE.get(choice, ())
    revision = hashlib.sha256("".join(p.sha256 for p in pins).encode("ascii")).hexdigest()[:8] if pins else "unpinned"
    return f"{choice}:{revision}"


@dataclass
class FillReport:
    model: str = ""
    pictures: int = 0              # pictures sketched in this run
    unread: int = 0                # pictures that could not be read in this run (noted, not retried)
    left_alone: int = 0            # pictures passed over because their folder does not ask for likeness
    done: int = 0                  # pictures looked at by this model, all told, at the end
    could: int = 0                 # pictures there are
    readers: int = 1
    device: str = ""
    seconds: float = 0.0
    load_seconds: float = 0.0
    stopped: bool = False
    out_of_time: bool = False
    paused: str = ""
    failure: Optional[Failure] = None
    compacted: Optional[Dict[str, int]] = None

    @property
    def per_second(self) -> float:
        return self.pictures / self.seconds if self.seconds > 0 else 0.0


@dataclass
class Timing:
    device: str
    pictures: int
    seconds: float
    load_seconds: float
    failed: int = 0

    @property
    def per_second(self) -> float:
        return self.pictures / self.seconds if self.seconds > 0 else 0.0


@dataclass
class Hit:
    path: str
    score: float


@dataclass
class Answer:
    asked: str                # the sentence, or "like <path>"
    hits: List[Hit]


@dataclass
class Measurement:
    pictures: int
    in_index: int
    timings: List[Timing] = field(default_factory=list)
    answers: List[Answer] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    unread: Dict[str, List[str]] = field(default_factory=dict)   # why pictures could not be read -> a few of their paths

    def disk_for(self, bytes_each: int = 1) -> int:
        return self.in_index * embedding.LOOKS_DIMS * bytes_each


def fetch_models(folder: Path, choice: str = "base", progress: Optional[Callable[[Progress], None]] = None,
                 stop: Optional[StopSignal] = None) -> "None | Failure":
    for item in embedding_models.LOOKS_BY_CHOICE[choice]:
        got = models.fetch(item, Path(folder), progress, stop)
        if isinstance(got, Failure):
            return got
    return None


class _Runner:
    """Sketches of pictures and sentences made in a worker, in batches."""

    def __init__(self, sandbox, folder: Path, gpu: bool, threads: int = 0) -> None:
        self.sandbox, self.folder, self.gpu, self.threads = sandbox, str(folder), gpu, threads
        self.device = ""

    def _run(self, payload: dict, stop, seconds: float):
        payload.update({"folder": self.folder, "gpu": self.gpu, "threads": self.threads})
        answer = self.sandbox.run(LOOK, payload, Limits(seconds), stop)
        if isinstance(answer, Failure):
            return answer
        if "failure" in answer:
            return Failure(FailureReason(answer["failure"]), str(answer["detail"]))
        self.device = str(answer.get("device") or self.device)
        return answer

    def warm(self, stop=None, seconds: float = LOAD_SECONDS):
        """Load the model in this runner's worker now. "" or a Failure."""
        answer = self._run({"paths": []}, stop, seconds)
        return answer if isinstance(answer, Failure) else ""

    def look(self, paths: Sequence[str], stop=None, seconds: float = BATCH_SECONDS, batch: int = BATCH):
        """(vectors, {row: why it failed}) for pictures at these paths, or a Failure."""
        import numpy as np
        pieces, failed = [], {}
        for at in range(0, len(paths), batch):
            answer = self._run({"paths": [path_to_data(p) for p in paths[at:at + batch]]}, stop, seconds)
            if isinstance(answer, Failure):
                return answer
            pieces.append(embedding.unpack(answer["vectors"], int(answer["rows"]), int(answer["dims"])))
            failed.update({at + int(k): v for k, v in (answer.get("failed") or {}).items()})
        return (np.concatenate(pieces) if pieces else np.zeros((0, embedding.LOOKS_DIMS), dtype=np.float32)), failed

    def read(self, sentences: Sequence[str], stop=None, seconds: float = BATCH_SECONDS):
        answer = self._run({"sentences": list(sentences)}, stop, seconds)
        if isinstance(answer, Failure):
            return answer
        return embedding.unpack(answer["vectors"], int(answer["rows"]), int(answer["dims"]))


def fill(store, runners: Sequence["_Runner"], wanted: Callable[[str], bool] = lambda path: True,
         progress: Optional[Callable[[Progress], None]] = None, stop: Optional[StopSignal] = None,
         budget_seconds: Optional[float] = None, timer: Callable[[], float] = time.monotonic,
         room: Optional[Callable[[], str]] = None, choice: str = "base", batch: int = BATCH) -> FillReport:
    """Sketch every picture that has no look by this model yet, the most recently changed first, with the
    runners side by side (each has its own worker), until done, stopped, out of time, out of room or failed.
    Each batch is saved as it comes back, so a stopped run has lost nothing. `wanted(path)` says whether a
    picture's folder asks for likeness."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from . import sketches
    name = model_name(choice)
    report = FillReport(model=name, readers=max(1, len(runners)))
    started = timer()
    if not runners:
        report.failure = Failure(FailureReason.OTHER, "no reader was given")
        return report
    with ThreadPoolExecutor(max_workers=len(runners)) as warming:      # every reader loads the model at once
        for warm in warming.map(lambda r: r.warm(stop), runners):
            if isinstance(warm, Failure):
                report.failure, report.stopped = warm, warm.reason is FailureReason.STOPPED
                report.done, report.could, _ = store.look_counts(name)
                return report
    report.load_seconds = timer() - started
    report.device = runners[0].device
    started = timer()
    done_before, total, _ = store.look_counts(name)
    after = None
    exhausted = False
    batches_done = 0

    def next_batch() -> Optional[List[FileRecord_]]:
        nonlocal after, exhausted
        found: List = []
        while len(found) < batch and not exhausted:
            rows = store.unlooked(name, FILES_PER_STEP, after=after)
            if not rows:
                exhausted = True
                break
            for record in rows:
                after = (record.modified, record.row_id)
                if not wanted(record.path):
                    report.left_alone += 1
                    continue
                found.append(record)
                if len(found) >= batch:
                    break
        return found or None

    def keep(records, made) -> None:
        vectors, failed = made
        rows = []
        for i, record in enumerate(records):
            if i in failed:
                rows.append((record.row_id, 0.0, None, None, failed[i]))
                report.unread += 1
            else:
                data, scales = sketches.quantize(vectors[i:i + 1])
                rows.append((record.row_id, scales[0], data, sketches.fingerprint_of(vectors[i]), ""))
                report.pictures += 1
        store.save_looks(name, rows)

    with ThreadPoolExecutor(max_workers=max(1, len(runners))) as pool:
        idle = list(runners)
        flying = {}      # future -> (runner, records)
        while True:
            if stop is not None and stop.is_set():
                report.stopped = True
                break
            if budget_seconds is not None and timer() - started > budget_seconds:
                report.out_of_time = True
                break
            if room is not None and batches_done % ROOM_EVERY == 0 and not flying:
                report.paused = str(room() or "")
                if report.paused:
                    break
            while idle and not report.stopped:
                records = next_batch()
                if records is None:
                    break
                runner = idle.pop()
                future = pool.submit(runner.look, [r.path for r in records], stop)
                flying[future] = (runner, records)
            if not flying:
                break
            finished, _ = wait(list(flying), return_when=FIRST_COMPLETED)
            for future in finished:
                runner, records = flying.pop(future)
                idle.append(runner)
                made = future.result()
                if isinstance(made, Failure):
                    if made.reason is FailureReason.STOPPED:
                        report.stopped = True
                    else:
                        report.failure = made
                    continue
                keep(records, made)
                batches_done += 1
                if progress is not None:
                    progress(Progress("look", "", done_before + report.pictures + report.unread, total,
                                      failed=report.unread, elapsed=timer() - started, current=records[-1].path))
            if report.failure is not None:
                break
        for future in flying:          # a stop or a failure: what is in flight is dropped and comes round again next run
            future.cancel()
    report.seconds = timer() - started
    report.done, report.could, _ = store.look_counts(name)
    if progress is not None:
        progress(Progress("look", "", report.done, report.could, failed=report.unread, elapsed=report.seconds))
    return report


def measure(store, sandbox_factory, folder: Path, pictures: int = 300, gpu: Optional[bool] = None,
            questions: Sequence[str] = (), like: Sequence[str] = (), choice: str = "base",
            progress: Optional[Callable[[Progress], None]] = None, stop: Optional[StopSignal] = None,
            timer: Callable[[], float] = time.monotonic) -> "Measurement | Failure":
    """`gpu` None: the processor, and the card too when the runtime has one. `like`: paths of pictures
    to find the likes of in the sample (they need not be in the index)."""
    import numpy as np
    sample = store.sample_pictures(pictures)
    if not sample:
        return Failure(FailureReason.OTHER, "the index lists no pictures to measure with")
    paths = [p for _, p in sample]
    found = Measurement(len(sample), store.picture_count())
    if not embedding_models.looks_present(folder, choice):
        fetched = fetch_models(folder, choice, progress, stop)
        if isinstance(fetched, Failure):
            return fetched
    devices = [False, True] if gpu is None else [bool(gpu)]
    vectors = None
    for on_card in devices:
        sandbox = sandbox_factory()
        try:
            runner = _Runner(sandbox, folder, on_card)
            started = timer()
            warm = runner.warm(stop)        # loads the model
            if isinstance(warm, Failure):
                if on_card and warm.reason is not FailureReason.STOPPED:
                    found.notes.append(f"the graphics card could not be used ({warm.detail}); processor only")
                    continue
                return warm
            load_seconds = timer() - started
            if on_card and not runner.device.startswith("graphics"):
                found.notes.append("no runtime for a graphics card here; the card run was left out")
                continue
            if progress is not None:
                progress(Progress("look", "pictures", 0, len(paths), current=runner.device))
            started = timer()
            made = runner.look(paths, stop)
            if isinstance(made, Failure):
                return made
            vectors, failed = made
            seconds = timer() - started
            found.timings.append(Timing(runner.device, len(paths) - len(failed), seconds, load_seconds, len(failed)))
            if not found.unread:
                for row, why in sorted(failed.items()):
                    found.unread.setdefault(why, []).append(paths[row])
            if progress is not None:
                progress(Progress("look", "pictures", len(paths), len(paths), elapsed=seconds, current=runner.device))
            if not found.answers:
                asks = list(questions)
                if asks:
                    read = runner.read(asks, stop)
                    if isinstance(read, Failure):
                        return read
                    for sentence, q in zip(asks, read):
                        found.answers.append(Answer(sentence, _top(vectors, q, paths, failed)))
                for example in like:
                    made_one = runner.look([example], stop)
                    if isinstance(made_one, Failure):
                        return made_one
                    one, bad = made_one
                    if bad:
                        found.notes.append(f"the example could not be read: {os.path.basename(example)} ({bad[0]})")
                        continue
                    found.answers.append(Answer(f"like {example}", _top(vectors, one[0], paths, failed, skip=example)))
        finally:
            close = getattr(sandbox, "close", None)
            if close is not None:
                close()
    return found


def _top(vectors, question, paths: Sequence[str], failed: Dict[int, str], count: int = 5, skip: str = "") -> List[Hit]:
    import numpy as np
    scores = vectors @ question
    order = [int(i) for i in np.argsort(-scores) if int(i) not in failed and paths[int(i)] != skip]
    return [Hit(paths[i], float(scores[i])) for i in order[:count]]
