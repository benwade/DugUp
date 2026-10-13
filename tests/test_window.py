"""The search window. The logic is tested anywhere; the window itself where a display and tkinter exist
(on a Linux machine without a screen, run pytest under xvfb-run). Nothing here waits for a length of time:
a test that needs a background run to finish keeps handing the window its events until the run says so."""

import datetime
import threading
import gc
import os

import pytest

import helpers
from corpus.images import build_png
from dugup import window as w
from dugup.datadir import DataLayout
from dugup.service import LocalService
from dugup.settings import Settings
from dugup.shapes import SearchRow, WHERE_NAME, WHERE_OCR, WHERE_TEXT
from test_ocrqueue import ScriptedOcr, words


# ---- logic --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("", None), ("   ", None), ("0", 0), ("1500", 1500), ("200k", 204800), ("200 KB", 204800), ("5m", 5 * 1024 ** 2),
    ("1.5g", int(1.5 * 1024 ** 3)), ("2,000", 2000), ("3 MB", 3 * 1024 ** 2), ("7b", 7)])
def test_sizes_as_people_type_them(text, expected):
    assert w.parse_size(text) == expected


@pytest.mark.parametrize("text", ["big", "5x", "-3", "k", "1e999m", "nan", "m5"])
def test_a_size_that_makes_no_sense_is_refused_with_a_sentence(text):
    with pytest.raises(ValueError, match="A size is a number"):
        w.parse_size(text)


def test_days_are_local_and_the_before_day_is_included():
    start = w.parse_day("2026-10-12")
    assert datetime.datetime.fromtimestamp(start) == datetime.datetime(2026, 10, 12)
    end = w.parse_day("2026-10-12", end_of_day=True)
    assert datetime.datetime.fromtimestamp(end) == datetime.datetime(2026, 10, 13)
    assert w.parse_day("  ") is None
    for bad in ("12/10/2026", "2026-13-01", "yesterday", "2026-02-30"):
        with pytest.raises(ValueError, match="year-month-day"):
            w.parse_day(bad)


def test_a_form_becomes_filters():
    every = [value for _, value in w.KIND_LABELS]
    assert w.build_filters() == w.SearchFilters()
    assert w.build_filters(kinds=every).kinds == (), "every kind ticked narrows nothing (and lets other kinds through)"
    f = w.build_filters("/docs", WHERE_OCR, ["pdf", "image"], "2026-01-01", "2026-01-31", "10k", "5m")
    assert (f.folder, f.where, f.kinds, f.min_size, f.max_size) == ("/docs", WHERE_OCR, ("pdf", "image"), 10240, 5 * 1024 ** 2)
    assert datetime.datetime.fromtimestamp(f.modified_before) == datetime.datetime(2026, 2, 1)
    assert w.build_filters(after="2026-10-12", before="2026-10-12").modified_after is not None, "one day is a valid range"
    with pytest.raises(ValueError, match="smallest size is larger"):
        w.build_filters(smallest="5m", largest="1m")
    with pytest.raises(ValueError, match="later than"):
        w.build_filters(after="2026-10-12", before="2026-10-11")


def test_clicking_headers_builds_up_to_three_sort_keys():
    order = w.SortOrder()
    assert order.as_request() == "rank" and order.describe() == "best match first" and order.mark("name") == ""
    order.click("name")
    assert order.as_request() == [("name", False)] and order.mark("name") == f" {w.UP}"
    order.click("name")
    assert order.as_request() == [("name", True)], "the first key again turns it round"
    order.click("kind")
    order.click("modified")
    assert order.as_request() == [("modified", True), ("kind", False), ("name", True)], \
        "the newest click leads; dates, sizes and counts start largest first"
    assert order.describe() == f"modified {w.DOWN}, then type {w.UP}, then name {w.DOWN}"
    assert (order.mark("modified"), order.mark("kind"), order.mark("name")) == (f" {w.DOWN}1", f" {w.UP}2", f" {w.DOWN}3")
    order.click("size")
    assert [k for k, _ in order.as_request()] == ["size", "modified", "kind"], "a fourth key pushes the last one out"
    order.click("kind")
    assert order.as_request() == [("kind", False), ("size", True), ("modified", True)], "a key already there moves to the front"
    order.reset()
    assert order.as_request() == "rank"


def test_rows_and_labels():
    row = SearchRow(1, "/d/a.pdf", "a.pdf", "/d", 2048, 0.0, "pdf", "done", 1234, -1.0)
    assert w.row_values(row)[0] == "a.pdf" and w.row_values(row)[3] == "2.0 KB" and w.row_values(row)[5] == "1,234"
    row.missing_since = 5.0
    assert w.row_values(row)[0] == "a.pdf  [missing]"
    row.missing_since, row.unavailable = None, True
    assert "not available" in w.row_values(row)[0]
    assert w.where_label("name", 0, "pdf") == "In the file name"
    assert w.where_label("ocr", 1, "image") == "In the picture (read by OCR)"
    assert w.where_label("ocr", 3, "pdf") == "Page 3 (read by OCR)"
    assert w.where_label("text", 3, "pdf") == "Page 3" and w.where_label("native", 0, "office") == "In the text"


# ---- the window ---------------------------------------------------------------------------------------------

_tk = {}      # the one hidden tkinter root every window test shares


def make_root():
    """A fresh top-level window for one test, under one root that lives for the whole run.

    tkinter is started once only. Starting it again for every test can fail now and then on Windows
    ("Can't find a usable tk.tcl": it reads its script files afresh each time), which says nothing
    about the program, whose window starts tkinter exactly once.
    """
    tkinter = pytest.importorskip("tkinter", reason="this Python has no tkinter")
    if "root" not in _tk:
        try:
            _tk["root"] = tkinter.Tk()
        except tkinter.TclError as exc:
            _tk["root"] = exc
        else:
            _tk["root"].withdraw()
    if isinstance(_tk["root"], Exception):
        pytest.skip(f"no window can be opened here ({str(_tk['root']).splitlines()[0]}); on Linux run pytest under xvfb-run")
    window = tkinter.Toplevel(_tk["root"])
    window.withdraw()
    return window


class FakeDesktop:
    def __init__(self):
        self.calls = []
        self.answer = ""

    def open_file(self, path):
        self.calls.append(("open", path))
        return self.answer

    def edit_file(self, path):
        self.calls.append(("edit", path))
        return self.answer

    def show_in_folder(self, path):
        self.calls.append(("show", path))
        return self.answer


class Bench:
    def __init__(self, tmp_path, intent=None, settings=None, pick=False, setup=False):
        self.docs = tmp_path / helpers.AWKWARD_FOLDER
        self.docs.mkdir()
        (self.docs / "sub").mkdir()
        stamp = datetime.datetime(2026, 3, 1).timestamp()
        for name, text, day in (("alpha.txt", "a needle in alpha " + "padding " * 40, 0), ("beta.txt", "the needle of beta", 10),
                                ("sub/gamma.txt", "needle needle needle gamma", 20), ("needle-name.txt", "nothing relevant", 30)):
            path = self.docs / name
            path.write_text(text, encoding="utf-8")
            os.utime(path, (stamp + day * 86400, stamp + day * 86400))
        (self.docs / "sign.png").write_bytes(build_png())
        self.sign = str(self.docs / "sign.png")
        self.ocr = ScriptedOcr({(self.sign, 1): words("needle on a shop sign")}, shapes={})
        self.layout = DataLayout(tmp_path / "data").ensure()
        # most tests look at one step at a time, so an index update does not run on into OCR unless a test asks
        self.service = LocalService(self.layout, settings or Settings(ocr_after_index=False), ocr_backend=self.ocr, intent=intent)
        self.root = make_root()
        self.desktop = FakeDesktop()
        self.asked = []
        self.answer = None
        self.folder_to_add = str(self.docs)
        self.told = []
        self.app = w.App(self.root, self.service, self.desktop, ask=self._ask, choose_folder=lambda: self.folder_to_add,
                         pick=pick, setup=setup, tell=lambda title, text: self.told.append((title, text)))
        self.copied = []
        self.app.copy_text = self.copied.append     # the tests leave the real clipboard alone

    def _ask(self, title, text):
        self.asked.append(text)
        return self.answer

    def finish_run(self):
        """Hand the window its events until the background run has reported its end."""
        assert self.app.worker is not None, "nothing was started"
        while self.app.worker is not None:        # a run may start another (after a question is answered)
            self.app.worker.join(0.05)
            self.app.drain()
        self.root.update()

    def names(self):
        return [self.app.tree.item(i, "values")[0] for i in self.app.tree.get_children()]

    def ready(self):
        self.app.add_folder()
        self.app.start("index")
        self.finish_run()
        return self

    def find(self, text="needle"):
        self.app.words.set(text)
        self.app.search()
        self.root.update()
        return self.names()

    def text(self, widget):
        return widget.get("1.0", "end").rstrip("\n")

    def close(self):
        if not self.app.closed:
            self.app.close()



@pytest.fixture(autouse=True)
def windows_are_collected_on_this_thread():
    """After every test here, whatever the test left behind is collected now, on the main thread. A window
    holds tkinter variables in reference cycles; left to chance they are collected during a later test's
    background run, on its thread, which tkinter does not allow (a warning on Windows, and once a hang)."""
    yield
    gc.collect()

@pytest.fixture
def bench(tmp_path):
    made = Bench(tmp_path)
    yield made
    made.close()
    # tkinter's variables talk to the window system when they are collected, and that must happen in this
    # thread: left to chance, a later test's background run could be the one that collects them.
    made.app = made.root = None
    del made
    gc.collect()


def test_a_new_window_says_how_to_start_and_adding_a_folder_then_indexing_works(bench):
    assert "No folders yet" in bench.text(bench.app.status)
    bench.app.start("index")
    assert "no folder to index yet" in bench.app.message.get() and bench.app.worker is None
    bench.app.add_folder()
    assert str(bench.docs) in bench.app.folder_box.cget("values") and "Update the index" in bench.app.message.get()
    bench.app.start("index")
    assert str(bench.app.stop_button.cget("state")) == "normal" and str(bench.app.run_buttons[1].cget("state")) == "disabled"
    bench.finish_run()
    status = bench.text(bench.app.status)
    assert "Read 5 file(s)" in status and "done 4" in status and "1 file(s) wait for OCR" in status
    assert str(bench.app.stop_button.cget("state")) == "disabled" and str(bench.app.run_buttons[1].cget("state")) == "normal"
    assert bench.app.progress_text.get().startswith("Finished in ")


def test_search_shows_rows_and_what_was_found_in_the_selected_file(bench):
    bench.ready()
    names = bench.find()
    assert sorted(names) == ["alpha.txt", "beta.txt", "gamma.txt", "needle-name.txt"]
    assert bench.app.message.get() == "Showing 4 of 4 file(s) for: needle"
    assert bench.app.tree.selection() == ("0",), "the first row is selected so the right side is never blank"
    bench.app.tree.selection_set(str(names.index("gamma.txt")))
    bench.root.update()
    assert bench.app.path_text.get() == str(bench.docs / "sub" / "gamma.txt")
    assert bench.app.note_text.get() == "done" and "text, " in bench.app.facts_text.get()
    shown = bench.text(bench.app.hits)
    assert shown.startswith("In the text\n") and "needle needle needle gamma" in shown
    ranges = bench.app.hits.tag_ranges("hit")
    assert ranges and bench.app.hits.get(ranges[0], ranges[1]).casefold().startswith("needle"), "the match is marked"
    bench.app.tree.selection_set(str(names.index("needle-name.txt")))
    bench.root.update()
    assert bench.text(bench.app.hits).startswith("In the file name\nneedle-name.txt")


def test_an_empty_box_and_no_match_are_said(bench):
    bench.ready()
    assert bench.find("   ") == [] and "Type one or more words" in bench.app.message.get()
    assert bench.find("zebra") == [] and bench.app.message.get() == "No files found for: zebra"
    assert bench.app.path_text.get() == "" and bench.text(bench.app.hits) == ""


def test_enter_in_the_words_box_searches(bench):
    bench.ready()
    bench.app.words.set("beta")
    bench.root.deiconify()
    bench.root.update()
    bench.app.words_entry.focus_set()
    bench.root.update()
    bench.app.words_entry.event_generate("<Return>")
    bench.root.update()
    assert bench.names() == ["beta.txt"]


def test_filters(bench):
    bench.ready()
    app = bench.app
    app.where.set("In the file name")
    assert bench.find() == ["needle-name.txt"]
    app.where.set("In the text")
    assert sorted(bench.find()) == ["alpha.txt", "beta.txt", "gamma.txt"]
    app.clear_filters()
    app.after_day.set("2026-03-11")
    app.before_day.set("2026-03-21")
    assert sorted(bench.find()) == ["beta.txt", "gamma.txt"], "both days are included"
    app.clear_filters()
    app.smallest.set("200")
    assert bench.find() == ["alpha.txt"]
    app.clear_filters()
    app.folder.set(str(bench.docs))
    assert len(bench.find()) == 4
    app.clear_filters()
    app.kinds["text"].set(False)
    assert bench.find() == [] and "No files found" in app.message.get()
    for variable in app.kinds.values():
        variable.set(False)
    bench.find()
    assert "No kind of file is ticked" in app.message.get()
    app.clear_filters()
    app.smallest.set("huge")
    before = bench.names()
    bench.find()
    assert "A size is a number" in app.message.get() and bench.names() == before, "a slip of the keyboard does not clear the results"
    app.clear_filters()
    assert len(bench.find()) == 4


def test_clicking_headers_sorts_over_every_match_by_up_to_three_keys(bench):
    bench.ready()
    bench.find()
    app = bench.app
    app.sort_by("name")
    assert bench.names() == ["alpha.txt", "beta.txt", "gamma.txt", "needle-name.txt"]
    assert app.tree.heading("name", "text") == f"Name {w.UP}" and app.order_text.get() == f"name {w.UP}"
    app.sort_by("name")
    assert bench.names() == ["needle-name.txt", "gamma.txt", "beta.txt", "alpha.txt"]
    app.sort_by("modified")
    assert bench.names() == ["needle-name.txt", "gamma.txt", "beta.txt", "alpha.txt"], "newest first"
    app.sort_by("kind")
    assert app.order_text.get() == f"type {w.UP}, then modified {w.DOWN}, then name {w.DOWN}"
    assert app.tree.heading("kind", "text") == f"Type {w.UP}1" and app.tree.heading("name", "text") == f"Name {w.DOWN}3"
    assert bench.names() == ["needle-name.txt", "gamma.txt", "beta.txt", "alpha.txt"], "all text: the date decides"
    app.reset_order()
    assert app.order_text.get() == "best match first" and app.tree.heading("kind", "text") == "Type"
    assert bench.names()[0] == "gamma.txt", "three matches in one short file ranks first"


def test_results_come_a_page_at_a_time_and_more_fetches_the_rest(bench, monkeypatch):
    bench.ready()
    monkeypatch.setattr(w, "PAGE_ROWS", 3)
    bench.app.sort_by("name")
    assert bench.find() == ["alpha.txt", "beta.txt", "gamma.txt"]
    assert bench.app.message.get() == "Showing 3 of 4 file(s) for: needle"
    assert str(bench.app.more_button.cget("state")) == "normal"
    bench.app.more()
    assert bench.names() == ["alpha.txt", "beta.txt", "gamma.txt", "needle-name.txt"]
    assert str(bench.app.more_button.cget("state")) == "disabled" and "Showing 4 of 4" in bench.app.message.get()
    bench.app.tree.selection_set("3")
    bench.root.update()
    assert bench.app.path_text.get().endswith("needle-name.txt"), "rows of a later page are selectable like any other"


def test_file_actions_go_to_the_desktop_and_problems_are_shown(bench):
    bench.ready()
    app = bench.app
    app.open_selected()
    assert "Select a file first" in app.message.get() and bench.desktop.calls == []
    bench.app.sort_by("name")
    bench.find()
    path = str(bench.docs / "alpha.txt")
    app.open_selected()
    app.edit_selected()
    app.show_in_folder()
    assert bench.desktop.calls == [("open", path), ("edit", path), ("show", path)]
    assert app.message.get() == f"Shown in its folder: {path}"
    bench.desktop.answer = "The file is not at this path right now."
    app.open_selected()
    assert app.message.get() == "The file is not at this path right now."
    app.copy_path()
    assert bench.copied == [path] and app.message.get() == f"Copied: {path}"


def test_ocr_from_the_window_makes_the_picture_findable(bench):
    bench.ready()
    assert bench.find("shop sign") == []
    bench.app.start("ocr")
    bench.finish_run()
    status = bench.text(bench.app.status)
    assert "OCR read 1 of 1 waiting page(s)" in status and "wait for OCR" not in status
    assert bench.find("shop sign") == ["sign.png"]
    assert bench.text(bench.app.hits).startswith("In the picture (read by OCR)\n")
    bench.app.where.set("In text read by OCR")
    assert bench.find("needle") == ["sign.png"]


def test_stop_ends_a_run_and_it_can_be_continued(bench):
    bench.app.add_folder()
    release = __import__("threading").Event()

    class Held:
        """A reader that waits until the test lets it go, so the stop request is certain to arrive mid-run."""

        def extract(self, record, stop=None):
            release.wait()
            from dugup.shapes import Failure, FailureReason
            return Failure(FailureReason.STOPPED, "stopped") if stop is not None and stop.is_set() else None

    real = bench.service.extractor
    bench.service.extractor = Held()
    bench.app.start("index")
    bench.app.start("ocr")
    assert "already running" in bench.app.message.get()
    bench.app.stop_work()
    assert "Stopping" in bench.app.progress_text.get()
    release.set()
    bench.finish_run()
    assert bench.app.progress_text.get() == "Stopped." and "Start it again to continue" in bench.text(bench.app.status)
    bench.service.extractor = real
    bench.app.start("index")
    bench.finish_run()
    assert "done 4" in bench.text(bench.app.status)


def test_changed_settings_are_put_to_the_person(bench, tmp_path):
    bench.ready()
    bench.app.start("ocr")
    bench.finish_run()
    bench.service.settings = Settings(chunk_chars=900)
    bench.answer = None
    bench.app.start("index")
    bench.finish_run()
    assert len(bench.asked) == 1 and "chunk_chars" in bench.asked[0] and bench.app.worker is None
    assert "the settings changed" in bench.app.progress_text.get()
    bench.answer = True
    bench.app.start("index")
    bench.finish_run()          # the refusal, answered with "read again", and the run that follows
    assert "Read 5 file(s)" in bench.text(bench.app.status)
    assert bench.find("shop sign") == ["sign.png"], "the re-read kept what OCR had read"


def test_a_run_that_cannot_start_is_reported_not_thrown(bench):
    bench.app.add_folder()
    other = LocalService(bench.layout)
    assert other._lock.acquire()
    try:
        bench.app.start("index")
        bench.finish_run()
        assert "Could not do that (busy)" in bench.text(bench.app.status) and bench.app.progress_text.get() == "It could not run."
    finally:
        other._lock.release()
        other.close()


def test_closing_during_a_run_stops_it_and_closes_the_service(bench):
    bench.ready()
    (bench.docs / "late.txt").write_text("late words", encoding="utf-8")
    seen = []
    entered = __import__("threading").Event()

    class WaitsForStop:
        """A reader that only ends when it is asked to stop. The limit bounds a broken window, nothing else."""

        def extract(self, record, stop=None):
            entered.set()
            seen.append(stop.wait(30))
            from dugup.shapes import Failure, FailureReason
            return Failure(FailureReason.STOPPED, "stopped")

    bench.service.extractor = WaitsForStop()
    bench.app.start("index")
    assert entered.wait(30), "the run never reached the file"     # the reader says when it is under way
    bench.app.close()
    assert seen == [True], "closing the window asks the run to stop, and waits for it"
    assert bench.app.closed and bench.service._store is None
    with LocalService(bench.layout) as again:
        assert again._lock.acquire(), "the run lock was given back"
        again._lock.release()


def test_more_lists_every_place_in_the_selected_file_a_lot_at_a_time(bench, monkeypatch):
    pages = "\n\n".join(f"Section {n}. " + ("The needle is mentioned here. " if n % 2 else "Nothing to see. ") + "filler words " * 200
                        for n in range(1, 15))
    (bench.docs / "long.txt").write_text(pages, encoding="utf-8")
    bench.ready()
    monkeypatch.setattr(w, "PLACES_STEP", 3)
    names = bench.find()
    app = bench.app
    app.tree.selection_set(str(names.index("beta.txt")))
    bench.root.update()
    assert str(app.places_button.cget("state")) == "disabled", "one place, all shown: nothing more to ask for"
    app.tree.selection_set(str(names.index("long.txt")))
    bench.root.update()
    row = app.selected()
    assert row.matches > 3 and f"best of {row.matches} places" in bench.text(app.hits)
    assert str(app.places_button.cget("state")) == "normal" and f"all {row.matches} places" in app.places_button.cget("text")

    def marked():
        ranges = app.hits.tag_ranges("hit")
        return [app.hits.get(ranges[i], ranges[i + 1]).casefold() for i in range(0, len(ranges), 2)]

    app.more_places()
    assert len(marked()) == 3 and "best of" not in bench.text(app.hits), "the first press starts the full list from the top"
    assert app.places_button.cget("text") == f"More... (3 of {row.matches} shown)"
    presses = 1
    while str(app.places_button.cget("state")) == "normal":
        app.more_places()
        presses += 1
    assert len(marked()) == row.matches and set(marked()) == {"needle"}
    assert presses == -(-row.matches // 3) and app.places_button.cget("text") == f"All {row.matches} places are shown"
    text = bench.text(app.hits)
    assert text.index("Section 1.") < text.index("Section 3.") < text.index("Section 13."), "in reading order"
    app.tree.selection_set(str(names.index("beta.txt")))
    bench.root.update()
    assert str(app.places_button.cget("state")) == "disabled" and app.places is None, "another file starts afresh"


def test_exact_form_tick_box(bench):
    (bench.docs / "forms.txt").write_text("we educate; they were educated", encoding="utf-8")
    (bench.docs / "school.txt").write_text("education for all", encoding="utf-8")
    bench.ready()
    assert sorted(bench.find("education")) == ["forms.txt", "school.txt"], "off: other forms of the word are found too"
    bench.app.exact.set(True)
    assert bench.find("education") == ["school.txt"]
    assert bench.app.filters().exact is True
    bench.app.clear_filters()
    assert bench.app.exact.get() is False and len(bench.find("education")) == 2
    assert w.build_filters(exact=True).exact and not w.build_filters().exact


# ---- plain words ---------------------------------------------------------------------------------------------

class FakeReader:
    """Stands in for the model: answers from a table, after the test lets it."""

    def __init__(self):
        self.answer = None
        self.asked = []
        self.go = __import__("threading").Event()
        self.go.set()
        self.closed = False

    def understand(self, text, folders=()):
        self.asked.append((text, list(folders)))
        self.go.wait()
        return self.answer

    def close(self):
        self.closed = True


def finish_asking(bench):
    asker = bench.app.asker
    assert asker is not None, "nothing was asked"
    while bench.app.asker is not None:
        asker.join(0.05)
        bench.app.drain()
    bench.root.update()


def test_plain_words_work_out_of_the_box_by_rules(bench):
    bench.ready()
    app = bench.app
    assert app.ask_row.winfo_manager() == "grid", "the row is there with nothing set up"
    assert app.setup_button.winfo_manager() == "", "and nothing is offered for download"
    app.sentence.set("text files about needle bigger than 150 bytes")
    app.ask_in_plain_words()
    finish_asking(bench)
    assert (app.words.get(), app.smallest.get()) == ("needle", "150")
    assert [k for k, v in app.kinds.items() if v.get()] == ["text"]
    assert app.message.get().startswith("Filled in: words: needle; types: text; at least 150")
    app.search()
    bench.root.update()
    assert bench.names() == ["alpha.txt"]


def test_a_model_can_be_set_up_from_the_window_when_one_is_asked_for(tmp_path, monkeypatch):
    from dugup.intent import Understood
    bench = Bench(tmp_path, settings=Settings(intent_reader="model"))
    try:
        app = bench.app
        assert app.ask_row.winfo_manager() == "", "a model is asked for and none is there: no row yet"
        assert app.setup_button.winfo_manager() == "pack"
        app.sentence.set("anything")
        app.ask_in_plain_words()
        finish_asking(bench)
        assert "not set up" in app.message.get()
        bench.answer = False
        app.set_up_plain_words()
        assert "huggingface.co" in bench.asked[-1] and "Download now?" in bench.asked[-1], "the person sees what and from where first"
        assert app.message.get() == "Nothing was downloaded." and app.worker is None
        reader = FakeReader()
        reader.answer = Understood(words="needle")

        def pretend_install(stop=None, progress=None):
            bench.service._intent, bench.service._intent_looked = reader, True
            return {"server": "s", "model_file": "m"}

        monkeypatch.setattr(bench.service, "install_models", pretend_install)
        bench.answer = True
        app.set_up_plain_words()
        bench.finish_run()
        assert app.ask_row.winfo_manager() == "grid" and app.setup_button.winfo_manager() == ""
        assert app.progress_text.get() == "Plain-words search is set up."
        from dugup.shapes import Failure, FailureReason
        monkeypatch.setattr(bench.service, "install_models", lambda stop=None, progress=None: Failure(FailureReason.UNREACHABLE, "no network"))
        app.start("models")
        bench.finish_run()
        assert "no network" in bench.text(app.status) and app.progress_text.get() == "The download did not finish."
    finally:
        bench.close()
        bench.app = bench.root = None
        gc.collect()


def test_a_sentence_fills_in_the_form_and_waits_for_the_person(tmp_path):
    from dugup.intent import Understood
    from dugup.shapes import Failure, FailureReason
    reader = FakeReader()
    bench = Bench(tmp_path, intent=reader)
    try:
        bench.ready()
        app = bench.app
        assert app.ask_row.winfo_manager() == "grid", "with a model set up, the row is there"
        app.ask_in_plain_words()
        assert "as a sentence" in app.message.get() and reader.asked == []
        app.smallest.set("5g")                     # an old filter, which the sentence should replace
        reader.answer = Understood(words="needle", folder=str(bench.docs), where="text", kinds=("text", "pdf"),
                                   after="2026-03-05", before="2026-03-25", smallest="10", exact=True,
                                   notes=("a kind of file it named is not one this program knows",))
        reader.go.clear()
        app.sentence.set("text files about needle from mid March")
        app.ask_in_plain_words()
        assert str(app.ask_button.cget("state")) == "disabled" and "Reading your sentence" in app.message.get()
        app.ask_in_plain_words()
        assert "still being read" in app.message.get() and len(reader.asked) <= 1
        reader.go.set()
        finish_asking(bench)
        assert reader.asked == [("text files about needle from mid March", [str(bench.docs)])]
        assert (app.words.get(), app.folder.get(), app.where.get()) == ("needle", str(bench.docs), "In the text")
        assert [k for k, v in app.kinds.items() if v.get()] == ["pdf", "text"]
        assert (app.after_day.get(), app.before_day.get(), app.smallest.get(), app.largest.get()) == ("2026-03-05", "2026-03-25", "10", "")
        assert app.exact.get() is True and str(app.ask_button.cget("state")) == "normal"
        said = app.message.get()
        assert said.startswith("Filled in: words: needle; types: text, pdf") and "Not used: a kind of file" in said
        assert "press Enter to search" in said and bench.names() == [], "nothing is searched until the person says so"
        app.search()
        bench.root.update()
        assert sorted(bench.names()) == ["beta.txt", "gamma.txt"], "the filled-in form searches like a typed one"

        reader.answer = Failure(FailureReason.TIMED_OUT, "the model did not answer within 30 s")
        app.sentence.set("something else")
        app.ask_in_plain_words()
        finish_asking(bench)
        assert "could not be read (timed out)" in app.message.get() and app.words.get() == "needle", "a failure leaves the form alone"
    finally:
        bench.close()
        bench.app = bench.root = None
        gc.collect()        # as the fixture does: a closed window's leftovers are collected in this thread
    assert reader.closed, "closing the window ends the model server"


# ---- an index update goes on into OCR -------------------------------------------------------------------------

def test_update_the_index_goes_straight_on_to_the_pictures_and_says_so(tmp_path):
    bench = Bench(tmp_path, settings=Settings())
    try:
        bench.app.add_folder()
        bench.app.start("index")
        bench.finish_run()                       # the index update, then the OCR it started
        status = bench.text(bench.app.status)
        assert "Read 5 file(s)" in status and "Then pictures and scans were read (OCR):" in status
        assert status.index("Read 5 file(s)") < status.index("Then pictures and scans"), "both reports, in the order they happened"
        assert "wait for OCR" not in status.split("Then pictures and scans")[1].split("Data folder")[1]
        assert "sign.png" in bench.find("shop sign"), "one press made the picture findable"
        assert bench.app.progress_text.get().startswith("Finished in ") and bench.app.carried == ""
        bench.app.start("index")
        bench.finish_run()
        assert "Then pictures and scans" not in bench.text(bench.app.status), "nothing waits now: no second OCR run"
    finally:
        bench.close()


def test_ocr_does_not_follow_where_the_level_stops_at_words_or_the_run_was_stopped(tmp_path):
    from dugup.shapes import LEVEL_WORDS
    bench = Bench(tmp_path, settings=Settings())
    try:
        bench.app.add_folder()
        bench.service.set_level(str(bench.docs), LEVEL_WORDS)
        bench.app.start("index")
        bench.finish_run()
        assert bench.service.ocr_waiting() == 0 and "Then pictures and scans" not in bench.text(bench.app.status)
        assert "sign.png" not in bench.find("shop sign"), "the folder asked for words only"
        assert bench.ocr.calls == [] if hasattr(bench.ocr, "calls") else True
    finally:
        bench.close()


# ---- read a folder now, fast ----------------------------------------------------------------------------------

def fast_bench(tmp_path, monkeypatch):
    from dugup import service as service_module
    made = []

    def more(self, count, gpu=False):
        made.extend(ScriptedOcr(shapes={}) for _ in range(count))
        return made[-count:] if count else []

    monkeypatch.setattr(service_module.LocalService, "_more_readers", more)
    bench = Bench(tmp_path, settings=Settings(ocr_after_index=False, ocr_fast_readers=3))
    bench.extra = made
    for seed, name in enumerate(("one.png", "two.png", "three.png"), start=1):
        (bench.docs / "sub" / name).write_bytes(build_png(seed=seed))      # three pictures, not one in three places
    return bench


def test_read_a_folder_now_reads_just_that_folder_with_several_readers(tmp_path, monkeypatch):
    bench = fast_bench(tmp_path, monkeypatch)
    try:
        bench.ready()
        bench.folder_to_add = str(bench.docs / "sub")
        bench.app.read_folder_now()
        assert "several readers" in bench.app.progress_text.get()
        bench.finish_run()
        status = bench.text(bench.app.status)
        assert "OCR read 3 of 3 waiting page(s)" in status and "[3 readers side by side]" in status
        assert len(bench.extra) == 3
        read = [path for reader in bench.extra for path, _ in reader.calls]
        assert len(read) == 3 and all(os.sep + "sub" + os.sep in path for path in read), "only that folder"
        assert bench.service.ocr_waiting() == 1, "the picture outside it still waits"
        bench.folder_to_add = str(tmp_path)
        bench.app.read_folder_now()
        assert "not indexed yet" in bench.app.message.get() and bench.app.worker is None
    finally:
        bench.close()


def test_read_a_folder_now_takes_over_from_a_run_that_is_going(tmp_path, monkeypatch):
    bench = fast_bench(tmp_path, monkeypatch)
    try:
        bench.ready()
        started, release = threading.Event(), threading.Event()

        def hold(path, page):                 # the ordinary run is held on its first picture until the test lets it go
            started.set()
            assert release.wait(30)

        bench.ocr.on_call = hold
        bench.app.start("ocr")
        assert started.wait(30)
        bench.folder_to_add = str(bench.docs / "sub")
        bench.app.read_folder_now()
        assert bench.app.waiting_fast == str(bench.docs / "sub") and bench.app.stop.is_set()
        assert "As soon as the current run has stopped" in bench.app.message.get()
        bench.ocr.on_call = None
        release.set()
        bench.finish_run()                    # the stopped run ends, the fast one starts and ends
        status = bench.text(bench.app.status)
        assert bench.app.waiting_fast == "" and "readers side by side]" in status
        waiting = [task.path for task in bench.service.store.ocr_queue()]
        assert all(os.sep + "sub" + os.sep not in path for path in waiting), "everything in that folder was read"
    finally:
        bench.close()


def test_the_bar_moves_while_no_total_is_known_and_shows_the_part_done_after(tmp_path):
    from dugup.indexer import Progress
    bench = Bench(tmp_path)
    try:
        app = bench.app
        assert str(app.bar.cget("mode")) == "determinate"
        app.events.put(Progress("scan", "x", 0, 0))
        app.drain()
        assert str(app.bar.cget("mode")) == "indeterminate", "listing files: no total yet, so the bar slides"
        app.events.put(Progress("read", "x", 25, 100))
        app.drain()
        assert str(app.bar.cget("mode")) == "determinate" and float(app.bar.cget("value")) == 25.0
        app.events.put(Progress("estimate", "x", 30, 100, projected_bytes=5e6))
        app.drain()
        assert float(app.bar.cget("value")) == 25.0, "an estimate does not send the bar back to nothing"
        app.events.put(Progress("record", "x", 50, 100))
        app.drain()
        assert float(app.bar.cget("value")) == 50.0
    finally:
        bench.close()


def test_the_bar_moves_while_no_total_is_known_and_shows_the_part_done_after(tmp_path):
    from dugup.indexer import Progress
    bench = Bench(tmp_path)
    try:
        app = bench.app
        assert str(app.bar.cget("mode")) == "determinate"
        app.events.put(Progress("scan", "x", 0, 0))
        app.drain()
        assert str(app.bar.cget("mode")) == "indeterminate", "listing files: no total yet, so the bar slides"
        app.events.put(Progress("read", "x", 25, 100))
        app.drain()
        assert str(app.bar.cget("mode")) == "determinate" and float(app.bar.cget("value")) == 25.0
        app.events.put(Progress("estimate", "x", 30, 100, projected_bytes=5e6))
        app.drain()
        assert float(app.bar.cget("value")) == 25.0, "an estimate does not send the bar back to nothing"
        app.events.put(Progress("record", "x", 50, 100))
        app.drain()
        assert float(app.bar.cget("value")) == 50.0
    finally:
        bench.close()


def test_a_file_kept_in_several_places_is_shown_once_with_its_places_and_every_copy_shows_all(bench):
    (bench.docs / "Backup").mkdir()
    (bench.docs / "letter.txt").write_text("needle in a letter", encoding="utf-8")
    (bench.docs / "Backup" / "letter.txt").write_text("needle in a letter", encoding="utf-8")
    (bench.docs / "other.txt").write_text("needle elsewhere", encoding="utf-8")
    bench.ready()
    names = bench.find("needle")
    assert names.count("letter.txt") == 1 and "other.txt" in names
    bench.app.tree.selection_set(str(names.index("letter.txt")))
    bench.app.show_selected()
    row = bench.app.selected()
    assert row.path == str(bench.docs / "letter.txt") and row.also_at == (str(bench.docs / "Backup" / "letter.txt"),)
    assert "The same file is in 1 more place(s)" in bench.app.note_text.get()
    shown = bench.text(bench.app.hits)
    assert shown.startswith("The same file is also at:") and str(bench.docs / "Backup" / "letter.txt") in shown
    bench.app.every_copy.set(True)
    assert bench.find("needle").count("letter.txt") == 2 and bench.app.filters().every_copy is True
    bench.app.clear_filters()
    assert bench.app.every_copy.get() is False and bench.find("needle").count("letter.txt") == 1
