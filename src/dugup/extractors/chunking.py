"""Cleaning text and cutting it into overlapping chunks (DigUp's 1800 characters with 200 overlap)."""

from __future__ import annotations

import unicodedata
from typing import List

_KEEP = {"\n", "\t"}


def clean_text(text: str) -> str:
    """One line-ending style, no control characters, no unpaired surrogates, no outer white space."""
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any((ch < " " and ch not in _KEEP) or "\x7f" <= ch <= "\x9f" or "\ud800" <= ch <= "\udfff" for ch in text):
        text = "".join(" " if ((ch < " " and ch not in _KEEP) or "\x7f" <= ch <= "\x9f" or "\ud800" <= ch <= "\udfff")
                       else ch for ch in text)
    return text.strip()


def chunk_text(text: str, size: int, overlap: int) -> List[str]:
    """Chunks of at most `size` characters; neighbours share about `overlap` characters. Cuts prefer white space."""
    text = clean_text(text)
    if not text:
        return []
    if size < 1:
        raise ValueError("chunk size must be at least 1")
    overlap = max(0, min(overlap, size - 1))
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    start, length = 0, len(text)
    slack = max(1, size // 6)   # how far back a cut may move to land on white space
    while start < length:
        end = min(start + size, length)
        if end < length:
            cut = _last_space(text, end - slack, end)
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        nxt = end - overlap
        if nxt <= start:      # always move forward, whatever the numbers
            nxt = end
        else:                 # begin the next chunk at a word start: look back first, then forward
            space = _last_space(text, max(nxt - slack, start + 1), nxt)
            if space > start:
                nxt = space
            elif not text[nxt - 1].isspace():
                ahead = _first_space(text, nxt, end)
                if ahead > 0:
                    nxt = ahead
        start = nxt
    return chunks


def _last_space(text: str, low: int, high: int) -> int:
    """Index just after the last white-space character in text[low:high], or -1."""
    for i in range(high - 1, max(low, 0) - 1, -1):
        if text[i].isspace():
            return i + 1
    return -1


def _first_space(text: str, low: int, high: int) -> int:
    """Index just after the first white-space character in text[low:high], or -1."""
    for i in range(low, min(high, len(text))):
        if text[i].isspace():
            return i + 1
    return -1


def unusable_fraction(text: str) -> float:
    """Share of the non-space characters that carry no readable meaning (replacement, private use, unassigned)."""
    total = bad = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        if ch == "\ufffd" or unicodedata.category(ch) in ("Co", "Cn", "Cc", "Cs"):
            bad += 1
    return bad / total if total else 0.0
