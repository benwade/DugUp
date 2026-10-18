"""Report a problem (report.py, reportwindow.py) and the freeze recorder (watchdog.py)."""

import io
import threading
import urllib.parse
from pathlib import Path

import pytest

from dugup import cli, report, watchdog


# ---- what the report holds, and what it hides --------------------------------------------------------------

def test_the_user_name_and_home_folder_are_replaced_only_where_they_name_a_place():
    text = r"C:\Users\Alice\Docs, D:\Backup\alice\x.txt, /home/alice/y and the root folder alice likes"
    out = report.redact(text, home=r"C:\Users\alice", user="alice")
    assert out.startswith(r"%USERPROFILE%\Docs") and r"D:\Backup\%USERNAME%\x.txt" in out and "/home/%USERNAME%/y" in out
    assert out.endswith("the root folder alice likes"), "the same letters outside a path are left alone"
    assert report.redact("INFO root scanned", home="/root", user="root") == "INFO root scanned"


def test_the_report_says_what_it_holds_and_gathers_the_log_and_freeze_notes(tmp_path):
    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    (logs / "dugup.log").write_text("".join(f"line {i}\n" for i in range(100)), encoding="utf-8")
    (logs / "freeze-window-20261018-120000.txt").write_text("the window stopped answering\nThread main: here", encoding="utf-8")
    text = report.gather(str(data), "it froze when I searched", doctor=lambda d: "[  ok] python: 3.13")
    assert text.startswith("> Sent with DugUp's") and "no document text" in text
    assert "it froze when I searched" in text and "[  ok] python: 3.13" in text
    assert "line 99" in text and "line 39" not in text, "the end of the log only"
    assert "## Freeze note: freeze-window-20261018-120000.txt" in text and "stopped answering" in text


def test_a_health_check_that_hangs_does_not_hang_the_report(monkeypatch, tmp_path):
    held = threading.Event()
    from dugup import doctor
    monkeypatch.setattr(doctor, "run_checks", lambda *a, **k: held.wait(30) and [])
    monkeypatch.setattr(report, "DOCTOR_SECONDS", 0.2)
    try:
        assert "did not finish within" in report._doctor(str(tmp_path))
    finally:
        held.set()


def test_the_github_address_carries_the_report_and_says_when_it_was_cut_short():
    url, cut = report.issue_url("short body")
    assert url.startswith(report.ISSUES + "?") and not cut
    assert urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["body"] == ["short body"]
    url, cut = report.issue_url("x" * (report.URL_BODY_CHARS + 50))
    assert cut and "on your clipboard" in urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["body"][0]


def test_the_report_command_prints_it(tmp_path):
    out = io.StringIO()
    assert cli.main(["report", "--print", "--data-dir", str(tmp_path / "data")], stdout=out, environ={}) == 0
    assert "## DugUp" in out.getvalue() and "## Health check" in out.getvalue()


# ---- the freeze recorder -------------------------------------------------------------------------------------

class Loop:
    """A window's event loop that the test runs by hand: ticks happen only when the test says."""

    def __init__(self):
        self.waiting = []

    def after(self, ms, fn):
        self.waiting.append(fn)
        return len(self.waiting)

    def after_cancel(self, job):
        pass

    def turn(self):
        jobs, self.waiting = self.waiting, []
        for job in jobs:
            job()


def test_a_frozen_window_is_written_down_once_with_where_it_was_stuck(tmp_path):
    now = [100.0]
    loop = Loop()
    dog = watchdog.Watchdog(loop, tmp_path / "logs", "window", clock=lambda: now[0], start=False)
    now[0] += 10
    assert dog.check() is None, "slow, not frozen"
    now[0] += 6
    note = dog.check()
    assert note is not None and note.name.startswith("freeze-window-")
    text = note.read_text(encoding="utf-8")
    assert "stopped answering for 16 s" in text and "Thread " in text and "test_report.py" in text
    now[0] += 30
    assert dog.check() is None, "once per freeze"
    loop.turn()                                   # it answers again
    assert dog.check() is None and "answered again" in note.read_text(encoding="utf-8")


def test_only_the_last_ten_notes_are_kept(tmp_path):
    import os
    import time
    for i in range(14):
        path = tmp_path / f"freeze-window-{i:02d}.txt"
        path.write_text("x", encoding="utf-8")
        os.utime(path, (time.time() - 1000 + i, time.time() - 1000 + i))
    watchdog.prune(tmp_path)
    left = sorted(p.name for p in tmp_path.glob("freeze-*.txt"))
    assert left == [f"freeze-window-{i:02d}.txt" for i in range(4, 14)]
    assert [p.name for p in watchdog.latest_notes(tmp_path)] == ["freeze-window-13.txt", "freeze-window-12.txt",
                                                                 "freeze-window-11.txt"]


# ---- the window --------------------------------------------------------------------------------------------

@pytest.fixture
def root():
    from test_window import make_root
    return make_root()


def test_the_report_window_shows_the_report_and_sends_nothing_by_itself(root, tmp_path):
    from dugup.reportwindow import WHY, ReportWindow
    gathered = threading.Event()
    opened, copied, saved = [], [], tmp_path / "report.md"

    def gather(data_dir, hide_names=True):
        assert gathered.wait(10)                  # held until the test lets it go: never assumed to be quick
        return report.gather(data_dir, doctor=lambda d: r"[  ok] data folder: E:\Clients\Acme Corp\notes.txt",
                             hide_names=hide_names)

    window = ReportWindow(root, str(tmp_path / "data"), open_url=opened.append, copy=copied.append,
                          ask_save=lambda: str(saved), gather=gather)
    try:
        root.update()
        assert not window.ready and "Gathering" in window.text.get("1.0", "end")
        assert "public" in WHY and "leaves out" in WHY and "never your documents' text" in WHY and "folder-01" in WHY
        gathered.set()
        for _ in range(200):
            root.update()
            if window.ready:
                break
            root.after(10)
        assert window.ready and opened == [] and copied == [], "nothing is sent or copied until the person chooses"
        shown = window.text.get("1.0", "end")
        assert "Acme Corp" not in shown and "file-01.txt" in shown, "names are hidden by default"
        window.hide_names.set(False)
        window._show_report()
        assert "Acme Corp" in window.text.get("1.0", "end") and "shown as they are" in window.status.get()
        window.hide_names.set(True)
        window._show_report()
        window.what.insert("1.0", "It froze while indexing")
        body = window.body()
        assert "## What happened\nIt froze while indexing" in body
        window.to_github()
        assert copied == [body] and len(opened) == 1 and "Submit new issue" in window.status.get()
        assert "It+froze+while+indexing" in opened[0] or "It%20froze%20while%20indexing" in opened[0]
        window.save()
        assert saved.read_text(encoding="utf-8") == body
    finally:
        window.close()


# ---- the ways in -------------------------------------------------------------------------------------------

def test_every_window_opens_the_report_as_a_program_of_its_own(tmp_path):
    started = []
    from dugup.datadir import DataLayout
    layout = DataLayout(tmp_path / "data")
    report.launch(layout, started.append)
    command = started[0]
    assert command[1:4] == ["-m", "dugup", "report"] and "--data-dir" in command


def test_the_recorder_writes_while_the_window_thread_is_really_stuck(root, tmp_path):
    """The real thing: a recorder thread, and a window thread that stops turning its event loop."""
    import time
    logs = tmp_path / "logs"
    dog = watchdog.Watchdog(root, logs, "stuck", frozen_after=0.3)
    try:
        deadline = time.monotonic() + 15
        while not list(logs.glob("freeze-stuck-*.txt")) and time.monotonic() < deadline:
            time.sleep(0.05)                      # stuck: this thread never lets the window's loop turn
        notes = list(logs.glob("freeze-stuck-*.txt"))
        assert notes and "test_the_recorder_writes_while_the_window_thread_is_really_stuck" in notes[0].read_text(encoding="utf-8")
    finally:
        dog.stop()



def test_folder_and_file_names_become_stand_ins_that_keep_what_helps():
    hider = report.Hider({"<data folder>": r"D:\Patients\Smith John\DugUp data",
                          "<program folder>": r"C:\Users\a\AppData\Local\Programs\DugUp"},
                         roots=[r"E:\Acme Corp"], drive_type=lambda letter: "removable" if letter == "E" else "fixed")
    text = (r"[  ok] data folder: D:\Patients\Smith John\DugUp data\logs" "\n"
            r"failed: E:\Acme Corp\Contracts\Smith John\Agreement - final #2.docx (could not open)" "\n"
            r'  File "C:\Users\a\AppData\Local\Programs\DugUp\python\Lib\dugup\window.py", line 12' "\n"
            r"see http://example.com/a/b at 10:30/45, and F:\Smith John\x.pdf" "\n")
    out = hider.hide(text)
    for private in ("Patients", "Smith", "Acme", "Contracts", "Agreement"):
        assert private not in out
    assert r"<data folder>\logs" in out and r"<program folder>\python\Lib\dugup\window.py" in out
    assert r"E:[removable]\folder-01\folder-02\folder-03\file-01.docx" in out
    assert "unusual characters" in out and "in indexed folder 01" in out and "depth 5" in out
    assert r"F:[fixed]\folder-03\file-02.pdf" in out, "the same folder name, the same stand-in"
    assert "http://example.com/a/b" in out and "10:30/45" in out, "web addresses and times are not paths"
    # a Windows path runs to the end of the line or to punctuation, since names may hold spaces: words after it
    # on the same line are hidden too, the safe side (stopping early could leave part of a name showing)
    assert "Smith" not in hider.hide(r"copied E:\Acme Corp\v1.2 notes\Smith file.txt and more")
    long = "C:\\" + "\\".join(["deep folder"] * 30) + "\\x.txt"
    assert "longer than Windows' classic limit of 260" in hider.hide(long)
