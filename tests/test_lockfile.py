"""The run lock: one holder at a time, never a wait, and a dead holder never keeps it."""

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import helpers
from dugup.lockfile import RunLock

SRC = str(Path(__file__).resolve().parents[1] / "src")
HOLDER = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[2])
    from dugup.lockfile import RunLock
    lock = RunLock(sys.argv[1])
    print("held" if lock.acquire() else "refused", flush=True)
    sys.stdin.read()          # hold the lock until the parent closes the pipe (or kills this process)
""")


def test_a_second_holder_is_refused_until_the_first_lets_go(tmp_path):
    path = tmp_path / helpers.AWKWARD_FOLDER
    path.mkdir()
    first, second = RunLock(path / "index.lock"), RunLock(path / "index.lock")
    assert first.acquire() and first.held
    assert first.acquire(), "asking again while holding is harmless"
    assert not second.acquire() and not second.held
    first.release()
    assert not first.held and second.acquire()
    second.release()
    second.release()   # releasing twice is harmless


def test_the_lock_works_as_a_context_manager(tmp_path):
    with RunLock(tmp_path / "x.lock") as lock:
        assert lock.acquire()
    assert not lock.held and RunLock(tmp_path / "x.lock").acquire()


def _holder(lock_path):
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_path), SRC], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held"   # the child says so itself: no waiting on a clock
    return child


def test_another_process_holding_the_lock_is_respected_and_its_exit_frees_it(tmp_path):
    lock_path = tmp_path / "index.lock"
    child = _holder(lock_path)
    mine = RunLock(lock_path)
    try:
        assert not mine.acquire()
    finally:
        child.stdin.close()
        child.wait(timeout=30)
        child.stdout.close()
    assert mine.acquire()
    mine.release()


def test_a_killed_holder_does_not_keep_the_lock(tmp_path):
    lock_path = tmp_path / "index.lock"
    child = _holder(lock_path)
    child.kill()
    child.wait(timeout=30)
    child.stdin.close()
    child.stdout.close()
    mine = RunLock(lock_path)
    # On Windows, python.exe in a virtual environment is a small launcher that starts the real interpreter.
    # Killing it ends the real one a moment later, and the lock is freed when THAT process is gone. So the
    # test asks until the lock is free, and the limit only bounds how long a broken lock can hold the run up.
    deadline = time.monotonic() + 30.0
    while not mine.acquire() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert mine.held, "a crash must never leave the index locked"
    mine.release()
