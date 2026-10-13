"""The folder picker's window (pickerwindow.py) and its place in the main window."""

import gc

import pytest

import helpers
from dugup.pickerwindow import IN, OUT, PLACEHOLDER
from dugup.shapes import LEVEL_MEANING, LEVEL_NAMES, LEVEL_WORDS
from test_window import Bench



@pytest.fixture(autouse=True)
def windows_are_collected_on_this_thread():
    """After every test here, whatever the test left behind is collected now, on the main thread. A window
    holds tkinter variables in reference cycles; left to chance they are collected during a later test's
    background run, on its thread, which tkinter does not allow (a warning on Windows, and once a hang)."""
    yield
    gc.collect()

@pytest.fixture
def bench(tmp_path):
    made = Bench(tmp_path, pick=True)
    docs = made.docs
    for folder in ("Books", "Trash", "Proj/.venv", "Letters/2019"):
        (docs / folder).mkdir(parents=True)
    (docs / "Books" / "novel.txt").write_text("a novel with a needle", encoding="utf-8")
    (docs / "Trash" / "old.txt").write_text("an old needle", encoding="utf-8")
    (docs / "Proj" / ".venv" / "pyvenv.cfg").write_text("home = x", encoding="utf-8")
    (docs / "Proj" / "readme.txt").write_text("project needle", encoding="utf-8")
    (docs / "Letters" / "2019" / f"{helpers.CAFE}.txt").write_text("letter needle", encoding="utf-8")
    yield made
    made.close()
    made.app = None
    import gc
    gc.collect()


def open_picker(bench):
    bench.app.add_folder()
    bench.finish_run()
    picker = bench.app.picker
    assert picker is not None and not picker.closed, bench.app.message.get()
    return picker


def row(picker, rel):
    return dict(zip(("tick", "words", "ocr", "meaning", "alike", "holds", "cost", "why"), picker.tree.item("n:" + rel, "values")))


def marks(picker, rel):
    """The four choices of a row as a string of ticks, for example "x-xx"."""
    r = row(picker, rel)
    return "".join("x" if r[key] == IN else "-" if r[key] == OUT else " " for key in ("words", "ocr", "meaning", "alike"))


def test_adding_a_folder_opens_the_picker_with_the_guesses_and_adds_nothing_yet(bench):
    picker = open_picker(bench)
    assert bench.service.list_roots() == [], "nothing is added until Apply"
    assert picker.where.get() == str(bench.docs)
    assert picker.tree.get_children("") == ("n:",) and picker.tree.item("n:", "text") == str(bench.docs), \
        "the first row is the folder itself"
    shown = [picker.tree.item(i, "text") for i in picker.tree.get_children("n:")]
    assert set(shown) == {"Books", "Trash", "Proj", "Letters", "sub"} and ".venv" not in shown
    assert [str(picker.tree.heading(key, "text")) for key in ("tick", "words", "ocr", "meaning", "alike")] == \
        ["Include", "Words", "Scans", "Meaning*", "Pictures alike*"]
    assert row(picker, "Trash")["tick"] == OUT and "trash" in row(picker, "Trash")["why"] and marks(picker, "Trash") == "    "
    assert row(picker, "Books")["tick"] == IN and marks(picker, "Books") == "----", "a book library: names only"
    assert "book library" in row(picker, "Books")["why"]
    assert marks(picker, "Letters") == "xxxx" and "1 file(s)" in row(picker, "Letters")["holds"]
    assert "1 file(s)" in str(picker.tool_box.cget("text")) and picker.skip_tool.get() is True
    assert "4 folder(s) in, 1 out" in picker.total.get() and "rough estimate" in picker.total.get()
    assert "starting guesses" in picker.message.get() and "remembered now" in picker.note.get()
    assert "Choose what to index" in bench.app.progress_text.get()


def test_ticks_choices_and_the_total_follow_clicks(bench):
    picker = open_picker(bench)
    before = picker.total.get()
    picker.toggle("Letters")
    assert row(picker, "Letters")["tick"] == OUT and row(picker, "Letters")["cost"] == "" and "3 folder(s) in, 2 out" in picker.total.get()
    assert "out" in picker.tree.item("n:Letters", "tags")
    picker.toggle_option("Letters", "ocr")
    assert "Tick its \"Include\" box first" in picker.message.get() and marks(picker, "Letters") == "    "
    picker.toggle("Letters")
    assert picker.total.get() == before
    picker.toggle_option("Books", "alike")
    assert marks(picker, "Books") == "---x" and "Books: Pictures alike." in picker.message.get()
    assert "differs from the folder above" in row(picker, "Books")["why"], "once changed by hand, the row says it has its own choice"
    picker.toggle_option("Books", "ocr")
    assert marks(picker, "Books") == "xx-x", "Scans brings Words with it"
    picker.toggle_option("Books", "words")
    assert marks(picker, "Books") == "---x", "without Words there are no Scans"
    picker.tree.focus("n:Trash")
    picker._selected()
    assert picker.message.get() == "Trash: a trash folder."
    picker.skip_tool.set(False)
    picker._tool_changed()
    assert picker.plan.skip_tool is False


def test_folders_inside_follow_the_folder_above_until_one_is_changed(bench):
    picker = open_picker(bench)
    assert picker.tree.get_children("n:Letters") == (PLACEHOLDER + "n:Letters",)
    assert picker.tree.get_children("n:Trash") == (), "nothing inside to open"
    picker.open("Letters")
    assert picker.tree.get_children("n:Letters") == ("n:Letters/2019",)
    assert row(picker, "Letters/2019")["why"] == "follows the folder above" and marks(picker, "Letters/2019") == "xxxx"
    picker.toggle_option("Letters", "ocr")                      # the whole folder, and so the folder inside
    assert marks(picker, "Letters") == "x-xx" and marks(picker, "Letters/2019") == "x-xx"
    picker.toggle_option("Letters/2019", "ocr")                 # then just the one inside
    assert marks(picker, "Letters/2019") == "xxxx" and marks(picker, "Letters") == "x-xx"
    assert "its own choice" in row(picker, "Letters/2019")["why"]

    picker.tree.focus("n:Letters/2019")
    picker.follow_above()
    assert marks(picker, "Letters/2019") == "x-xx" and "follows the folder above again" in picker.message.get()
    picker.follow_above()
    assert "already follows the folder above" in picker.message.get()

    picker.toggle_option("Letters/2019", "alike")
    picker.tree.focus("n:Letters")
    picker.apply_inside()
    assert marks(picker, "Letters/2019") == "x-xx" and "1 folder(s) inside Letters had their own choice" in picker.message.get()
    picker.apply_inside()
    assert "already follows it" in picker.message.get()
    picker.tree.focus("")
    picker.apply_inside()
    assert picker.message.get() == "Select a folder first."

    picker.toggle("Letters")
    assert marks(picker, "Letters/2019") == "    " and "out" in picker.tree.item("n:Letters/2019", "tags")
    assert row(picker, "Letters/2019")["tick"] == OUT, "shown as left out, because Letters is"


def test_apply_saves_the_choices_and_indexing_follows_them(bench):
    picker = open_picker(bench)
    picker.toggle("sub")
    picker.apply()
    assert picker.closed and picker.applied
    root = bench.service.list_roots()[0]
    assert "/Trash" in root.exclude and "/sub" in root.exclude and dict(root.levels) == {"Books": LEVEL_NAMES}
    assert "Saved the choices" in bench.app.message.get() and str(bench.docs) in bench.app.folder_box.cget("values")
    bench.app.start("index")
    bench.finish_run()
    found = bench.find("needle")
    assert "readme.txt" in found and f"{helpers.CAFE}.txt" in found and "alpha.txt" in found
    assert "old.txt" not in found and "gamma.txt" not in found and "novel.txt" not in found
    assert bench.find("novel") == ["novel.txt"]


def test_cancel_changes_nothing_and_start_over_brings_the_guesses_back(bench):
    picker = open_picker(bench)
    picker.toggle("Books")
    picker.toggle("Trash")
    picker.start_over()
    assert row(picker, "Books")["tick"] == IN and row(picker, "Trash")["tick"] == OUT and "starting guesses" in picker.message.get()
    picker.close()
    assert bench.service.list_roots() == [] and picker.closed and not picker.applied


def test_an_indexed_folder_can_be_opened_again_and_changed(bench):
    open_picker(bench).apply()
    bench.app.start("index")
    bench.finish_run()
    assert "old.txt" not in bench.find("needle")
    bench.app.choose_what()
    bench.finish_run()
    picker = bench.app.picker
    assert "already indexed" in picker.message.get() and row(picker, "Trash")["tick"] == OUT
    picker.toggle("Trash")
    picker.toggle("Letters")
    picker.apply()
    assert f"{helpers.CAFE}.txt" not in bench.find("needle"), "Letters left the index at once"
    bench.app.start("index")
    bench.finish_run()
    assert "old.txt" in bench.find("needle")


def test_what_cannot_be_done_is_said(bench):
    bench.app.choose_what()
    assert "no folder yet" in bench.app.message.get().lower() and bench.app.worker is None
    bench.folder_to_add = str(bench.docs / "missing")
    bench.app.add_folder()
    bench.finish_run()
    assert bench.app.picker is None and "could not be looked through" in bench.app.message.get()
    bench.folder_to_add = str(bench.docs)
    picker = open_picker(bench)
    picker.on_apply = lambda plan: "That could not be done: the disk is full"
    picker.apply()
    assert not picker.closed and "disk is full" in picker.message.get(), "a refused Apply leaves the picker open"
    bench.app.close()
    assert picker.closed, "closing the main window closes the picker"


def test_the_first_row_is_the_folder_itself_and_sets_everything_inside_that_has_no_choice_of_its_own(bench):
    picker = open_picker(bench)
    assert marks(picker, "") == "xxxx" and row(picker, "")["tick"] == IN and "the folder itself" in row(picker, "")["why"]
    assert picker.tree.item("n:", "open"), "it starts opened, so the folders inside are in view"
    picker.toggle_option("", "ocr")
    assert marks(picker, "") == "x-xx" and marks(picker, "Letters") == "x-xx", "a folder with no choice of its own follows"
    assert marks(picker, "Books") == "----", "a folder that started with its own guess keeps it"
    assert "The whole folder: Words in documents, meaning, pictures alike." in picker.message.get()
    picker.toggle("")
    assert row(picker, "")["tick"] == IN and "stays in" in picker.message.get(), "the folder being chosen cannot be unticked"
    picker.tree.focus("n:")
    picker.follow_above()
    assert "nothing above it to follow" in picker.message.get()
    picker.apply_inside()
    assert marks(picker, "Books") == "x-xx" and row(picker, "Trash")["tick"] == IN, "from the top row: every folder inside follows again"
    assert "inside this folder had their own choice" in picker.message.get()
    picker.apply()
    root = bench.service.list_roots()[0]
    assert root.levels == (("", "words+meaning+alike"),) and "/Trash" not in root.exclude


def test_a_look_through_a_folder_says_how_far_it_has_got(bench, monkeypatch):
    import threading
    started, release = threading.Event(), threading.Event()
    real = type(bench.service).plan_folder

    def held(self, path, stop=None):
        started.set()
        assert release.wait(30)                 # the look is held until the test has read the progress line
        return real(self, path, stop)

    monkeypatch.setattr(type(bench.service), "plan_folder", held)
    monkeypatch.setattr(type(bench.service), "looking_at", lambda self: {"files": 48213, "sorting": False})
    bench.app.add_folder()
    assert started.wait(30)
    text = bench.app.looking_text()
    assert "48,213 file(s) found" in text and "no file is opened" in text and "a whole drive can take a few minutes" in text and "If you cannot wait, press Stop" in text
    assert str(bench.docs) in text and "so that you can choose what to include" in text
    waiting = bench.app.waiting
    assert waiting is not None and not waiting.closed, "a wait is shown in a window of its own, not only in a line"
    assert waiting.window.title() == "Getting ready - please wait" and str(bench.docs) in waiting.text.get()
    bench.app._poll()
    assert "48,213 file(s) found" in waiting.text.get(), "and it is kept up to date"
    monkeypatch.setattr(type(bench.service), "looking_at", lambda self: {"files": 48213, "sorting": True})
    assert "sorting them into folders" in bench.app.looking_text()
    release.set()
    bench.finish_run()
    assert waiting.closed and bench.app.waiting is None, "it goes away when the look is done"
    assert bench.app.picker is not None and not bench.app.picker.closed


def test_stop_in_the_waiting_window_stops_the_look_and_a_failure_is_said_in_a_box(bench, monkeypatch):
    import threading
    started = threading.Event()
    real = type(bench.service).plan_folder

    def held(self, path, stop=None):
        started.set()
        assert stop.wait(30)                    # the look goes on until it is asked to stop
        return real(self, path, stop)

    monkeypatch.setattr(type(bench.service), "plan_folder", held)
    bench.app.add_folder()
    assert started.wait(30)
    bench.app.waiting.stop()
    assert bench.app.stop.is_set() and bench.app.waiting.text.get() == "Stopping ..."
    bench.finish_run()
    assert bench.app.waiting is None and bench.app.picker is None and bench.told == [], "stopping is not a failure"

    monkeypatch.setattr(type(bench.service), "plan_folder", real)
    bench.folder_to_add = str(bench.docs / "missing")
    bench.app.add_folder()
    bench.finish_run()
    assert len(bench.told) == 1 and bench.told[0][0] == "A folder could not be looked through"
    assert str(bench.docs / "missing") in bench.told[0][1], "what must not be missed is said in a box of its own"


def test_a_folder_that_is_not_included_shows_everything_inside_it_as_not_included(bench):
    picker = open_picker(bench)
    picker.open("Letters")
    assert row(picker, "Letters/2019")["tick"] == IN
    picker.toggle("Letters")
    assert row(picker, "Letters")["tick"] == OUT and row(picker, "Letters/2019")["tick"] == OUT, \
        "what is inside a folder that is left out is left out, and is shown so"
    picker.toggle("Letters/2019")
    assert "A folder above this one is not included" in picker.message.get() and row(picker, "Letters/2019")["tick"] == OUT
    picker.toggle("Letters")
    assert row(picker, "Letters")["tick"] == IN and row(picker, "Letters/2019")["tick"] == IN
