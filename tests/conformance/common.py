"""Helpers shared by the conformance checks.

A conformance check is a plain function that takes a harness (or a store) and raises
`CheckFailed` with a plain-words message when the part breaks its contract. The same check runs
against a fake today and against the real part later.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional, Tuple

from dugup.shapes import Failure, FailureReason, failed

# How long a check waits for a call that should have returned, beyond the limit it gave.
# This only bounds how long a BROKEN part can hang the test run; a correct part never waits for it.
SLACK_SECONDS = 5.0


class CheckFailed(AssertionError):
    """The part under test broke its contract."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def require_failure(value: object, reason: FailureReason, what: str) -> Failure:
    """The value must be a Failure with this reason and a stated detail."""
    if isinstance(value, BaseException):
        raise CheckFailed(f"{what}: raised {type(value).__name__} instead of returning a Failure")
    require(failed(value), f"{what}: expected Failure({reason.value}), got {type(value).__name__}")
    require(value.reason is reason, f"{what}: expected reason {reason.value}, got {value.reason.value}")
    require(value.detail.strip(), f"{what}: the Failure has no detail")
    return value  # type: ignore[return-value]


def require_result(value: object, what: str) -> Any:
    """The value must not be a Failure (and must not be None: silence is not a result)."""
    require(value is not None, f"{what}: returned None (a part never returns nothing without a reason)")
    if failed(value):
        raise CheckFailed(f"{what}: unexpected Failure({value.reason.value}): {value.detail}")
    return value


class CountingStop:
    """A stop signal that turns on after it has been looked at `after` times.

    It makes "the part looks at the stop request between small units of work" testable without
    clocks or threads: a part that never looks, or looks only once at the start, never sees it.
    """

    def __init__(self, after: int) -> None:
        self.after = after
        self.looks = 0

    def is_set(self) -> bool:
        self.looks += 1
        return self.looks > self.after

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self.is_set()


def call_with_deadline(fn: Callable[[], Any], seconds: float) -> Tuple[bool, Any]:
    """Run fn in a thread. Returns (finished, value); an exception in fn comes back as the value."""
    box: dict = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # reported by the check, not lost
            box["value"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return False, None
    return True, box.get("value")


def timed(fn: Callable[[], Any]) -> Tuple[float, Any]:
    started = time.monotonic()
    value = fn()
    return time.monotonic() - started, value


def expect_fail(check: Callable[..., None], *args: Any) -> None:
    """For the meta-tests: the check must complain about this (deliberately broken) part."""
    try:
        check(*args)
    except CheckFailed:
        return   # the check complained in its own words, which is the point
    except Exception as exc:
        raise AssertionError(
            f"{getattr(check, '__name__', check)} crashed with {type(exc).__name__}: {exc} "
            "instead of reporting a broken part (a check must turn surprises into CheckFailed)")
    raise AssertionError(f"{getattr(check, '__name__', check)} did not catch the broken part")
