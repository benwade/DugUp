"""Sketches of meaning, kept small: how a vector is stored as bytes and found again in two stages.

A sketch (an embedding, embedding.py) is a row of float32 numbers of length 1. Stored, each number becomes
one signed byte with one scale for the row: a quarter of the space, and the closeness between two sketches
comes out the same to about two decimal places. The first FINGERPRINT numbers of every sketch are kept
once more as the "fingerprint", a thin table that fits in memory; a search scans it first and re-scores the
best few hundred from their full sketches. None of this knows which model made the sketches: the store
keeps the model's name beside them, and sketches from different models are never compared.
"""

from __future__ import annotations

from typing import Sequence, Tuple

FINGERPRINT = 96            # numbers in the thin fingerprint (the first ones; the larger model's can be cut short)
CANDIDATES = 800            # how many the fingerprint scan hands on to be re-scored in full
SCAN_BLOCK = 65536          # fingerprints scored at a time (a block is 24 MB as float32)


def quantize(vectors) -> Tuple[bytes, "list[float]"]:
    """Rows of float32 as bytes (one signed byte a number, rows laid end to end) and one scale a row."""
    import numpy as np
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim == 1:
        array = array[None, :]
    widest = np.maximum(np.abs(array).max(axis=1), 1e-9)
    scales = (widest / 127.0).astype(np.float32)
    packed = np.clip(np.rint(array / scales[:, None]), -127, 127).astype(np.int8)
    return packed.tobytes(), [float(s) for s in scales]


def dequantize(data: bytes, dims: int, scales: Sequence[float]):
    """The rows back as float32 (close to, not exactly, what went in)."""
    import numpy as np
    packed = np.frombuffer(data, dtype=np.int8).reshape(len(scales), dims)
    return packed.astype(np.float32) * np.asarray(scales, dtype=np.float32)[:, None]


def fingerprint_of(vector) -> bytes:
    """The thin fingerprint of one sketch: its first FINGERPRINT numbers as bytes, scaled on their own so
    that scanning them with a question's fingerprint ranks like the full sketch would, roughly."""
    import numpy as np
    head = np.asarray(vector, dtype=np.float32)[:FINGERPRINT]
    widest = max(float(np.abs(head).max()), 1e-9)
    return np.clip(np.rint(head / widest * 127.0), -127, 127).astype(np.int8).tobytes()


PIECES_EACH = 3             # how many of one file's pieces the scan hands on, at most


def fingerprint_scores(fingerprints, asked):
    """Each fingerprint's score against the question's, as float32: exact (every sum of 96 products of
    numbers up to 127 fits a float32's 24 bits), done in blocks so no copy of the whole matrix is ever
    made, and through the fast float routines instead of numpy's slow integer ones."""
    import numpy as np
    rows = fingerprints.shape[0]
    want = np.asarray(asked, dtype=np.float32)
    scores = np.empty(rows, dtype=np.float32)
    for at in range(0, rows, SCAN_BLOCK):
        scores[at:at + SCAN_BLOCK] = fingerprints[at:at + SCAN_BLOCK].astype(np.float32) @ want
    return scores


def scan_files(fingerprints, file_ids, question, keep: int = CANDIDATES, each: int = PIECES_EACH):
    """The row numbers of the fingerprints to re-score, chosen so that `keep` different FILES are covered:
    each file's best `each` rows by the fingerprint, best file first. A file with many pages near the
    question then takes a few places, not the whole list. `file_ids` is an int64 row per fingerprint."""
    import numpy as np
    rows = fingerprints.shape[0]
    if rows == 0 or keep <= 0:
        return np.zeros(0, dtype=np.int64)
    head = np.asarray(question, dtype=np.float32)[:FINGERPRINT]
    asked = np.clip(np.rint(head / max(float(np.abs(head).max()), 1e-9) * 127.0), -127, 127).astype(np.int32)
    scores = fingerprint_scores(fingerprints, asked)
    files = np.asarray(file_ids, dtype=np.int64)
    take = min(rows, keep * 4)
    while True:      # widen until enough different files are in, or every row is
        top = np.argpartition(-scores, take - 1)[:take] if take < rows else np.arange(rows)
        top = top[np.argsort(-scores[top], kind="stable")]
        chosen_files = np.unique(files[top])
        if len(chosen_files) >= keep or take >= rows:
            break
        take = min(rows, take * 4)
    # walk the best first: a file's first `each` rows are kept, until `keep` files have been seen
    kept, seen, counts = [], {}, {}
    for row in top:
        f = int(files[row])
        if f not in seen:
            if len(seen) >= keep:
                continue
            seen[f] = True
        if counts.get(f, 0) < each:
            counts[f] = counts.get(f, 0) + 1
            kept.append(int(row))
    return np.asarray(kept, dtype=np.int64)


def scan(fingerprints, question, keep: int = CANDIDATES):
    """The row numbers of the `keep` fingerprints closest to the question's, best first. `fingerprints` is
    an int8 matrix (rows, FINGERPRINT); `question` the question's full sketch."""
    import numpy as np
    head = np.asarray(question, dtype=np.float32)[:FINGERPRINT]
    asked = np.clip(np.rint(head / max(float(np.abs(head).max()), 1e-9) * 127.0), -127, 127).astype(np.int32)
    scores = fingerprint_scores(fingerprints, asked)
    keep = min(max(0, keep), scores.shape[0])
    if keep == 0:
        return np.zeros(0, dtype=np.int64)
    top = np.argpartition(-scores, keep - 1)[:keep] if keep < scores.shape[0] else np.arange(scores.shape[0])
    return top[np.argsort(-scores[top])]


def closeness(full, question):
    """How close each full sketch (rows) is to the question: the cosine, since both are of length 1."""
    import numpy as np
    return np.asarray(full, dtype=np.float32) @ np.asarray(question, dtype=np.float32)
