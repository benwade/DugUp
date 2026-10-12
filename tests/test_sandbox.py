"""The launcher and the Sandbox with real processes (CONTRACTS.md part 7 and "Stopping").

Nothing here sleeps to wait for something to happen. A job that must be caught in the act writes a
marker file when it starts, and the test waits for that file. WAIT only bounds how long a broken
program can hang the run.
"""

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import helpers
from dugup import launcher
from dugup.launcher import Completed, alive
from dugup.protocols import Limits, Sandbox
from dugup.sandbox import SubprocessSandbox
from dugup.shapes import Failure, FailureReason

WAIT = 30.0
TESTS = str(Path(__file__).resolve().parent)
SRC = str(Path(__file__).resolve().parents[1] / "src")
LONG = Limits(WAIT)


def wait_for(condition, what):
    waiter = threading.Event()
    for _ in range(int(WAIT / 0.01)):
        if condition():
            return
        waiter.wait(0.01)
    raise AssertionError(f"gave up waiting for {what}")


def wait_gone(pids, what="the processes to end"):
    wait_for(lambda: not any(alive(p) for p in pids), what)


def read_marker(path):
    wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0, "the job to start")
    return [int(x) for x in Path(path).read_text(encoding="utf-8").split()]


@pytest.fixture
def box():
    with SubprocessSandbox(extra_paths=[TESTS]) as sandbox:
        yield sandbox


@pytest.fixture
def marker(tmp_path):
    folder = tmp_path / helpers.AWKWARD_FOLDER
    folder.mkdir()
    return str(folder / "started.txt")


# ---- the launcher ------------------------------------------------------------------------------------------

def test_a_program_runs_and_its_output_comes_back_as_bytes():
    code = "import sys; sys.stdout.buffer.write('caf\\u00e9 \\u65e5\\u672c'.encode('utf-8')); sys.stderr.write('warn')"
    done = launcher.run([sys.executable, "-c", code], WAIT)
    assert isinstance(done, Completed) and done.returncode == 0
    assert done.stdout.decode("utf-8") == f"{helpers.CAFE} " + helpers.CJK[:2] and done.stderr == b"warn"


def test_input_is_handed_over_and_a_large_output_does_not_block():
    code = "import sys; data = sys.stdin.buffer.read(); sys.stdout.buffer.write(data * 200)"
    done = launcher.run([sys.executable, "-c", code], WAIT, input=b"x" * 10_000)
    assert isinstance(done, Completed) and len(done.stdout) == 2_000_000


def test_a_missing_program_is_not_installed_and_an_exit_code_is_reported():
    missing = launcher.run(["no-such-program-dugup-test"], WAIT)
    assert isinstance(missing, Failure) and missing.reason is FailureReason.NOT_INSTALLED
    done = launcher.run([sys.executable, "-c", "import sys; sys.exit(3)"], WAIT)
    assert isinstance(done, Completed) and done.returncode == 3


def test_a_program_that_runs_too_long_is_ended_with_its_children(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess, sys, threading; p = subprocess.Popen([sys.executable, '-c', "
            "'import threading; threading.Event().wait()']); open(sys.argv[1], 'w').write(str(p.pid)); "
            "threading.Event().wait()")
    result = launcher.run([sys.executable, "-c", code, str(pidfile)], 1.5)
    assert isinstance(result, Failure) and result.reason is FailureReason.TIMED_OUT and "1.5" in result.detail
    child = int(pidfile.read_text())
    wait_gone([child], "the child of a timed-out program to end")


def test_a_stop_request_ends_a_blocked_program_at_once():
    stop = threading.Event()
    stop.set()
    result = launcher.run([sys.executable, "-c", "import threading; threading.Event().wait()"], WAIT, stop=stop)
    assert isinstance(result, Failure) and result.reason is FailureReason.STOPPED


def test_killing_a_tree_twice_and_after_the_end_is_harmless():
    program = launcher.launch([sys.executable, "-c", "import threading; threading.Event().wait()"])
    assert not isinstance(program, Failure) and alive(program.pid)
    program.kill_tree()
    program.kill_tree()
    wait_gone([program.pid])
    program.close()
    program.close()
    assert not alive(0) and not alive(-1)


# ---- the Sandbox contract -----------------------------------------------------------------------------------

def test_the_sandbox_fits_its_contract(box):
    assert isinstance(box, Sandbox)


def test_a_job_finishes_and_non_ascii_survives_both_ways(box):
    payload = {"name": f"{helpers.CAFE} {helpers.CJK} {helpers.EMOJI}", "n": 3, "nested": {"list": [1, None, True]}}
    result = box.run("sandbox_jobs:echo", payload, LONG)
    assert result["got"] == payload and result["accents"] == f"{helpers.CAFE} {helpers.CJK} {helpers.EMOJI}"
    assert result["pid"] != os.getpid(), "the job must run in another process"


def test_one_worker_serves_many_jobs(box):
    pids = {box.run("sandbox_jobs:echo", {"i": i}, LONG)["pid"] for i in range(20)}
    assert len(pids) == 1 and box.started == 1


def test_a_job_that_raises_is_a_stated_failure_and_the_worker_lives_on(box):
    first = box.run("sandbox_jobs:echo", {}, LONG)["pid"]
    failed = box.run("sandbox_jobs:boom", {}, LONG)
    assert isinstance(failed, Failure) and failed.reason is FailureReason.CRASHED
    assert "RuntimeError" in failed.detail and helpers.CAFE in failed.detail
    assert box.run("sandbox_jobs:echo", {}, LONG)["pid"] == first


def test_a_worker_that_dies_is_replaced_for_the_next_job(box):
    first = box.run("sandbox_jobs:echo", {}, LONG)["pid"]
    died = box.run("sandbox_jobs:die", {"code": 9}, LONG)
    assert isinstance(died, Failure) and died.reason is FailureReason.CRASHED and "9" in died.detail
    again = box.run("sandbox_jobs:echo", {}, LONG)
    assert again["pid"] != first and box.started == 2


@pytest.mark.parametrize("job", ["hang", "spin"])
def test_a_job_over_its_time_limit_is_ended(box, marker, job):
    result = box.run(f"sandbox_jobs:{job}", {"marker": marker}, Limits(2.0))
    assert isinstance(result, Failure) and result.reason is FailureReason.TIMED_OUT and "2 s" in result.detail
    wait_gone(read_marker(marker), "the timed-out worker to end")
    assert box.run("sandbox_jobs:echo", {}, LONG)["got"] == {}, "the next job gets a fresh worker"


def test_a_job_that_needs_too_much_memory_is_out_of_memory(box):
    result = box.run("sandbox_jobs:hog", {}, Limits(WAIT, memory_mb=400))
    assert isinstance(result, Failure) and result.reason is FailureReason.OUT_OF_MEMORY
    assert box.run("sandbox_jobs:echo", {}, Limits(WAIT, memory_mb=400))["got"] == {}


def test_what_a_job_prints_cannot_forge_an_answer(box):
    assert box.run("sandbox_jobs:chatter", {}, LONG) == "the real answer"
    assert box.run("sandbox_jobs:echo", {"after": 1}, LONG)["got"] == {"after": 1}


def test_a_result_that_cannot_be_sent_is_a_stated_failure(box):
    result = box.run("sandbox_jobs:unserialisable", {}, LONG)
    assert isinstance(result, Failure) and result.reason is FailureReason.CRASHED
    assert box.run("sandbox_jobs:echo", {}, LONG)["got"] == {}


def test_a_payload_that_is_not_plain_data_is_a_programming_error(box):
    with pytest.raises(ValueError):
        box.run("sandbox_jobs:echo", {"bad": object()}, LONG)


def test_an_unknown_job_is_a_stated_failure(box):
    for name in ("sandbox_jobs:no_such_job", "no_such_module_xyz:job", "not-a-job-name"):
        result = box.run(name, {}, LONG)
        assert isinstance(result, Failure) and result.reason is FailureReason.CRASHED, name


@pytest.mark.parametrize("job", ["connect", "raw_socket"])
def test_a_job_cannot_use_the_network(box, job):
    result = box.run(f"sandbox_jobs:{job}", {}, LONG)
    assert isinstance(result, Failure) and result.reason is FailureReason.CRASHED
    assert "no network access" in result.detail or "unreachable" in result.detail.lower() or "refused" in result.detail.lower()


def test_the_worker_says_how_far_it_is_from_the_network(box):
    described = box.describe()
    assert described["network"] in ("namespace", "python") and described["pid"] in box.worker_pids()


# ---- stopping -----------------------------------------------------------------------------------------------

def test_a_stop_before_the_job_starts_nothing(box):
    stop = threading.Event()
    stop.set()
    result = box.run("sandbox_jobs:echo", {}, LONG, stop)
    assert isinstance(result, Failure) and result.reason is FailureReason.STOPPED and box.started == 0


def test_a_stop_while_the_job_is_blocked_ends_the_worker_and_everything_it_started(box, marker):
    stop = threading.Event()
    answers = []
    thread = threading.Thread(target=lambda: answers.append(box.run("sandbox_jobs:family", {"marker": marker}, LONG, stop)))
    thread.start()
    worker, child, grandchild = read_marker(marker)
    assert alive(worker) and alive(child) and alive(grandchild), "the whole family is running before the stop"
    stop.set()
    thread.join(WAIT)
    assert not thread.is_alive(), "the stop request did not get the caller out"
    assert isinstance(answers[0], Failure) and answers[0].reason is FailureReason.STOPPED
    wait_gone([worker, child, grandchild], "the worker, its child and its grandchild to end")


def test_closing_the_sandbox_ends_its_workers(marker):
    sandbox = SubprocessSandbox(extra_paths=[TESTS])
    pid = sandbox.run("sandbox_jobs:echo", {}, LONG)["pid"]
    assert alive(pid) and sandbox.worker_pids() == [pid]
    sandbox.close()
    wait_gone([pid], "the worker to end when the sandbox closes")
    sandbox.close()


def test_the_caller_stays_responsive_while_a_job_is_blocked(box, marker):
    stop = threading.Event()
    thread = threading.Thread(target=lambda: box.run("sandbox_jobs:hang", {"marker": marker}, LONG, stop))
    thread.start()
    read_marker(marker)
    assert thread.is_alive()
    done = launcher.run([sys.executable, "-c", "print(1)"], WAIT)     # this thread can still do other work
    assert isinstance(done, Completed) and done.stdout.strip() == b"1"
    stop.set()
    thread.join(WAIT)
    assert not thread.is_alive()


PARENT = textwrap.dedent("""
    import sys, threading
    sys.path[:0] = [sys.argv[1], sys.argv[2]]
    from dugup.protocols import Limits
    from dugup.sandbox import SubprocessSandbox
    box = SubprocessSandbox(extra_paths=[sys.argv[2]])
    threading.Thread(target=lambda: box.run("sandbox_jobs:family", {"marker": sys.argv[3]}, Limits(600)), daemon=True).start()
    threading.Event().wait()      # until this process is killed from outside
""")


def test_workers_do_not_outlive_a_program_that_is_killed(marker):
    """The program holding the sandbox is ended with no chance to clean up, as a crash or the task manager would."""
    parent = subprocess.Popen([sys.executable, "-c", PARENT, SRC, TESTS, marker], stdin=subprocess.DEVNULL)
    try:
        worker, child, grandchild = read_marker(marker)
        assert alive(worker)
    finally:
        parent.kill()
        parent.wait(WAIT)
    wait_gone([worker], "the worker to end after its program was killed")
    if os.name == "nt":   # the Job Object takes the whole family; elsewhere only the worker is promised
        wait_gone([child, grandchild], "the worker's family to end after its program was killed")
    else:
        for pid in (child, grandchild):   # tidy up what this system does not promise to end
            try:
                os.kill(pid, 9)
            except OSError:
                pass
