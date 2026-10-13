"""A mark for a file's exact content (a fingerprint; called a mark here because "fingerprint" already names
the settings an index was built with).

Two files with the same fingerprint hold the same bytes, whatever they are called and wherever they lie.
The file is read once, start to end, and nothing in it is interpreted, so a damaged or hostile file is as
safe to fingerprint as any other. The fingerprint says nothing about what the file contains.
"""

from __future__ import annotations

import hashlib
from typing import Optional

CHUNK = 1024 * 1024
BYTES = 16              # 128 bits: two different files in one index sharing a fingerprint is not going to happen


def of_file(path: str, stop=None) -> Optional[str]:
    """The fingerprint as text (hex), or None when a stop was asked for part way. OSError is the caller's."""
    digest = hashlib.blake2b(digest_size=BYTES)
    with open(path, "rb") as handle:
        while True:
            if stop is not None and stop.is_set():
                return None
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def of_bytes(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=BYTES).hexdigest()
