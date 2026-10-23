"""The settings window (settingswindow.py): every setting, found by group or by words, with its cost."""

from __future__ import annotations

import gc
import threading

import pytest

from dugup import catalogue
from dugup.catalogue import Machine
from dugup.settings import Settings
from dugup.settingswindow import ALL, SettingsWindow, matches, sorted_entries

GB = 1024 ** 3
STRONG = Machine(cores=32, free_memory_bytes=35 * GB, free_disk_bytes=550 * GB, card="graphics card (DirectML)", card_speedup=23.0)


@pytest.fixture(autouse=True)
def collected():
    yield
    gc.collect()


class Stage:
    def __init__(self, settings=None, look="now"):
        from test_window import make_root
        self.root = make_root()
        self.saved = []
        self.answer = ""
        self.release = threading.Event()
        values = {name: getattr(settings or Settings(), name) for name in catalogue.ENTRIES}

        def looker(stop):
            if look == "held":
                assert self.release.wait(30)
            if look == "broken":
                raise RuntimeError("no such computer")
            return STRONG

        self.window = SettingsWindow(self.root, values, self.save, looker if look else None)

    def save(self, changes):
        if not self.answer:
            self.saved.append(changes)
        return self.answer

    def settle(self):
        for _ in range(300):
            if self.window.looking is None or not self.window.looking.is_alive():
                break
            self.window.looking.join(0.05)
        self.window._poll()
        self.root.update()

    def close(self):
        self.window.close()


@pytest.fixture
def stage():
    made = []

    def make(**kw):
        s = Stage(**kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_every_setting_is_shown_and_search_finds_by_label_help_or_name(stage):
    s = stage()
    s.window.advanced.set(True)
    s.window._advanced_chosen()
    assert sorted(s.window.widgets) == sorted(catalogue.ENTRIES), "All settings: a row for each"
    s.window.query.set("readers")
    s.window.redraw()
    found = set(s.window.widgets)
    assert {"index_readers", "ocr_fast_readers", "looks_readers", "watch_readers"} <= found and "hotkey" not in found
    s.window.query.set("battery")
    s.window.redraw()
    assert set(s.window.widgets) == {"watch_on_battery"}
    s.window.query.set("zzzz nothing")
    s.window.redraw()
    assert s.window.widgets == {}
    s.window.query.set("")
    s.window.groups.selection_clear(0, "end")
    s.window.groups.selection_set(s.window.group_keys.index("tray"))
    s.window._group_chosen()
    assert set(s.window.widgets) == {e.name for e in catalogue.in_group("tray")}


def test_rows_come_everyday_first_then_by_what_they_cost():
    entries = catalogue.in_group("speed")
    ordered = sorted_entries(entries)
    assert all(e.everyday for e in ordered[:sum(1 for e in entries if e.everyday)])
    rest = [catalogue.cost(e.name)[0] for e in ordered if not e.everyday]
    rank = {"heavy": 0, "medium": 1, "light": 2, "": 3}
    assert rest == sorted(rest, key=rank.get)
    assert matches(catalogue.ENTRIES["watch_readers"], "quarter cores") and not matches(catalogue.ENTRIES["hotkey"], "quarter")


def test_a_change_is_checked_marked_resettable_and_saved_together(stage):
    s = stage()
    s.window.changed("near_words", "nonsense")
    assert "near_words" in s.window.problems and "Words close together" in s.window.message.get()
    s.window.save()
    assert s.saved == [] and "marked in red" in s.window.message.get()
    s.window.changed("near_words", "20")
    s.window.changed("watch_every_minutes", "30")
    assert s.window.edits == {"near_words": 20, "watch_every_minutes": 30.0} and "2 change(s)" in s.window.message.get()
    s.window.reset("near_words")
    assert s.window.edits == {"watch_every_minutes": 30.0}
    s.window.save()
    assert s.saved == [{"watch_every_minutes": 30.0}] and s.window.edits == {} and "Saved 1" in s.window.message.get()
    s.answer = "the disk is full"
    s.window.changed("near_words", "21")
    s.window.save()
    assert s.window.message.get() == "the disk is full" and s.window.edits == {"near_words": 21}, "a failed save keeps the edits"


def test_suggest_for_this_machine_fills_in_values_with_reasons_and_saves_nothing_by_itself(stage):
    s = stage(look="held")
    s.window.suggest()
    assert str(s.window.suggest_button["state"]) == "disabled" and "Looking at this computer" in s.window.message.get()
    s.release.set()
    s.settle()
    assert str(s.window.suggest_button["state"]) == "normal"
    assert s.window.edits.get("ocr_gpu") is True, "a strong card: OCR on it"
    assert s.window.reasons["ocr_gpu"].startswith("Suggested: the graphics card read a sample page")
    assert "filled in for this computer (32 cores)" in s.window.message.get() and s.saved == []
    s.window.save()
    assert s.saved and s.saved[0]["ocr_gpu"] is True


def test_a_look_that_fails_says_so_and_leaves_the_window_working(stage):
    s = stage(look="broken")
    s.window.suggest()
    s.settle()
    assert "could not be looked at" in s.window.message.get() and str(s.window.suggest_button["state"]) == "normal"
    s.window.changed("near_words", "20")
    assert s.window.edits == {"near_words": 20}


def test_without_a_look_the_button_is_off(stage):
    s = stage(look=None)
    assert str(s.window.suggest_button["state"]) == "disabled"


def test_it_opens_on_a_short_list_that_also_shows_whatever_was_changed(stage):
    assert set(catalogue.SHORT_LIST) <= set(catalogue.ENTRIES)
    s = stage(settings=Settings(near_words=33))
    assert set(s.window.widgets) == set(catalogue.SHORT_LIST) | {"near_words"}, "the short list, and a changed one"
    assert not s.window.groups.winfo_ismapped()
    s.window.query.set("battery")
    s.window.redraw()
    assert set(s.window.widgets) == {"watch_on_battery"}
    s.window.query.set("readers")
    s.window.redraw()
    assert "index_readers" in s.window.widgets, "a search reaches every setting"
    s.window.query.set("")
    s.window.advanced.set(True)
    s.window._advanced_chosen()
    assert sorted(s.window.widgets) == sorted(catalogue.ENTRIES)
    s.window.advanced.set(False)
    s.window._advanced_chosen()
    assert len(s.window.widgets) == len(catalogue.SHORT_LIST) + 1
