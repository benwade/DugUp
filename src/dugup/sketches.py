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


def scan(fingerprints, question, keep: int = CANDIDATES):
    """The row numbers of the `keep` fingerprints closest to the question's, best first. `fingerprints` is
    an int8 matrix (rows, FINGERPRINT); `question` the question's full sketch."""
    import numpy as np
    head = np.asarray(question, dtype=np.float32)[:FINGERPRINT]
    asked = np.clip(np.rint(head / max(float(np.abs(head).max()), 1e-9) * 127.0), -127, 127).astype(np.int32)
    scores = fingerprints @ asked                         # int8 rows against an int32 question: no copy made
    keep = min(max(0, keep), scores.shape[0])
    if keep == 0:
        return np.zeros(0, dtype=np.int64)
    top = np.argpartition(-scores, keep - 1)[:keep] if keep < scores.shape[0] else np.arange(scores.shape[0])
    return top[np.argsort(-scores[top])]


def closeness(full, question):
    """How close each full sketch (rows) is to the question: the cosine, since both are of length 1."""
    import numpy as np
    return np.asarray(full, dtype=np.float32) @ np.asarray(question, dtype=np.float32)
