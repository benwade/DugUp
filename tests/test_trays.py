"""One DugUp in the tray, the newest version (trays.py).

Every tray here is a fake that answers through a fake summon, so nothing depends on time: a tray asked to quit
keeps answering until the test lets it go, and "still running after 30 seconds" is a fake clock, not a wait.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dugup import serverprocs, trays
from dugup.datadir import ProgramHome
from dugup.trays import candidate_roots, is_newer, is_tray, settle, survey, version_key, wait_gone

INSTALLED = r"C:\Users\a\AppData\Local\Programs\DugUp\python\DugUp.exe"
CHECKOUT_VENV = r"D:\Claude\projects\digupwin-app\.venv\Scripts\python.exe"


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


class FakeTray:
    def __init__(self, version="0.2.8", leaves_after=0):
        self.version = version            # None: a tray too old to know "version" (it answers "no")
        self.leaves_after = leaves_after  # pings answered after "quit" before it is gone; None: it never goes
        self.asked = []
        self.quitting = False
        self.gone = False

    def answer(self, command):
        if self.gone:
            return None
        self.asked.append(command)
        if command == "ping":
            if self.quitting:
                if self.leaves_after is not None and self.leaves_after <= 0:
                    self.gone = True
                    return None
                if self.leaves_after is not None:
                    self.leaves_after -= 1
            return "pong"
        if command == "version":
            return self.version or "no"
        if command == "quit":
            self.quitting = True
        return "ok"


class Desk:
    """Trays by data folder, and the summon that reaches them."""

    def __init__(self, **trays_by_root):
        self.trays = {_key(root): tray for root, tray in trays_by_root.items()}

    def put(self, root, tray):
        self.trays[_key(root)] = tray
        return tray

    def summon(self, root, command):
        tray = self.trays.get(_key(root))
        return tray.answer(command) if tray is not None else None


def homes(**by_folder):
    """home_of for the tests: a program folder -> the ProgramHome its marker gives (data beside it)."""
    table = {_key(folder): home for folder, home in by_folder.items()}
    return lambda folder: table.get(_key(folder))


def home(folder, data):
    return ProgramHome(Path(folder), Path(data))


@pytest.fixture
def folders(tmp_path):
    own = tmp_path / "checkout-data"
    other = tmp_path / "installed-data"
    per_user = tmp_path / "per-user"
    for f in (own, other, per_user):
        f.mkdir()
    return own, other, per_user


# ---- versions ---------------------------------------------------------------------------------------------------

def test_versions_sort_the_way_releases_follow_each_other():
    order = ["0.2.8", "0.2.9.dev0", "0.2.9.dev3", "0.2.9a1", "0.2.9b2", "0.2.9rc1", "0.2.9", "0.2.10", "1.0"]
    keys = [version_key(v) for v in order]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert version_key("0.2.9") == version_key("0.2.9.0")
    assert version_key("v0.2.8-beta") > version_key("0.2.7"), "a tag's spelling reads too"


def test_a_tray_that_cannot_say_its_version_counts_as_older():
    assert version_key("no") is None and version_key("") is None and version_key(None) is None
    assert is_newer("0.2.9.dev0", None) and is_newer("0.2.9.dev0", "no")
    assert not is_newer("0.2.9", "0.2.9"), "the same version is not newer: the running tray stays"
    assert not is_newer("0.2.8", "0.2.9.dev0")
    assert not is_newer(None, None)


# ---- reading command lines --------------------------------------------------------------------------------------

def test_only_a_command_line_that_runs_a_tray_counts():
    split = serverprocs.split_command_line
    assert is_tray(split(f'"{INSTALLED}" -m dugup tray'))
    assert is_tray(split(f'"{CHECKOUT_VENV}" -m dugup tray --data-dir "D:\\x y"'))
    assert is_tray(split(r"C:\x\Scripts\dugup.exe tray --no-icon"))
    assert not is_tray(split(f'"{INSTALLED}" -m dugup tray --quit')), "asking a tray to quit is not a tray"
    assert not is_tray(split(f'"{INSTALLED}" -m dugup tray --startup refresh'))
    assert not is_tray(split(f'"{INSTALLED}" -m dugup tray --startup=add'))
    assert not is_tray(split(f'"{INSTALLED}" -m dugup window'))
    assert not is_tray(split("python -m other tray"))
    query = split('powershell -Command "' + serverprocs.windows_query(trays.TRAY_LIKE) + '"')
    assert not is_tray(query), "the listing itself is never taken for a tray"


def test_a_trays_data_folder_is_the_one_given_or_beside_its_program_then_the_per_user_one(folders):
    own, other, per_user = folders
    parts = serverprocs.split_command_line(f'"{INSTALLED}" -m dugup tray --data-dir "{other}"')
    assert candidate_roots(parts, per_user, homes()) == [Path(os.path.abspath(other))]
    program_folder = os.path.dirname(INSTALLED) if os.name == "nt" else INSTALLED.rsplit("\\", 1)[0]
    found = homes(**{program_folder: home(program_folder, other)})
    parts = serverprocs.split_command_line(f'"{INSTALLED}" -m dugup tray')
    assert [_key(r) for r in candidate_roots(parts, per_user, found)] == [_key(other), _key(per_user)]
    assert [_key(r) for r in candidate_roots(parts, per_user, homes())] == [_key(per_user)], \
        "no marker found: the per-user default, as the copy itself would choose"


def test_a_note_beside_the_program_that_keeps_an_older_folder_is_followed(folders, tmp_path):
    """Ben's checkout: its data folder holds use-folder.txt, which sends it to the per-user folder."""
    own, other, per_user = folders
    beside = tmp_path / "checkout" / "data"
    beside.mkdir(parents=True)
    (beside / "use-folder.txt").write_text(str(other) + "\n", encoding="utf-8")
    program_folder = str(tmp_path / "checkout" / ".venv" / "Scripts")
    found = homes(**{program_folder: home(tmp_path / "checkout", beside)})
    parts = [os.path.join(program_folder, "python.exe"), "-m", "dugup", "tray"]
    assert _key(candidate_roots(parts, per_user, found)[0]) == _key(other)


# ---- what a starting tray does ----------------------------------------------------------------------------------

def ended(pid):
    return lambda: False


def _rows(*lines):
    return lambda: [(100 + i, line) for i, line in enumerate(lines)]


def test_nothing_else_running_the_tray_starts_and_says_nothing(folders):
    own, other, per_user = folders
    desk = Desk()
    settled = settle(own, "0.2.9.dev0", desk.summon, listing=_rows(), per_user=lambda: per_user, home_of=homes(),
                     own_pid=1, watch=ended)
    assert settled.start and settled.lines() == []


def test_the_tray_on_this_folder_of_the_same_version_stays_and_no_process_is_listed(folders):
    own, other, per_user = folders
    desk = Desk()
    here = desk.put(own, FakeTray("0.2.9.dev0"))
    listed = []
    settled = settle(own, "0.2.9.dev0", desk.summon, listing=lambda: listed.append(1) or [], own_pid=1)
    assert not settled.start and settled.brought_up.root == own
    assert here.asked[-1] == "popup" and "quit" not in here.asked
    assert listed == [], "the usual case (a shortcut clicked again) costs one question, no listing"
    assert "is already in the tray" in settled.lines()[0]


def test_an_older_tray_on_this_folder_is_closed_and_this_one_takes_over(folders):
    own, other, per_user = folders
    desk = Desk()
    old = desk.put(own, FakeTray(None, leaves_after=2))      # 0.2.8 and before cannot say their version
    settled = settle(own, "0.2.9.dev0", desk.summon, listing=_rows(), per_user=lambda: per_user, home_of=homes(),
                     own_pid=1, watch=ended)
    assert settled.start and old.gone and "quit" in old.asked
    assert settled.lines() == [f"Closed an earlier DugUp ({own}) and took over the tray."]


def test_an_older_copy_elsewhere_is_closed_gently_and_this_one_takes_over(folders):
    own, other, per_user = folders
    program_folder = str(Path(other).parent / "DugUp" / "python")
    desk = Desk()
    installed = desk.put(other, FakeTray("0.2.8", leaves_after=3))
    program = os.path.join(program_folder, "DugUp.exe")
    settled = settle(own, "0.2.9.dev0", desk.summon, listing=_rows(f'"{program}" -m dugup tray'),
                     per_user=lambda: per_user, home_of=homes(**{program_folder: home(program_folder, other)}),
                     own_pid=1, watch=ended)
    assert settled.start
    assert installed.asked[:3] == ["ping", "version", "quit"], "asked, then told to quit the usual way"
    assert installed.gone, "waited until it had gone before taking over"
    assert settled.lines() == [f"Closed DugUp 0.2.8 ({program}) and took over the tray."]


@pytest.mark.parametrize("theirs", ["0.2.9", "0.2.9.dev0"])
def test_a_newer_or_equal_copy_elsewhere_keeps_the_tray_and_its_search_box_comes_up(folders, theirs):
    own, other, per_user = folders
    desk = Desk()
    installed = desk.put(other, FakeTray(theirs))
    settled = settle(own, "0.2.9.dev0", desk.summon,
                     listing=_rows(f'"{INSTALLED}" -m dugup tray --data-dir "{other}"'),
                     per_user=lambda: per_user, home_of=homes(), own_pid=1)
    assert not settled.start and settled.brought_up.root == Path(os.path.abspath(other))
    assert installed.asked[-1] == "popup" and "quit" not in installed.asked
    assert settled.lines()[0].startswith(f"DugUp {theirs} ({INSTALLED}) is already in the tray, so this copy "
                                         "(DugUp 0.2.9.dev0) did not start a second one.")


def test_an_older_tray_that_will_not_quit_stops_this_one_from_starting(folders):
    own, other, per_user = folders
    desk = Desk()
    stuck = desk.put(other, FakeTray("0.2.8", leaves_after=None))
    now = [0.0]
    settled = settle(own, "0.2.9.dev0", desk.summon,
                     listing=_rows(f'"{INSTALLED}" -m dugup tray --data-dir "{other}"'),
                     per_user=lambda: per_user, home_of=homes(), own_pid=1,
                     gone=lambda root, alive: wait_gone(root, desk.summon, alive, timeout=30,
                                                        sleep=lambda s: now.__setitem__(0, now[0] + s),
                                                        clock=lambda: now[0]), watch=ended)
    assert not settled.start and settled.stuck and not stuck.gone
    assert "was asked to quit but is still running, so this tray did not start" in settled.lines()[0]


def test_a_tray_that_cannot_be_reached_is_named_never_ended(folders):
    own, other, per_user = folders
    desk = Desk()                         # it is in the process list, but no folder it might use answers
    settled = settle(own, "0.2.9.dev0", desk.summon, listing=_rows(f'"{INSTALLED}" -m dugup tray'),
                     per_user=lambda: per_user, home_of=homes(), own_pid=1, platform="win32")
    assert settled.start
    assert settled.lines() == ["Another DugUp tray may be running that this one cannot reach (process 100). If the "
                               "search key does nothing, end it (Stop-Process -Id 100), then start DugUp again."]


def test_this_tray_and_the_venv_launcher_that_started_it_are_not_taken_for_others(folders):
    own, other, per_user = folders
    desk = Desk()
    line = f'"{CHECKOUT_VENV}" -m dugup tray --data-dir "{own}"'
    rows = [(1, line), (2, line)]          # 2: the venv's launcher, which shares this tray's command line
    others, unreached = survey(Path(own), 1, rows, per_user, desk.summon, homes())
    assert others == [] and unreached == []


def test_a_venv_launcher_and_the_python_it_starts_are_one_tray(folders):
    own, other, per_user = folders
    desk = Desk()
    desk.put(other, FakeTray("0.2.8"))
    line = f'"{CHECKOUT_VENV}" -m dugup tray --data-dir "{other}"'
    others, unreached = survey(Path(own), 1, [(10, line), (11, line)], per_user, desk.summon, homes())
    assert len(others) == 1 and others[0].pids == [10, 11] and others[0].version == "0.2.8"


def test_when_the_processes_cannot_be_listed_only_this_folder_is_checked(folders):
    own, other, per_user = folders
    desk = Desk()
    settled = settle(own, "0.2.9.dev0", desk.summon, listing=lambda: None, own_pid=1)
    assert settled.start and settled.lines() == []


def test_waiting_for_a_tray_to_go_asks_until_it_stops_answering():
    """No delay is relied on: the fake tray goes only when the test lets it, after two more answers."""
    desk = Desk()
    tray = desk.put("x", FakeTray("0.2.8", leaves_after=2))
    tray.quitting = True
    slept = []
    assert wait_gone(Path("x"), desk.summon, sleep=slept.append, clock=lambda: 0.0)
    assert len(slept) == 2 and tray.gone


def test_a_tray_counts_as_gone_only_when_its_process_has_ended_too():
    """It stops answering before its process ends (the search key is let go on the way out), so waiting stops
    only when both are true. The fake process ends only when the test says so."""
    desk = Desk()
    tray = desk.put("x", FakeTray("0.2.8", leaves_after=0))
    tray.quitting = True
    running = {"yes": 3}

    def alive():
        running["yes"] -= 1
        return running["yes"] >= 0

    slept = []
    assert wait_gone(Path("x"), desk.summon, [alive], sleep=slept.append, clock=lambda: 0.0)
    assert tray.gone and len(slept) == 3, "kept waiting after the tray went quiet, until its process ended"


def test_the_trays_own_process_is_watched_from_before_the_quit_and_names_its_program(folders):
    """Only the process the tray wrote into its address file is waited for: a shell whose command line names the
    tray (or a venv's launcher) is not the tray, and waiting on it could last as long as the shell."""
    own, other, per_user = folders
    desk = Desk()
    desk.put(other, FakeTray("0.2.8", leaves_after=0))
    order = []

    def watch(pid):
        order.append(("watch", pid))
        return lambda: False

    real = desk.summon
    desk.summon = lambda root, command: (order.append(("ask", command)), real(root, command))[1]
    shell = f'bash -c python -m dugup tray --data-dir "{other}"'
    line = f'"{INSTALLED}" -m dugup tray --data-dir "{other}"'
    settled = settle(own, "0.2.9.dev0", desk.summon, listing=lambda: [(10, shell), (11, line)],
                     per_user=lambda: per_user, home_of=homes(), own_pid=1, watch=watch, pid_of=lambda root: 11)
    assert [w for w in order if w[0] == "watch"] == [("watch", 11)]
    assert order.index(("watch", 11)) < order.index(("ask", "quit"))
    assert settled.lines() == [f"Closed DugUp 0.2.8 ({INSTALLED}) and took over the tray."]
