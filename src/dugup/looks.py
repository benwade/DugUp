"""Pictures by likeness: measured before it is built.

`measure()` takes a sample of the index's own pictures, makes sketches of them with the pinned picture model
(embedding.Looker) on the processor and, when the runtime can, on a graphics card, and reports speed, the
disk a full index of pictures would take, and what a few sentences, or a picture given as an example, find in
the sample. Nothing is stored. The model is fetched on request into the data folder (models/looks).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import embedding, embedding_models, models
from .indexer import Progress
from .jobs import LOOK, path_to_data
from .protocols import Limits, StopSignal
from .shapes import Failure, FailureReason

BATCH = 32
LOAD_SECONDS = 600.0
BATCH_SECONDS = 300.0


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
            warm = runner.look(paths[:1], stop, LOAD_SECONDS)        # loads the model
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
