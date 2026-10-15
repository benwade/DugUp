"""The setup wizard's window (setupwindow.py) and its place in the main window."""

import os
import gc
import threading

import pytest

from dugup import catalogue, settings as settings_module
from dugup.catalogue import Machine
from dugup.setup import SetupPlan
from dugup.places import MAYBE, SYSTEM, YOURS, Place
from dugup.setupwindow import COMPUTER, PLACES, SUMMARY, WELCOME, SetupWizard
from dugup.settings import Settings
from test_window import Bench, make_root

GB = 1024 ** 3
STRONG = Machine(cores=32, free_memory_bytes=35 * GB, free_disk_bytes=550 * GB, card="graphics card (DirectML)", card_speedup=23.0)


@pytest.fixture(autouse=True)
def windows_are_collected_on_this_thread():
    yield
    gc.collect()


OFFERED = [Place("/home/u/Documents", "Documents", True, "your own documents", YOURS),
           Place("/home/u/Pictures", "Pictures", True, "photos and scans", YOURS),
           Place("/home/u/Downloads", "Downloads", False, "mostly installers", MAYBE),
           Place("/usr", "/usr", False, "the system's programs: nothing you wrote lives here", SYSTEM)]


class Stage:
    def __init__(self, first_run=True, machine=None, look="now", settings=None):
        self.root = make_root()
        self.saved = []
        self.answer = ""
        self.release = threading.Event()
        found = machine or STRONG

        def looker(stop):
            if look == "held":
                assert self.release.wait(30)
            return found

        self.plan = SetupPlan(settings or Settings(), first_run=first_run, advanced=not first_run)
        if first_run:
            self.plan.offer(OFFERED)
        self.folder_to_add = ""
        self.wizard = SetupWizard(self.root, self.plan, looker if look else None, self.save,
                                  choose_folder=lambda: self.folder_to_add)

    def save(self, plan):
        if not self.answer:
            self.saved.append(plan.changes())
        return self.answer

    def settle(self):
        """Let a look at the computer that was started come to its end and be taken in."""
        while self.wizard.looking is not None:
            self.wizard.looking.join(0.05)
            self.wizard.drain()
        self.root.update()

    def texts(self):
        out = []
        for child in self.wizard.body.winfo_children():
            if child.winfo_class() == "Text":
                out.append(child.get("1.0", "end").strip())
            elif child.winfo_class() == "TEntry":
                out.append(child.get())
            elif child.winfo_class() in ("TRadiobutton", "TButton"):
                out.append(str(child.cget("text")))
            else:
                for inner in child.winfo_children():
                    if inner.winfo_class() in ("TLabel", "TCheckbutton"):
                        out.append(str(inner.cget("text")))
        return "\n".join(out)

    def close(self):
        self.release.set()
        self.wizard.close()
        self.root.destroy()


@pytest.fixture
def stage():
    made = []

    def make(**options):
        made.append(Stage(**options))
        return made[-1]

    yield make
    for one in made:
        one.close()


def test_the_welcome_page_warns_that_setup_is_heavy_and_temporary_and_offers_to_choose(stage):
    s = stage()
    assert s.wizard.page() == WELCOME and s.wizard.window.title() == "Set up DugUp"
    text = s.texts()
    assert "uses a lot of the computer for a while" in text and "This is temporary" in text
    assert "You do not have to answer any of them" in text
    assert "Set it up for me (recommended)" in text and "Let me choose" in text
    assert str(s.wizard.back_button.cget("state")) == "disabled" and s.wizard.choice.get() == "express"


def test_the_express_path_is_welcome_computer_summary_and_finish_saves_what_suits_the_computer(stage):
    s = stage()
    s.settle()
    s.wizard.next()
    assert s.wizard.page() == COMPUTER
    text = s.texts()
    assert "Processor cores: 32" in text and "23.0 times the processor's speed" in text and "reads none of your files" in text
    assert "The speed test is finished" in text and str(s.wizard.again_button.cget("text")) == "Run the speed test again"
    assert str(s.wizard.skip_button.cget("state")) == "disabled" and str(s.wizard.again_button.cget("state")) == "normal"
    s.wizard.next()
    assert s.wizard.page() == PLACES
    s.wizard.next()
    assert s.wizard.page() == SUMMARY and str(s.wizard.next_button.cget("text")) == "Finish"
    summary = s.wizard.summary_text
    assert "Read on the graphics card: yes  (the graphics card read a sample page 23.0 times faster" in summary
    assert "Spending limit for the index: 56,320 MB  (a tenth of the 550 GB that is free now)" in summary
    assert "Folders to index\n  /home/u/Documents\n  /home/u/Pictures" in summary and "This is temporary" in summary
    assert "looks through each of these folders in turn (opening no file)" in summary and "Nothing is read until you agree" in summary
    assert "Every choice can be changed later under Settings." in summary
    s.wizard.next()
    assert s.saved == [{"ocr_gpu": True, "max_index_mb": 56320}] and s.wizard.finished and s.wizard.closed


def test_choosing_goes_through_one_page_for_each_group_with_rows_from_the_catalogue(stage):
    s = stage()
    s.settle()
    s.wizard.choice.set("choose")
    s.wizard.next()
    s.wizard.next()
    s.wizard.next()
    seen = []
    while s.wizard.page() != SUMMARY:
        key = s.wizard.page()
        group = catalogue.group(key)
        text = s.texts()
        assert s.wizard.heading.get() == group.title and group.about in text
        for entry in catalogue.in_group(key, everyday_only=True):
            assert entry.label in text and entry.help in text and entry.why in text, entry.name
            assert entry.name in s.wizard.widgets
            seen.append(entry.name)
        s.wizard.next()
    assert sorted(seen) == sorted(e.name for e in catalogue.ENTRIES.values() if e.everyday), "every everyday setting, once"
    s.wizard.back()
    assert s.wizard.page() != SUMMARY, "Back works from the summary"


def test_a_change_is_checked_at_once_and_the_hint_follows(stage):
    s = stage()
    s.settle()
    s.wizard.express = False
    s.wizard.at = s.wizard.sequence().index("disk")
    s.wizard.show()
    assert "Chosen for this computer: a tenth of the 550 GB" in s.wizard.hints["max_index_mb"].get()
    s.wizard.widgets["max_index_mb"].set("plenty")
    s.wizard.changed("max_index_mb", "plenty")
    assert "That will not do" in s.wizard.message.get() and s.plan.values["max_index_mb"] == 56320
    s.wizard.changed("max_index_mb", "2000")
    assert s.wizard.message.get() == "" and s.plan.values["max_index_mb"] == 2000
    assert "DugUp would suggest 56,320 MB" in s.wizard.hints["max_index_mb"].get()
    s.wizard.at = s.wizard.sequence().index("pictures")
    s.wizard.show()
    assert s.wizard.widgets["ocr_gpu"].get() is True, "a tick box shows the suggested yes"
    s.wizard.changed("ocr_gpu", "no")
    s.wizard.at = len(s.wizard.sequence()) - 1
    s.wizard.show()
    assert "Read on the graphics card: no  (your choice; DugUp would suggest yes)" in s.wizard.summary_text
    assert "Spending limit for the index: 2,000 MB  (your choice; DugUp would suggest 56,320 MB)" in s.wizard.summary_text


def test_the_speed_test_can_still_be_running_and_can_be_skipped(stage):
    s = stage(look="held")
    s.wizard.next()
    assert s.wizard.page() == COMPUTER and "The speed test is running. It started by itself" in s.texts() and s.wizard.busy()
    assert "not looked at yet" in s.texts(), "nothing is claimed before it is known"
    assert str(s.wizard.skip_button.cget("state")) == "normal" and str(s.wizard.again_button.cget("state")) == "disabled"
    s.wizard.skip_test()
    assert "skipped" in s.wizard.message.get()
    s.release.set()
    s.settle()
    assert s.plan.values["ocr_gpu"] is False, "a skipped test never turns the card on"
    other = stage(look="held")
    other.wizard.next()
    other.release.set()
    other.settle()
    assert other.plan.values["ocr_gpu"] is True and "23.0 times" in other.texts(), "the page is redrawn when the result comes"


def test_a_look_that_fails_is_no_hurdle(stage):
    s = Stage.__new__(Stage)
    s.root, s.saved, s.answer, s.release = make_root(), [], "", threading.Event()

    def broken(stop):
        raise RuntimeError("no such device")

    s.plan = SetupPlan(Settings())
    s.wizard = SetupWizard(s.root, s.plan, broken, s.save)
    s.folder_to_add = ""
    try:
        s.settle()
        assert "usual defaults are used" in s.wizard.message.get()
        for _ in range(4):
            s.wizard.next()
        assert s.saved == [{}] and s.wizard.finished
    finally:
        s.close()


def test_a_save_that_fails_keeps_the_wizard_open_and_cancel_saves_nothing(stage):
    s = stage()
    s.settle()
    s.answer = "That could not be saved: the disk is full"
    s.wizard.at = len(s.wizard.sequence()) - 1
    s.wizard.show()
    s.wizard.next()
    assert not s.wizard.closed and "disk is full" in s.wizard.message.get() and s.saved == []
    s.wizard.close()
    assert s.wizard.closed and not s.wizard.finished and s.saved == []


def test_settings_later_shows_every_setting_as_it_stands_and_changes_nothing_by_itself(stage):
    s = stage(first_run=False, look=None, settings=Settings(near_words=9))
    assert s.wizard.window.title() == "DugUp settings"
    assert s.wizard.sequence() == [g.key for g in catalogue.GROUPS] + [SUMMARY], "no welcome, no test: straight to the settings"
    shown = []
    while s.wizard.page() != SUMMARY:
        shown += list(s.wizard.widgets)
        s.wizard.next()
    assert sorted(shown) == sorted(catalogue.ENTRIES), "every setting has a row"
    assert "Words close together: 9 words" in s.wizard.summary_text
    s.wizard.next()
    assert s.saved == [{}]


# ---- in the main window ---------------------------------------------------------------------------------------

def started(bench, seconds=30.0, until=None):
    """Let the window work until the look it queued has begun (it is queued, so it may take a turn or two)."""
    import time
    end = time.monotonic() + seconds
    until = until or (lambda: bench.app.worker is not None)
    while not until() and time.monotonic() < end:
        bench.root.update()
        time.sleep(0.01)


def test_the_wizard_opens_by_itself_on_a_first_start_and_finishing_saves_and_asks_for_a_folder(tmp_path, monkeypatch):
    from dugup import service as service_module
    monkeypatch.setattr(service_module.LocalService, "look_at_machine", lambda self, stop=None, test_card=True: STRONG)
    bench = Bench(tmp_path, setup=True)
    try:
        wizard = bench.app.wizard
        assert wizard is not None and not wizard.closed and wizard.plan.first_run
        assert bench.app.setting_up and bench.root.state() != "normal", "the main window is out of the way while setting up"
        while wizard.looking is not None:
            wizard.looking.join(0.05)
            wizard.drain()
        wizard.plan.offer([Place(str(bench.docs), "Documents", True, "your own documents", YOURS),
                           Place(str(bench.docs / "sub"), "sub", True, "inside the first", YOURS),
                           Place(str(tmp_path / "nowhere"), "Gone", True, "a folder that cannot be read", MAYBE)])
        for _ in range(4):
            wizard.next()
        assert wizard.finished
        assert bench.service.settings.ocr_gpu is True and bench.service.settings.max_index_mb == 56320
        saved, notes = settings_module.load(bench.layout.config)
        assert saved.ocr_gpu is True and saved.max_index_mb == 56320 and notes == []
        assert bench.app.place_queue == [str(bench.docs), str(tmp_path / "nowhere")], "the folder inside another is covered by it"
        started(bench)                             # finishing queued a look through the first folder
        bench.finish_run()
        picker = bench.app.picker
        assert picker is not None and picker.where.get() == str(bench.docs), "no bare file box: the picker, for the folder chosen"
        assert "1 more folder(s) after this one" in bench.app.message.get()
        assert bench.app.setting_up and bench.root.state() != "normal", "still setting up: folders are left to choose"
        picker.apply()
        assert str(bench.docs) in bench.app.folder_box.cget("values")
        started(bench, until=lambda: not bench.app.place_queue)   # closing the picker moves on to the next folder
        if bench.app.worker is not None:           # (a look that fails at once may already be over)
            bench.finish_run()
        assert "could not be looked through" in bench.app.message.get() and bench.app.place_queue == []
        assert bench.app.worker is None
        assert not bench.app.setting_up and bench.root.state() == "normal", "setting up is over: the main window is back"
    finally:
        bench.close()


def test_closing_the_wizard_without_finishing_brings_the_main_window_back(tmp_path, monkeypatch):
    from dugup import service as service_module
    monkeypatch.setattr(service_module.LocalService, "look_at_machine", lambda self, stop=None, test_card=True: STRONG)
    bench = Bench(tmp_path, setup=True)
    try:
        assert bench.app.setting_up and bench.root.state() != "normal"
        bench.app.wizard.close()
        assert not bench.app.setting_up and bench.root.state() == "normal"
    finally:
        bench.close()


def test_no_wizard_when_there_is_a_folder_already_and_settings_opens_on_request(tmp_path):
    first = Bench(tmp_path)
    first.app.add_folder()
    first.close()
    first.root.destroy()
    bench = Bench.__new__(Bench)
    from dugup import window as w
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    bench.root = make_root()
    bench.service = LocalService(DataLayout(tmp_path / "data").ensure(), Settings())
    bench.app = w.App(bench.root, bench.service, setup=True, pick=False)
    try:
        assert bench.app.wizard is None, "a folder is there: this is not a first start"
        bench.app.open_setup()
        shown = bench.app.wizard
        assert shown.window.title() == "DugUp settings" and shown.look is not None, "the settings window, with a look at the machine on offer"
        shown.changed("near_words", "20")
        shown.save()
        assert bench.service.settings.near_words == 20 and "Saved 1 setting(s)." in bench.app.message.get()
        bench.app.open_setup()
        assert bench.app.wizard is shown and not shown.closed, "opened again: the same window comes to the front"
        shown.close()
        bench.app.open_setup()
        assert bench.app.wizard is not shown and not bench.app.wizard.closed
    finally:
        bench.app.close()


# ---- which folders ---------------------------------------------------------------------------------------------

def test_the_folders_page_lists_places_with_reasons_and_explains_the_cost(stage):
    s = stage()
    s.settle()
    s.wizard.at = s.wizard.sequence().index(PLACES)
    s.wizard.show()
    assert s.wizard.heading.get() == "Which folders"
    assert "Listing file names takes seconds" in s.texts() and "Nothing is read until you have seen that and agreed" in s.texts()
    assert {path: box.get() for path, box in s.wizard.place_boxes.items()} == {
        "/home/u/Documents": True, "/home/u/Pictures": True, "/home/u/Downloads": False, "/usr": False}
    assert "2 folder(s) chosen." in s.wizard.message.get()
    s.wizard.tick_place("/home/u/Documents", False)
    s.wizard.tick_place("/home/u/Pictures", False)
    assert "No folder is ticked" in s.wizard.message.get()
    s.folder_to_add = "/data/Archive"
    s.wizard.add_place()
    added = os.path.normpath("/data/Archive")          # the path is put in this system's own form
    assert s.plan.chosen_places() == [added] and s.wizard.place_boxes[added].get() is True
    s.folder_to_add = ""
    s.wizard.add_place()
    assert s.plan.chosen_places() == [added], "a cancelled folder box adds nothing"
    s.wizard.at = len(s.wizard.sequence()) - 1
    s.wizard.show()
    assert f"Folders to index\n  {added}" in s.wizard.summary_text and "looks through this folder (opening no file)" in s.wizard.summary_text


def test_finishing_with_no_folder_says_what_to_do_next(stage):
    s = stage()
    s.settle()
    for path in list(s.plan.ticked):
        s.plan.ticked[path] = False
    s.wizard.at = len(s.wizard.sequence()) - 1
    s.wizard.show()
    assert "Folders to index\n  none yet" in s.wizard.summary_text and "Add folder..." in s.wizard.summary_text


def test_the_speed_test_can_be_run_again_by_a_button(stage):
    s = stage()
    s.settle()
    s.wizard.at = 1
    s.wizard.show()
    s.wizard.test_again()
    assert s.wizard.busy() or s.wizard.looking is not None
    s.settle()
    assert "The speed test is finished" in s.texts() and s.plan.values["ocr_gpu"] is True
