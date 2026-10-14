"""Searching by meaning: sketches of every piece of text, filled in the background, and found in two stages.

`fill()` makes sketches (sketches.py) of the files that have none yet, the most recently changed first, and
keeps them in the store as bytes; it can be stopped at any point and carries on from there next time.
`model_name()` names what was stored (the model choice and how many numbers), so a change of either makes
every file want a new sketch. `measure()` takes a sample of the index's own text, makes sketches of it with one or both pinned models,
on the processor and (when the runtime can) on a graphics card, and reports speed, the disk a full index
would take at each vector size, and, for questions given, what the sample finds. Nothing is stored. The
models are fetched on request into the data folder (models/meaning), like the plain-words model.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import embedding, embedding_models, models
from .indexer import Progress
from .jobs import EMBED
from .protocols import Limits, StopSignal
from .shapes import Failure, FailureReason

BATCH = 64
LOAD_SECONDS = 600.0       # a first load reads half a gigabyte and may be on a slow disk
BATCH_SECONDS = 300.0
FILES_PER_STEP = 100       # files fetched from the store per step of the fill
PIECE_CHARS = 4000         # a piece of text longer than this is sketched from its start (the model reads about 512 tokens anyway)


def model_name(choice: str, dims: int) -> str:
    """What a stored sketch is tagged with: the model choice, how many of its numbers were kept, and the first
    letters of the pinned model file's hash, e.g. "larger:768:fe3a1c9b". A change of weights or tokenizer
    changes the pin, so every file wants a new sketch, and old and new are never compared."""
    pins = embedding_models.BY_CHOICE.get(choice, ())
    revision = hashlib.sha256("".join(p.sha256 for p in pins).encode("ascii")).hexdigest()[:8] if pins else "unpinned"
    return f"{choice}:{int(dims)}:{revision}"


def dims_for(choice: str, size: str) -> int:
    """How many numbers the sketches keep for a model choice and a size choice (embedding.SIZES); a model
    that cannot be cut short keeps all of its own."""
    shape = embedding.SHAPES[choice]
    asked = embedding.SIZES.get(size, shape.dims)
    return min(asked, shape.dims) if shape.cuttable else shape.dims


@dataclass
class FillReport:
    model: str = ""
    files: int = 0                 # files sketched in this run
    pieces: int = 0                # pieces of text sketched
    left_alone: int = 0            # files passed over because their folder does not ask for meaning
    done: int = 0                  # files sketched by this model, all told, at the end
    could: int = 0                 # files with text that could be
    device: str = ""
    seconds: float = 0.0
    load_seconds: float = 0.0
    stopped: bool = False
    out_of_time: bool = False
    paused: str = ""               # why the run stopped itself: no room left for the index to grow
    failure: Optional[Failure] = None   # what ended the run early, when something did
    compacted: Optional[Dict[str, int]] = None   # the index's size before and after, when it was compacted at the end

    @property
    def per_second(self) -> float:
        return self.pieces / self.seconds if self.seconds > 0 else 0.0


@dataclass
class Timing:
    choice: str
    device: str
    pieces: int
    seconds: float            # for the sketches, the model already loaded
    load_seconds: float
    dims: int

    @property
    def per_second(self) -> float:
        return self.pieces / self.seconds if self.seconds > 0 else 0.0


@dataclass
class Hit:
    path: str
    page: int
    score: float
    snippet: str


@dataclass
class Answer:
    question: str
    choice: str
    dims: int
    hits: List[Hit]


@dataclass
class Measurement:
    pieces: int
    in_index: int               # pieces of text in the whole index
    timings: List[Timing] = field(default_factory=list)
    answers: List[Answer] = field(default_factory=list)
    agreement: Dict[Tuple[str, int], float] = field(default_factory=dict)   # (question, dims) -> share of the top 10 at full size kept
    notes: List[str] = field(default_factory=list)

    def disk_for(self, dims: int, bytes_each: int = 4) -> int:
        return self.in_index * dims * bytes_each


def fetch_models(folder: Path, choice: str, progress: Optional[Callable[[Progress], None]] = None,
                 stop: Optional[StopSignal] = None) -> "None | Failure":
    for item in embedding_models.BY_CHOICE[choice]:
        got = models.fetch(item, Path(folder), progress, stop)
        if isinstance(got, Failure):
            return got
    return None


class _Runner:
    """Sketches made in a worker, in batches."""

    def __init__(self, sandbox, folder: Path, choice: str, gpu: bool, threads: int = 0) -> None:
        self.sandbox, self.folder, self.choice, self.gpu, self.threads = sandbox, str(folder), choice, gpu, threads
        self.device = ""

    def embed(self, texts: Sequence[str], kind: str, dims: int, stop=None, seconds: float = BATCH_SECONDS):
        import numpy as np
        pieces = []
        for at in range(0, len(texts), BATCH):
            answer = self.sandbox.run(EMBED, {"folder": self.folder, "choice": self.choice, "texts": list(texts[at:at + BATCH]),
                                              "kind": kind, "dims": dims, "gpu": self.gpu, "threads": self.threads},
                                      Limits(seconds), stop)
            if isinstance(answer, Failure):
                return answer
            if "failure" in answer:
                return Failure(FailureReason(answer["failure"]), str(answer["detail"]))
            self.device = str(answer.get("device") or self.device)
            pieces.append(embedding.unpack(answer["vectors"], int(answer["rows"]), int(answer["dims"])))
        return np.concatenate(pieces) if pieces else np.zeros((0, dims or 1), dtype=np.float32)


ROOM_EVERY = 50            # files between looks at whether the index may still grow


def fill(store, runner: "_Runner", choice: str, dims: int, wanted: Callable[[str], bool] = lambda path: True,
         progress: Optional[Callable[[Progress], None]] = None, stop: Optional[StopSignal] = None,
         budget_seconds: Optional[float] = None, timer: Callable[[], float] = time.monotonic,
         room: Optional[Callable[[], str]] = None) -> FillReport:
    """Sketch every file with text that has no sketch by this model yet, the most recently changed first,
    in the calling thread, until done, stopped, out of time, out of room or failed. Each file's sketches are
    saved as it is done, so a stopped run has lost nothing. `wanted(path)` says whether a file's folder asks
    for meaning. `room()` says why nothing more may be written ("" when there is room; the same guards as
    indexing: the drive's free space and the index's spending limit); it is asked before the first file and
    every ROOM_EVERY files after."""
    import numpy as np
    from . import sketches
    name = model_name(choice, dims)
    report = FillReport(model=name)
    started = timer()
    warm = runner.embed(["ready"], "document", dims, stop, LOAD_SECONDS)
    if isinstance(warm, Failure):
        report.failure, report.stopped = warm, warm.reason is FailureReason.STOPPED
        report.done, report.could = store.sketch_counts(name)
        return report
    report.load_seconds = timer() - started
    report.device = runner.device
    started = timer()
    done_before, total = store.sketch_counts(name)
    after = None
    while True:
        if stop is not None and stop.is_set():
            report.stopped = True
            break
        if budget_seconds is not None and timer() - started > budget_seconds:
            report.out_of_time = True
            break
        batch = store.unsketched(name, FILES_PER_STEP, after=after)
        if not batch:
            break
        for record in batch:
            after = (record.modified, record.row_id)
            if stop is not None and stop.is_set():
                report.stopped = True
                break
            if budget_seconds is not None and timer() - started > budget_seconds:
                report.out_of_time = True
                break
            if not wanted(record.path):
                report.left_alone += 1
                continue
            if room is not None and report.files % ROOM_EVERY == 0:
                report.paused = str(room() or "")
                if report.paused:
                    break
            if progress is not None:
                progress(Progress("sketch", "", done_before + report.files, total, elapsed=timer() - started,
                                  current=record.path))
            pieces = store.text_of(record.row_id)
            texts = [t[:PIECE_CHARS] for _, _, t in pieces]
            if not texts:
                store.save_sketches(record.row_id, name, dims, [])
                continue
            vectors = runner.embed(texts, "document", dims, stop)
            if isinstance(vectors, Failure):
                if vectors.reason is FailureReason.STOPPED:
                    report.stopped = True
                else:
                    report.failure = vectors
                break
            if vectors.shape[0] != len(texts):
                report.failure = Failure(FailureReason.CRASHED, f"the model gave {vectors.shape[0]} sketches for {len(texts)} pieces")
                break
            data, scales = sketches.quantize(vectors)
            rows = [(seg, scales[i], data[i * dims:(i + 1) * dims], sketches.fingerprint_of(vectors[i]))
                    for i, (seg, _, _) in enumerate(pieces)]
            store.save_sketches(record.row_id, name, dims, rows)
            report.files += 1
            report.pieces += len(texts)
        if report.stopped or report.out_of_time or report.paused or report.failure is not None:
            break
    report.seconds = timer() - started
    report.done, report.could = store.sketch_counts(name)
    if progress is not None:
        progress(Progress("sketch", "", report.done, report.could, elapsed=report.seconds))
    return report


def measure(store, sandbox_factory, folder: Path, choices: Sequence[str] = ("small", "larger"), pieces: int = 2000,
            gpu: Optional[bool] = None, questions: Sequence[str] = (), sizes: Sequence[int] = (768, 256, 128),
            progress: Optional[Callable[[Progress], None]] = None, stop: Optional[StopSignal] = None,
            timer: Callable[[], float] = time.monotonic) -> "Measurement | Failure":
    """`gpu` None: processor, and the card too when the runtime has one."""
    import numpy as np
    sample = store.sample_text(pieces)
    if not sample:
        return Failure(FailureReason.OTHER, "the index holds no text to measure with")
    texts = [t for _, _, _, t in sample]
    found = Measurement(len(sample), store.segment_count())
    devices = [False, True] if gpu is None else [bool(gpu)]
    for choice in choices:
        if not embedding_models.present(folder, choice):
            fetched = fetch_models(folder, choice, progress, stop)
            if isinstance(fetched, Failure):
                return fetched
        full = embedding.SHAPES[choice].dims
        by_dims: Dict[int, object] = {}
        for on_card in devices:
            sandbox = sandbox_factory()
            try:
                runner = _Runner(sandbox, folder, choice, on_card)
                started = timer()
                warm = runner.embed(texts[:2], "document", 0, stop, LOAD_SECONDS)       # loads the model
                if isinstance(warm, Failure):
                    if on_card and warm.reason is not FailureReason.STOPPED:
                        found.notes.append(f"{choice}: the graphics card could not be used ({warm.detail}); processor only")
                        continue
                    return warm
                load_seconds = timer() - started
                if on_card and not runner.device.startswith("graphics"):
                    found.notes.append(f"{choice}: no runtime for a graphics card here; the card run was left out")
                    continue
                if progress is not None:
                    progress(Progress("measure", choice, 0, len(texts), current=runner.device))
                started = timer()
                vectors = runner.embed(texts, "document", 0, stop)
                if isinstance(vectors, Failure):
                    return vectors
                seconds = timer() - started
                found.timings.append(Timing(choice, runner.device, len(texts), seconds, load_seconds, full))
                if progress is not None:
                    progress(Progress("measure", choice, len(texts), len(texts), elapsed=seconds, current=runner.device))
                if not by_dims:
                    by_dims[full] = vectors
                    if embedding.SHAPES[choice].cuttable:
                        for dims in sizes:
                            if dims < full:
                                cut = vectors[:, :dims]
                                by_dims[dims] = cut / np.maximum(np.linalg.norm(cut, axis=1, keepdims=True), 1e-9)
                    for question in questions:
                        asked = runner.embed([question], "query", 0, stop)
                        if isinstance(asked, Failure):
                            return asked
                        top_full: List[int] = []
                        for dims in sorted(by_dims, reverse=True):
                            q = asked[0][:dims]
                            q = q / max(float(np.linalg.norm(q)), 1e-9)
                            scores = by_dims[dims] @ q
                            order = list(np.argsort(-scores)[:10])
                            if dims == full:
                                top_full = order
                            else:
                                found.agreement[(question, dims)] = len(set(order) & set(top_full)) / 10.0
                            hits = [Hit(sample[i][1], sample[i][2], float(scores[i]), " ".join(texts[i].split())[:160]) for i in order[:5]]
                            found.answers.append(Answer(question, choice, dims, hits))
            finally:
                close = getattr(sandbox, "close", None)
                if close is not None:
                    close()
    return found
