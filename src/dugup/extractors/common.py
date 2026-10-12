"""What every extractor shares: opening a file safely and turning trouble into a stated reason."""

from __future__ import annotations

from typing import BinaryIO, Optional, Union

from ..dirscan import long_path
from ..protocols import StopSignal
from ..shapes import Failure, FailureReason


def stopped(stop: Optional[StopSignal]) -> bool:
    return stop is not None and stop.is_set()


STOPPED = Failure(FailureReason.STOPPED, "stopped before the file was read")


def open_file(path: str) -> Union[BinaryIO, Failure]:
    """Open for reading only. The path goes through the long-path form, so Windows paths over 260 work."""
    try:
        return open(long_path(path), "rb")
    except FileNotFoundError:
        return Failure(FailureReason.UNREACHABLE, "the file was not there when it was read")
    except PermissionError as exc:
        return Failure(FailureReason.OTHER,
                       f"the file could not be opened: no permission, or another program has it locked ({exc.strerror or exc})")
    except OSError as exc:
        return Failure(FailureReason.OTHER, f"the file could not be opened ({type(exc).__name__}: {exc.strerror or exc})")
