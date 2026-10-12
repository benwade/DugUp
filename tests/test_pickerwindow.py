"""The folder picker's window (pickerwindow.py) and its place in the main window."""

import gc

import pytest

import helpers
from dugup.picker import LEVEL_LABELS
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
    return dict(zip(("tick", "level", "holds", "cost", "why"), picker.tree.item("n:" + rel, "values")))


def test_adding_a_folder_opens_the_picker_with_the_guesses_and_adds_nothing_yet(bench):
    picker = open_picker(bench)
    assert bench.service.list_roots() == [], "nothing is added until Apply"
    assert picker.where.get() == str(bench.docs)
    shown = [picker.tree.item(i, "text") for i in picker.tree.get_children("")]
    assert set(shown) == {"Books", "Trash", "Proj", "Letters", "sub"} and ".venv" not in shown
    assert row(picker, "Trash")["tick"] == OUT and "trash" in row(picker, "Trash")["why"] and row(picker, "Trash")["level"] == ""
    assert row(picker, "Books")["tick"] == IN and row(picker, "Books")["level"] == LEVEL_LABELS[LEVEL_NAMES]
    assert "book library" in row(picker, "Books")["why"]
    assert row(picker, "Letters")["level"] == LEVEL_LABELS[LEVEL_MEANING] and "1 file(s)" in row(picker, "Letters")["holds"]
    assert "1 file(s)" in str(picker.tool_box.cget("text")) and picker.skip_tool.get() is True
    assert "4 folder(s) in, 1 out" in picker.total.get() and "rough estimate" in picker.total.get()
    assert "starting guesses" in picker.message.get()
    assert "Choose what to index" in bench.app.progress_text.get()


def test_ticks_levels_and_the_total_follow_clicks(bench):
    picker = open_picker(bench)
    before = picker.total.get()
    picker.toggle("Letters")
    assert row(picker, "Letters")["tick"] == OUT and row(picker, "Letters")["cost"] == "" and "3 folder(s) in, 2 out" in picker.total.get()
    assert "out" in picker.tree.item("n:Letters", "tags")
    picker.step_level("Letters")
    assert "Tick its box first" in picker.message.get() and row(picker, "Letters")["level"] == ""
    picker.toggle("Letters")
    assert picker.total.get() == before
    picker.step_level("Books")
    assert row(picker, "Books")["level"] == LEVEL_LABELS[LEVEL_WORDS] and row(picker, "Books")["why"] == "", \
        "once changed by hand, the starting reason no longer describes the row"
    picker.tree.focus("n:Trash")
    picker._selected()
    assert picker.message.get() == "Trash: a trash folder."
    picker.tree.focus("n:Books")
    picker._selected()
    assert picker.message.get() == "Books: Words.", "the full reason, or the present choice, of the selected row"
    picker.skip_tool.set(False)
    picker._tool_changed()
    assert picker.plan.skip_tool is False


def test_folders_inside_are_drawn_when_opened_and_follow_the_folder_above(bench):
    picker = open_picker(bench)
    assert picker.tree.get_children("n:Letters") == (PLACEHOLDER + "n:Letters",)
    assert picker.tree.get_children("n:Trash") == (), "nothing inside to open"
    picker.open("Letters")
    assert picker.tree.get_children("n:Letters") == ("n:Letters/2019",)
    assert "(as above)" in row(picker, "Letters/2019")["level"]
    picker.step_level("Letters/2019")
    assert row(picker, "Letters/2019")["level"] == LEVEL_LABELS[LEVEL_NAMES], "Everything steps round to Names only"
    picker.toggle("Letters")
    assert row(picker, "Letters/2019")["level"] == "" and "out" in picker.tree.item("n:Letters/2019", "tags")
    assert row(picker, "Letters/2019")["tick"] == IN, "its own box is still ticked; it is out because Letters is"


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
