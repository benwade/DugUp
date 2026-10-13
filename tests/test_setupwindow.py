"""The setup wizard's window (setupwindow.py) and its place in the main window."""

import gc
import threading

import pytest

from dugup import catalogue, settings as settings_module
from dugup.catalogue import Machine
from dugup.setup import SetupPlan
from dugup.setupwindow import COMPUTER, SUMMARY, WELCOME, SetupWizard
from dugup.settings import Settings
from test_window import Bench, make_root

GB = 1024 ** 3
STRONG = Machine(cores=32, free_memory_bytes=35 * GB, free_disk_bytes=550 * GB, card="graphics card (DirectML)", card_speedup=23.0)


@pytest.fixture(autouse=True)
def windows_are_collected_on_this_thread():
    yield
    gc.collect()


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
        self.wizard = SetupWizard(self.root, self.plan, looker if look else None, self.save)

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
    s.wizard.next()
    assert s.wizard.page() == SUMMARY and str(s.wizard.next_button.cget("text")) == "Finish"
    summary = s.wizard.summary_text
    assert "Read on the graphics card: yes  (the graphics card read a sample page 23.0 times faster" in summary
    assert "Spending limit for the index: 56,320 MB  (a tenth of the 550 GB that is free now)" in summary
    assert "Next you choose which folders to index." in summary and "This is temporary" in summary
    assert "Every choice can be changed later under Settings." in summary
    s.wizard.next()
    assert s.saved == [{"ocr_gpu": True, "max_index_mb": 56320}] and s.wizard.finished and s.wizard.closed


def test_choosing_goes_through_one_page_for_each_group_with_rows_from_the_catalogue(stage):
    s = stage()
    s.settle()
    s.wizard.choice.set("choose")
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
    assert s.wizard.page() == COMPUTER and "The test is running" in s.texts() and s.wizard.busy()
    assert "not tested" not in s.texts() and "none that the reader can use" in s.texts(), "nothing is claimed before it is known"
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
    try:
        s.settle()
        assert "usual defaults are used" in s.wizard.message.get()
        s.wizard.next()
        s.wizard.next()
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

def test_the_wizard_opens_by_itself_on_a_first_start_and_finishing_saves_and_asks_for_a_folder(tmp_path, monkeypatch):
    from dugup import service as service_module
    monkeypatch.setattr(service_module.LocalService, "look_at_machine", lambda self, stop=None, test_card=True: STRONG)
    bench = Bench(tmp_path, setup=True)
    try:
        wizard = bench.app.wizard
        assert wizard is not None and not wizard.closed and wizard.plan.first_run
        while wizard.looking is not None:
            wizard.looking.join(0.05)
            wizard.drain()
        for _ in range(3):
            wizard.next()
        assert wizard.finished and "Saved 2 setting(s)." in bench.app.message.get()
        assert bench.service.settings.ocr_gpu is True and bench.service.settings.max_index_mb == 56320
        saved, notes = settings_module.load(bench.layout.config)
        assert saved.ocr_gpu is True and saved.max_index_mb == 56320 and notes == []
        bench.root.update()                        # the folder step that finishing queued
        assert str(bench.docs) in bench.app.folder_box.cget("values"), "setting up ends with choosing a folder"
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
        wizard = bench.app.wizard
        assert wizard.window.title() == "DugUp settings" and not wizard.plan.first_run and wizard.look is None
        wizard.changed("near_words", "20")
        wizard.at = len(wizard.sequence()) - 1
        wizard.show()
        wizard.next()
        assert bench.service.settings.near_words == 20 and "Saved 1 setting(s)." in bench.app.message.get()
        bench.app.open_setup()
        assert bench.app.wizard is not wizard and not bench.app.wizard.closed
    finally:
        bench.app.close()
