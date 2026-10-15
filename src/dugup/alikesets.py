"""Near-duplicate pictures: sets of pictures that look nearly the same (by their sketches, looks.py), a
keeper chosen by rules a person can see, and the rest proposed for the trash.

Unlike the file cleanup (cleanup.py), which moves only byte-identical copies, these pictures differ in bytes
(a resized copy, a re-saved one, a second shot of the same scene), so the threshold is a judgement and the
sets are for a person to look at before anything moves. Nothing here moves a file.

The keeper of a set: a picture in a preferred folder first; else the larger file (an original over a resized
or re-saved copy); else the older one; else the shorter path. Pictures in protected folders are shown aside
and never proposed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .shapes import FileRecord

THRESHOLD = 0.97           # closeness at and above which two pictures count as nearly the same
LOOSE = 0.90               # the fingerprint scan keeps pairs at or above this; the full sketches decide
BLOCK = 2048


@dataclass
class Member:
    record: FileRecord
    closeness: float           # to the keeper (1.0 for the keeper itself)
    protected: bool = False
    why: str = ""              # for the keeper: which rule chose it
    identity: Optional[Tuple] = None   # naming.seen_identity when the set was made; None: the file was not there
    stale: str = ""            # why the index's sketch is not evidence about the file at the path now ("" when it is)

    def __post_init__(self) -> None:
        """Look at the file once, and check that it is the file the index described: the sketch the set was
        built from belongs to the indexed file, so a file that changed since (edited, replaced) is stale, shown
        but never proposed, until a scan renews it. The identity kept here is what the act checks against."""
        from .naming import same_as_indexed, seen_identity
        if self.identity is None and not self.stale:      # a copy of a stale member is never looked at again
            self.identity = seen_identity(self.record.path)
        if not self.stale:
            self.stale = same_as_indexed(self.identity, self.record)


@dataclass
class AlikeSet:
    keep: Member
    extra: List[Member]                           # at or above the threshold against the keeper: proposed to go
    aside: List[Member] = field(default_factory=list)     # protected: shown, never weighed
    left: List[Member] = field(default_factory=list)      # in the chain, but below the threshold against this keeper
    threshold: float = THRESHOLD
    stale: List[Member] = field(default_factory=list)     # changed since indexed: the sketch is not about them; never weighed

    @property
    def bytes_extra(self) -> int:
        return sum(m.record.size for m in self.extra)


@dataclass
class AlikeReport:
    threshold: float
    sets: List[AlikeSet]
    pictures: int                        # pictures with sketches that were compared
    seconds: float = 0.0
    stopped: bool = False

    @property
    def extra_pictures(self) -> int:
        return sum(len(s.extra) for s in self.sets)

    @property
    def bytes_extra(self) -> int:
        return sum(s.bytes_extra for s in self.sets)


def pairs_alike(ids, prints, full_of: Callable[[Sequence[int]], Dict[int, object]], threshold: float = THRESHOLD,
                stop=None, progress: Optional[Callable[[int, int], None]] = None) -> List[Tuple[int, int, float]]:
    """(file id, file id, closeness) for every pair at or above `threshold`. The thin fingerprints find the
    likely pairs block by block (float32 matrix products, so a large collection takes seconds, not hours);
    the full sketches decide."""
    import numpy as np
    n = len(ids)
    if n < 2:
        return []
    thin = prints.astype(np.float32)
    thin /= np.maximum(np.linalg.norm(thin, axis=1, keepdims=True), 1e-9)
    likely: List[Tuple[int, int]] = []
    for start in range(0, n, BLOCK):
        if stop is not None and stop.is_set():
            return []
        block = thin[start:start + BLOCK]
        scores = block @ thin[start:].T                       # only the pairs ahead of the block: each pair once
        rows, cols = np.nonzero(scores >= LOOSE)
        for r, c in zip(rows, cols):
            a, b = start + int(r), start + int(c)
            if a < b:
                likely.append((a, b))
        if progress is not None:
            progress(min(n, start + BLOCK), n)
    if not likely:
        return []
    wanted = sorted({ids[a] for a, _ in likely} | {ids[b] for _, b in likely})
    full = full_of(wanted)
    found: List[Tuple[int, int, float]] = []
    for a, b in likely:
        va, vb = full.get(ids[a]), full.get(ids[b])
        if va is None or vb is None:
            continue
        score = float(np.dot(va, vb))
        if score >= threshold:
            found.append((int(ids[a]), int(ids[b]), min(1.0, score)))
    return found


def _sets(pairs: Sequence[Tuple[int, int, float]]) -> List[List[int]]:
    parent: Dict[int, int] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _ in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: Dict[int, List[int]] = {}
    for x in list(parent):
        groups.setdefault(find(x), []).append(x)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def choose_keeper(records: Sequence[FileRecord], preferred: Sequence[str], fold: Callable[[str], str]) -> Tuple[FileRecord, str]:
    """The picture to keep of a set, and the rule that chose it."""
    def inside(path: str, folders: Sequence[str]) -> bool:
        return any(fold(path).startswith(fold(f.rstrip("/\\")) + sep) for f in folders for sep in ("/", "\\"))

    liked = [r for r in records if inside(r.path, preferred)]
    if liked and len(liked) < len(records):
        pool, why = liked, "in a preferred folder"
    else:
        pool, why = list(records), ""
    biggest = max(r.size for r in pool)
    large = [r for r in pool if r.size == biggest]
    if len(large) < len(pool):
        pool, why = large, why or "the larger file"
    oldest = min(r.modified for r in pool)
    old = [r for r in pool if r.modified == oldest]
    if len(old) < len(pool):
        pool, why = old, why or "the older one"
    keeper = min(pool, key=lambda r: (len(r.path), r.path))
    return keeper, why or ("the shorter path" if len(pool) > 1 else "the only choice")


def _around(keep: Member, open_ones: Sequence[Member], shielded: Sequence[Member],
            close: Callable[[int, int], float], threshold: float) -> AlikeSet:
    """A set measured against its keeper. A chain (A like B, B like C) can hold a picture that is not within
    the threshold of the keeper at all; such a one is `left`, shown but never proposed to go. Members come in
    as they were first seen (their identity is kept, never looked up again here): one that is stale, or a
    keeper that is, stays so, so choosing another keeper cannot restore trust in a file that changed."""
    keeper = keep.record

    def measured(member: Member, protected: bool = False) -> Member:
        return replace(member, closeness=close(member.record.row_id, keeper.row_id), protected=protected, why="")

    near, left, stale = [], [], []
    for m in open_ones:
        member = measured(m)
        (stale if member.stale else near if member.closeness >= threshold else left).append(member)
    if keep.stale:                      # a changed keeper is no evidence for its extras: shown, nothing proposed
        stale.extend(near)
        near = []
    near.sort(key=lambda m: -m.closeness)
    left.sort(key=lambda m: -m.closeness)
    stale.sort(key=lambda m: -m.closeness)
    aside = [measured(m, True) for m in shielded]
    return AlikeSet(keep, near, aside, left, threshold, stale)


def rekeep(store, same: AlikeSet, keeper_row_id: int) -> AlikeSet:
    """The same set with another of its open members as keeper, every closeness measured afresh against it
    and the split at the threshold made again. The members are the ones first seen: a stale member stays
    stale, and a stale member cannot be chosen (the set comes back as it was)."""
    from . import sketches
    from .embedding import LOOKS_DIMS
    members = [same.keep] + same.extra + same.left + same.stale
    chosen = next((m for m in members if m.record.row_id == keeper_row_id), None)
    if chosen is None or chosen.stale:
        return same
    raw = store.looks_of([m.record.row_id for m in members] + [m.record.row_id for m in same.aside])
    vectors = {i: sketches.dequantize(v[1], LOOKS_DIMS, [v[0]])[0] for i, v in raw.items()}

    def close(a: int, b: int) -> float:
        va, vb = vectors.get(a), vectors.get(b)
        return min(1.0, float(va @ vb)) if va is not None and vb is not None else 0.0

    return _around(replace(chosen, closeness=1.0, protected=False, why="chosen by hand"),
                   [m for m in members if m is not chosen], list(same.aside), close, same.threshold)


def report(store, model: str, threshold: float = THRESHOLD, protected: Sequence[str] = (), preferred: Sequence[str] = (),
           fold: Callable[[str], str] = lambda p: p, stop=None,
           progress: Optional[Callable[[int, int], None]] = None, timer: Callable[[], float] = time.monotonic) -> AlikeReport:
    """Every set of pictures that look nearly the same, with a keeper. Changes nothing."""
    started = timer()
    ids, prints = store.look_fingerprints(model)
    found = AlikeReport(threshold, [], int(len(ids)))

    def full_of(wanted):
        from . import sketches
        from .embedding import LOOKS_DIMS
        raw = store.looks_of(list(wanted))
        out = {}
        for file_id, (scale, sketch) in raw.items():
            out[file_id] = sketches.dequantize(sketch, LOOKS_DIMS, [scale])[0]
        return out

    pairs = pairs_alike(ids, prints, full_of, threshold, stop, progress)
    if stop is not None and stop.is_set():
        found.stopped = True
        found.seconds = timer() - started
        return found
    groups = _sets(pairs)
    vectors = full_of([i for g in groups for i in g])

    def close(a: int, b: int) -> float:
        va, vb = vectors.get(a), vectors.get(b)
        return min(1.0, float(va @ vb)) if va is not None and vb is not None else 0.0

    records: Dict[int, FileRecord] = {}
    for group in groups:
        for file_id in group:
            if file_id not in records:
                record = store.get_file_by_id(file_id)
                if record is None:
                    continue
                records[file_id] = record
        members = [records[i] for i in group if i in records]
        if len(members) < 2:
            continue

        def shielded(r: FileRecord) -> bool:
            return any(fold(r.path).startswith(fold(f.rstrip("/\\")) + sep) for f in protected for sep in ("/", "\\"))

        open_ones = [Member(r, 0.0) for r in members if not shielded(r)]
        fresh = [m for m in open_ones if not m.stale]      # the sketch is evidence only about the file as indexed
        if len(fresh) < 2:
            continue                  # as the file cleanup does: protected copies are shown aside, never weighed
        keeper, why = choose_keeper([m.record for m in fresh], preferred, fold)
        keep = next(m for m in fresh if m.record is keeper)
        made = _around(replace(keep, closeness=1.0, why=why), [m for m in open_ones if m is not keep],
                       [Member(r, 0.0, True) for r in members if shielded(r)], close, threshold)
        if not made.extra:
            continue
        found.sets.append(made)
    found.sets.sort(key=lambda s: (-s.bytes_extra, s.keep.record.path))
    found.seconds = timer() - started
    return found
