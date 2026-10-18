"""The administrator note and the notes before the window opens."""

import gc
import io
import types

import pytest

from dugup import cli, datadir, elevated, startnotes
from dugup.datadir import MARKER_NAME, read_marker
from dugup.doctor import INFO, OK, Environment, run_checks
from dugup.elevated import is_elevated as real_is_elevated    # bound before the test fixture stands in for it


def test_the_note_says_it_is_from_dugup_and_that_nothing_is_wrong():
    for text in (elevated.TEXT, elevated.LINE):
        assert "Nothing is wrong" in text and "precaution" in text
        assert "DugUp" in text
    assert elevated.TITLE == "A note from DugUp"


def test_a_command_run_with_administrator_rights_notes_it_and_carries_on(tmp_path, monkeypatch):
    monkeypatch.setattr(elevated, "is_elevated", lambda: True)
    out = io.StringIO()
    code = cli.main(["init", "--data-dir", str(tmp_path / "d")], stdout=out, environ={})
    assert code == 0 and elevated.LINE in out.getvalue() and "Data folder ready" in out.getvalue()


def test_without_administrator_rights_there_is_no_note(tmp_path):
    out = io.StringIO()
    cli.main(["init", "--data-dir", str(tmp_path / "d")], stdout=out, environ={})
    assert elevated.LINE not in out.getvalue()


def test_doctor_reports_it_as_information_not_a_problem():
    on = {c.name: c for c in run_checks(Environment(elevated=lambda: True))}
    off = {c.name: c for c in run_checks(Environment(elevated=lambda: False))}
    assert on["administrator rights"].status == INFO and "Nothing is wrong" in on["administrator rights"].detail
    assert off["administrator rights"].status == OK


def test_the_real_check_answers_yes_or_no_on_this_machine():
    assert isinstance(real_is_elevated(), bool)        # never raises, whatever the platform


# ---- before the window ---------------------------------------------------------------------------------------

def _waiting(tmp_path, monkeypatch):
    program = tmp_path / "DugUp"
    program.mkdir()
    (program / MARKER_NAME).write_text("data = data\n", encoding="utf-8")
    home = read_marker(program / MARKER_NAME)
    per_user = tmp_path / "per-user"
    per_user.mkdir()
    (per_user / "index.sqlite").write_bytes(b"")
    monkeypatch.setattr(datadir, "find_program_home", lambda *a, **k: home)
    monkeypatch.setattr(datadir, "default_data_dir", lambda: per_user)
    return home, per_user


ARGS = types.SimpleNamespace(data_dir=None)


@pytest.fixture(autouse=True)
def windows_are_collected_on_this_thread():
    """As in test_pickerwindow.py: whatever a test's windows leave behind is collected now, on this thread,
    never during a later test's background run."""
    yield
    gc.collect()


def test_no_notes_when_there_is_nothing_to_say(tmp_path):
    shown = []
    got = startnotes.before_window(None, ARGS, {}, asker=lambda *a: shown.append(a), tell=lambda *a: shown.append(a))
    assert got is None and shown == []


def test_the_administrator_note_is_shown_once_and_nothing_else_changes(tmp_path):
    shown = []
    got = startnotes.before_window(None, ARGS, {}, tell=lambda parent, title, text: shown.append((title, text)),
                                   is_elevated=lambda: True)
    assert got is None and shown == [(elevated.TITLE, elevated.TEXT)]


def test_keep_is_remembered(tmp_path, monkeypatch):
    home, per_user = _waiting(tmp_path, monkeypatch)
    got = startnotes.before_window(None, ARGS, {}, asker=lambda *a: startnotes.KEEP)
    assert got == startnotes.KEEP and datadir.kept_elsewhere(home.data) == per_user
    assert startnotes.before_window(None, ARGS, {}, asker=lambda *a: 1 / 0) is None     # not asked again


def test_closing_the_question_asks_again_next_time(tmp_path, monkeypatch):
    home, per_user = _waiting(tmp_path, monkeypatch)
    assert startnotes.before_window(None, ARGS, {}, asker=lambda *a: startnotes.LATER) == startnotes.LATER
    assert datadir.beside_program(None, {}).move_from == per_user


def test_a_failed_move_says_nothing_was_lost_and_offers_again(tmp_path, monkeypatch):
    home, per_user = _waiting(tmp_path, monkeypatch)
    told = []
    got = startnotes.before_window(None, ARGS, {}, asker=lambda *a: startnotes.MOVE,
                                   mover=lambda parent, old, new: "DugUp is running in the tray",
                                   tell=lambda parent, title, text: told.append(text))
    assert got == startnotes.LATER and "Nothing was lost" in told[0]
    assert datadir.beside_program(None, {}).move_from == per_user


def test_a_successful_move_is_quiet(tmp_path, monkeypatch):
    _waiting(tmp_path, monkeypatch)
    told = []
    got = startnotes.before_window(None, ARGS, {}, asker=lambda *a: startnotes.MOVE,
                                   mover=lambda parent, old, new: "", tell=lambda *a: told.append(a))
    assert got == startnotes.MOVE and told == []


# ---- the real dialogs, driven through their buttons -------------------------------------------------------

def _click_when_shown(root, label):
    """Press the button labelled `label` as soon as the question window exists (checked on each idle turn of
    the window loop, so nothing here depends on how fast the machine is)."""
    def look():
        for win in root.winfo_children():
            for widget in win.winfo_children():
                try:
                    if widget.cget("text") == label:
                        widget.invoke()
                        return
                except Exception:
                    pass
        root.after(10, look)
    root.after_idle(look)


def test_the_move_question_answers_with_the_button_pressed(tmp_path):
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        _click_when_shown(root, "Keep it where it is")
        assert startnotes.ask_move(root, tmp_path / "old", tmp_path / "new") == startnotes.KEEP
        _click_when_shown(root, "Move it")
        assert startnotes.ask_move(root, tmp_path / "old", tmp_path / "new") == startnotes.MOVE
    finally:
        root.destroy()


def test_the_move_runs_behind_a_wait_window_and_reports(tmp_path):
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        def mover(old, new, tell, stop):
            tell(5, 10)
            tell(10, 10)
            return types.SimpleNamespace(left_behind="")

        assert startnotes.run_move(root, tmp_path / "old", tmp_path / "new", mover=mover) == ""

        def refusing(old, new, tell, stop):
            from dugup.datamove import MoveError
            raise MoveError("DugUp is running in the tray")

        assert startnotes.run_move(root, tmp_path / "old", tmp_path / "new", mover=refusing) == "DugUp is running in the tray"
    finally:
        root.destroy()


def test_a_note_window_is_shown_and_closes_with_ok(tmp_path):
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        _click_when_shown(root, "OK")
        startnotes.show(root, "DugUp", "Your index was not moved: a test reason.")
        assert not [w for w in root.winfo_children() if isinstance(w, tkinter.Toplevel)], "the note is gone after OK"
    finally:
        root.destroy()
