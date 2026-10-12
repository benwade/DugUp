"""A tiny PNG writer (standard library only) for test images."""

from __future__ import annotations

import struct
import zlib


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def build_png(width: int = 32, height: int = 24, seed: int = 0) -> bytes:
    """A small grey picture with a pattern (no text in it)."""
    rows = b"".join(b"\x00" + bytes(((x * 7 + y * 13 + seed * 31) % 256) for x in range(width)) for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b"")
