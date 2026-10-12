"""What this machine has to work with: processor cores and free memory. Used to decide how many OCR readers
may work side by side. Everything here is a best effort: an answer of None means "could not tell"."""

from __future__ import annotations

import os
from typing import Optional


def cores() -> int:
    return max(1, os.cpu_count() or 1)


def free_memory_bytes() -> Optional[int]:
    """Memory that can be used without pushing other programs out, or None when the system does not say."""
    try:
        if os.name == "nt":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                            ("page_available", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                            ("virtual_available", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]

            status = Status()
            status.length = ctypes.sizeof(Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):   # type: ignore[attr-defined]
                return None
            return int(status.available)
        with open("/proc/meminfo", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        return None
    return None


def readers_for(wanted: int, threads_each: int, memory_each_mb: int, core_count: Optional[int] = None,
                free_bytes: Optional[int] = None, most: int = 16) -> int:
    """How many OCR readers to run side by side in fast mode.

    `wanted` above 0 is the person's own number and is used as given (still at least 1). Otherwise: readers
    times threads-each fill about half the cores, so the machine stays usable; never more than the free
    memory holds; never more than `most`. Division is guarded; an unknown amount of memory does not limit.
    """
    if wanted > 0:
        return max(1, wanted)
    core_count = cores() if core_count is None else max(1, core_count)
    by_cores = max(1, (core_count // 2) // max(1, threads_each))
    by_memory = most
    if free_bytes is not None and memory_each_mb > 0:
        by_memory = max(1, int(free_bytes // (memory_each_mb * 1024 * 1024)))
    return max(1, min(by_cores, by_memory, most))
