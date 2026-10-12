"""One indexing run at a time (CONTRACTS.md part 6: one writer owns the file).

A lock on a small file in the data folder, taken without waiting. The operating system drops it when the
process ends, however it ends, so a crash never leaves the index locked.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class RunLock:
    def __init__(self, path: os.PathLike | str) -> None:
        self.path = Path(path)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """True when this object now holds the lock; False when someone else does. Never waits."""
        if self._fd is not None:
            return True
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            _lock(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            _unlock(self._fd)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
