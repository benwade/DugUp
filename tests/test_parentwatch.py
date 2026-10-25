"""Ending `dugup mcp serve` when the app that started it is gone (parentwatch.py).

No test waits on a delay: the watch is driven through `check` directly, and the real-process tests wait for the
child to have ended (Popen.wait returns only then) before asking."""

import os
import subprocess
import sys
import threading

import pytest

from dugup import parentwatch
from dugup.parentwatch import Watch, watched_pid


# ---- the watch ----------------------------------------------------------------------------------------------

def test_the_watch_acts_once_when_the_app_is_gone():
    answers = iter([True, True, False, False])
    gone = []
    watch = Watch(lambda: next(answers), lambda: gone.append(1))
    assert watch.check() and watch.check()
    assert not watch.check() and gone == [1]
    assert not watch.check() and gone == [1], "only once"


def test_a_question_that_fails_never_ends_the_server():
    def broken():
        raise OSError("could not ask")
    gone = []
    assert Watch(broken, lambda: gone.append(1)).check() is True and gone == []


def test_run_returns_after_acting_and_does_not_run_once_stopped():
    answers = iter([True, True, False])
    gone = []
    Watch(lambda: next(answers), lambda: gone.append(1), every=0).run()
    assert gone == [1]
    stop = threading.Event()
    stop.set()
    asked = []
    Watch(lambda: asked.append(1) or False, lambda: gone.append(2), every=0, stop=stop).run()
    assert asked == [] and gone == [1]


# ---- which process to watch ---------------------------------------------------------------------------------

def _tree(**parents):
    """{pid: (its parent, its own file name)} from keyword pairs like p10=(1, "Claude.exe"), as the Windows
    process table gives them."""
    table = {int(k[1:]): v for k, v in parents.items()}
    return (lambda p: table.get(p, (0, ""))[0]), (lambda p: table.get(p, (0, ""))[1])


def test_an_installed_python_watches_its_parent():
    parent_of, name_of = _tree(p30=(20, "python.exe"), p20=(10, "Claude.exe"))
    assert watched_pid(30, parent_of, name_of, r"C:\DugUp\python\python.exe", r"C:\DugUp\python\python.exe") == 20
    parent_of, name_of = _tree(p30=(20, "python.exe"), p20=(10, "python.exe"), p10=(1, "Claude.exe"))
    assert watched_pid(30, parent_of, name_of, r"C:\DugUp\python\python.exe", r"C:\DugUp\python\python.exe") == 20, \
        "outside a virtual environment a parent named python.exe is not a launcher"


def test_behind_a_venv_launcher_the_app_beyond_it_is_watched():
    """The case that left a server behind: the app starts the venv's python.exe, a launcher, which starts the
    real Python. The launcher lives as long as the server, so watching it would never end anything."""
    parent_of, name_of = _tree(p30=(20, "python.exe"), p20=(10, "python.exe"), p10=(1, "Claude.exe"))
    venv = r"D:\checkout\.venv\Scripts\python.exe"
    base = r"C:\Users\a\miniconda3\python.exe"
    assert watched_pid(30, parent_of, name_of, venv, base) == 10


def test_a_venv_started_straight_from_the_app_watches_the_app():
    parent_of, name_of = _tree(p30=(10, "python.exe"), p10=(1, "Claude.exe"))
    assert watched_pid(30, parent_of, name_of, r"D:\c\.venv\Scripts\python.exe", r"C:\py\python.exe") == 10


def test_nothing_is_watched_when_the_parent_cannot_be_told():
    parent_of, name_of = _tree()
    assert watched_pid(30, parent_of, name_of, "python", "python") is None


# ---- asking the system about a real process -------------------------------------------------------------------

def _child():
    return subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows process handles")
def test_windows_tells_a_running_process_from_one_that_has_ended():
    child = _child()
    try:
        alive = parentwatch.windows_alive(child.pid)
        assert alive is not None and alive()
    finally:
        child.stdin.close()
        child.wait(timeout=60)          # returns once the process has ended
    assert alive() is False


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows process table")
def test_the_windows_table_knows_this_process_and_its_parent():
    table = parentwatch._windows_table()
    assert table[os.getpid()][0] == os.getppid()
    assert table[os.getpid()][1].lower().endswith(".exe")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process table")
def test_posix_tells_a_running_process_from_one_that_has_ended():
    child = _child()
    try:
        alive = parentwatch.posix_alive(child.pid)
        assert alive()
    finally:
        child.stdin.close()
        child.wait(timeout=60)          # ended and reaped: its id is no longer in the table
    assert alive() is False


def test_the_server_starts_and_stops_its_watch(monkeypatch):
    from dugup import mcpserver
    from dugup.assistant import NoIndex

    class FakeServer:
        def run(self, how):
            assert how == "stdio"

    started = []

    class FakeWatch:
        stopped = False

        def stop(self):
            FakeWatch.stopped = True

    monkeypatch.setattr(mcpserver, "build", lambda tools: FakeServer())
    monkeypatch.setattr(parentwatch, "start_for_this_process", lambda: started.append(1) or FakeWatch())
    mcpserver.serve(None, lambda: None, tools=NoIndex(layout=None))
    assert started == [1] and FakeWatch.stopped
    started.clear()
    mcpserver.serve(None, lambda: None, tools=NoIndex(layout=None), watch_parent=False)
    assert started == []
