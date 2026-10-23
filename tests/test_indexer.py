"""The indexer against an in-memory disk, with the in-memory store and with the real SQLite store.

The disk is the FileSource fake's; the reader below answers from the same disk, so every test is exact
and none touches a real folder. Clocks are fakes: nothing here waits or sleeps.
"""

import logging

import pytest

import helpers
from conformance.common import CountingStop
from dugup.indexer import (KEEP, OFFLINE_NOTE, POLICY_NOTE, REFUSE, REREAD, Indexer, IndexReport, Progress,
                              outcome_of, safe_rate)
from dugup.protocols import WARN_MASS_LOSS, WARN_ROOT_EMPTY, Extraction, PageInfo, PageVerdict
from dugup.settings import Settings
from dugup.shapes import (POLICY_NAMES_ONLY, Failure, FailureReason, FileState, Root, SegmentIn, SegmentKind)
from dugup.store import SqliteStore
from fakes.fake_filesource import Harness as DiskHarness
from fakes.fake_store import FakeStore

DAY = 86400.0


class DiskReader:
    """An Extractor that answers from the fake disk. A file's first word can ask for a particular outcome."""

    def __init__(self, disk) -> None:
        self.disk = disk
        self.calls = []

    def _content(self, path):
        for root, files in self.disk.roots.items():
            if path.startswith(root + "/") and root not in self.disk.unreachable:
                found = files.get(path[len(root) + 1:])
                return None if found is None else found.content
        return None

    def extract(self, record, stop=None):
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped before the file was read")
        self.calls.append(record.path)
        content = self._content(record.path)
        if content is None:
            return Failure(FailureReason.UNREACHABLE, "the file was not there when it was read")
        text = content.decode("utf-8")
        if text.startswith("!password"):
            return Failure(FailureReason.PASSWORD_PROTECTED, "needs a password")
        if text.startswith("!unsupported"):
            return Failure(FailureReason.UNSUPPORTED, "no reader for this")
        if text.startswith("!crash"):
            raise RuntimeError("reader bug")
        if text.startswith("!none"):
            return None
        if text.startswith("!ocr"):
            return Extraction((), (PageInfo(1, PageVerdict.NO_LAYER, True),))
        if text.startswith("!mixed"):
            return Extraction((SegmentIn(1, 0, SegmentKind.TEXT, text),),
                              (PageInfo(1, PageVerdict.BORN_DIGITAL, False), PageInfo(2, PageVerdict.NO_LAYER, True),
                               PageInfo(3, PageVerdict.UNREADABLE, False, "bad object")))
        if text.startswith("!cut"):
            return Extraction((SegmentIn(0, 0, SegmentKind.TEXT, text),), (), ("only the first part was read",))
        return Extraction((SegmentIn(0, 0, SegmentKind.TEXT, text),) if text.strip() else ())


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class World:
    def __init__(self, store, settings=None):
        self.h = DiskHarness()
        self.store = store
        self.reader = DiskReader(self.h.disk)
        self.clock = Clock()
        self.seen = []
        self.settings = settings or Settings()
        self.root = self.add_root()

    def add_root(self, policy="open"):
        root = self.h.new_root()
        self.store.set_root(Root(root, policy))
        return root

    def indexer(self, settings=None):
        return Indexer(self.store, self.h.source, self.reader, settings or self.settings, self.seen.append, self.clock,
                       timer=Clock(0.0))

    def run(self, stop=None, **options) -> IndexReport:
        self.reader.calls.clear()
        options.setdefault("on_settings_change", REFUSE)
        return self.indexer(options.pop("settings", None)).run(stop, **options)

    def put(self, rel, text="words", root=None, **kw):
        return self.h.put(root or self.root, rel, text.encode("utf-8"), **kw)

    def get(self, rel, root=None):
        return self.store.get_file(self.h.full_path(root or self.root, rel))

    def found(self, word):
        return sorted(r.path[len(self.root) + 1:] for r in self.store.search_words(word).rows)

    def picture(self):
        """Everything stored, in a form that compares equal between two stores."""
        out = {}
        for record in self.store.list_files():
            words = sorted(w for w in ("alpha", "beta", "gamma", "delta", "fresh") if record.path in
                           [r.path for r in self.store.search_words(w, limit=10_000).rows])
            out[record.path] = (record.state, record.error, record.missing_since, record.size, record.modified, tuple(words))
        return out


@pytest.fixture(params=["fake", "sqlite"])
def w(request, tmp_path):
    if request.param == "fake":
        yield World(FakeStore())
    else:
        with SqliteStore(tmp_path / "index.sqlite") as store:
            yield World(store)


# ---- the plain life of a file ----------------------------------------------------------------------------

def test_a_first_run_reads_everything_and_every_file_ends_in_a_stated_state(w):
    w.put("a.txt", "alpha here")
    w.put(f"sub/{helpers.CAFE} {helpers.CJK}.txt", "beta there")
    w.put("empty.txt", "   ")
    report = w.run()
    assert (report.read, report.done, report.stopped) == (3, 3, False) and report.roots[0].added == 3
    assert w.found("alpha") == ["a.txt"] and w.found("beta") == [f"sub/{helpers.CAFE} {helpers.CJK}.txt"]
    assert dict(w.store.state_counts()) == {"done": 3}, "nothing may be left waiting after a full run"
    assert w.get("empty.txt").state is FileState.DONE and w.get("empty.txt").error == ""


def test_a_second_run_on_an_unchanged_tree_changes_nothing_and_reads_nothing(w):
    for i in range(10):
        w.put(f"f{i}.txt", f"alpha {i}")
    w.run()
    before = w.picture()
    report = w.run()
    assert w.reader.calls == [] and report.read == 0
    assert (report.roots[0].added, report.roots[0].changed, report.roots[0].missing) == (0, 0, 0)
    assert w.picture() == before


def test_an_edited_file_is_read_again_and_its_old_text_is_forgotten(w):
    w.put("a.txt", "alpha old")
    w.put("b.txt", "beta untouched")
    w.run()
    w.put("a.txt", "fresh text")
    report = w.run()
    assert w.reader.calls == [w.h.full_path(w.root, "a.txt")] and report.roots[0].changed == 1
    assert w.found("alpha") == [] and w.found("fresh") == ["a.txt"] and w.found("beta") == ["b.txt"]


def test_a_renamed_file_keeps_its_text_without_being_read_again(w):
    w.put("old name.txt", "alpha kept")
    w.run()
    w.h.rename(w.root, "old name.txt", "sub/new name.txt")
    report = w.run()
    assert report.roots[0].moved == 1 and (report.roots[0].added, report.roots[0].missing) == (0, 0)
    assert w.reader.calls == [], "a move must not cost a re-read"
    assert w.found("alpha") == ["sub/new name.txt"] and w.store.file_count() == 1
    assert w.get("sub/new name.txt").state is FileState.DONE


def test_a_file_that_was_moved_and_edited_is_moved_then_read_again(w):
    w.put("a.txt", "alpha old")
    w.run()
    w.h.rename(w.root, "a.txt", "b.txt")
    w.put("b.txt", "fresh words")
    report = w.run()
    assert report.roots[0].moved == 1 and report.read == 1 and w.store.file_count() == 1
    assert w.found("fresh") == ["b.txt"] and w.found("alpha") == []


# ---- gone, back, purged ----------------------------------------------------------------------------------

def test_a_deleted_file_is_flagged_missing_and_stays_searchable(w):
    w.put("keep.txt", "alpha keep")
    w.put("gone.txt", "alpha gone")
    w.run()
    w.h.delete(w.root, "gone.txt")
    report = w.run()
    record = w.get("gone.txt")
    assert report.roots[0].missing == 1 and record.missing_since == w.clock.now and record.state is FileState.DONE
    assert w.found("alpha") == ["gone.txt", "keep.txt"], "a missing file stays findable until it is purged"
    assert dict(w.store.state_counts()) == {"done": 1, "missing": 1}
    first_seen = record.missing_since
    w.clock.now += 5 * DAY
    assert w.run().roots[0].missing == 0 and w.get("gone.txt").missing_since == first_seen, "the date is when it was first found gone"


def test_a_file_that_comes_back_unchanged_is_restored_without_a_re_read(w):
    path = w.put("a.txt", "alpha text")
    w.put("other.txt", "beta stays")
    w.run()
    saved = w.h.disk.roots[w.root].pop("a.txt")
    w.run()
    assert w.get("a.txt").missing_since is not None
    w.h.disk.roots[w.root]["a.txt"] = saved
    report = w.run()
    assert report.roots[0].restored == 1 and report.roots[0].added == 0 and w.reader.calls == []
    assert w.get("a.txt").missing_since is None and w.get("a.txt").state is FileState.DONE and w.found("alpha") == ["a.txt"]
    assert path == w.get("a.txt").path


def test_a_file_that_comes_back_different_is_read_again(w):
    w.put("a.txt", "alpha text")
    w.run()
    w.h.delete(w.root, "a.txt")
    w.run()
    w.put("a.txt", "fresh text")
    report = w.run()
    assert report.read == 1 and w.get("a.txt").missing_since is None
    assert w.found("fresh") == ["a.txt"] and w.found("alpha") == []


def test_missing_files_are_purged_only_after_the_retention_time(w):
    w.put("gone.txt", "alpha gone")
    w.put("stay.txt", "beta stay")
    w.run()
    w.h.delete(w.root, "gone.txt")
    w.run()
    w.clock.now += 29 * DAY
    assert w.run().roots[0].purged == 0 and w.found("alpha") == ["gone.txt"]
    w.clock.now += 2 * DAY
    assert w.run().roots[0].purged == 1
    assert w.found("alpha") == [] and w.get("gone.txt") is None and w.found("beta") == ["stay.txt"]


def test_the_retention_time_is_a_setting(w):
    w.put("gone.txt", "alpha gone")
    w.put("stay.txt", "beta stay")
    w.run()
    w.h.delete(w.root, "gone.txt")
    w.run(on_settings_change=KEEP)
    w.clock.now += 2 * DAY
    assert w.run(settings=Settings(missing_retention_days=1), on_settings_change=KEEP).roots[0].purged == 1


def test_a_file_moved_onto_the_place_of_a_missing_file_does_not_collide(w):
    w.put("a.txt", "alpha one")
    w.put("b.txt", "beta two")
    w.put("c.txt", "gamma three")
    w.run()
    w.h.delete(w.root, "b.txt")
    w.run()                                   # b.txt is now flagged missing
    w.h.rename(w.root, "a.txt", "b.txt")      # and a.txt takes its place
    report = w.run()
    assert report.roots[0].moved == 1 and w.reader.calls == [], "it is still a move: the text is kept, not re-read"
    assert w.found("alpha") == ["b.txt"] and w.found("beta") == []
    assert w.get("b.txt").missing_since is None and w.get("a.txt") is None and w.store.file_count() == 2


# ---- trouble with the folder itself ----------------------------------------------------------------------

def test_an_unreachable_root_changes_nothing_and_is_flagged(w):
    for i in range(5):
        w.put(f"f{i}.txt", f"alpha {i}")
    w.run()
    before = w.picture()
    w.h.make_unreachable(w.root)
    report = w.run()
    assert report.roots[0].reachable is False and report.roots[0].detail and report.read == 0
    assert w.picture() == before, "an unreachable folder must never look like an emptied one"
    assert [r.reachable for r in w.store.list_roots()] == [False]
    w.h.make_reachable(w.root)
    report = w.run()
    assert report.roots[0].reachable and w.reader.calls == [] and w.picture() == before
    assert [r.reachable for r in w.store.list_roots()] == [True]


def test_one_unreachable_root_does_not_stop_the_others(w):
    other = w.add_root()
    w.put("a.txt", "alpha")
    w.put("b.txt", "beta", root=other)
    w.h.make_unreachable(w.root)
    report = w.run()
    assert [r.reachable for r in report.roots] == [False, True] and report.done == 1
    assert [r.path for r in w.store.search_words("beta").rows] == [w.h.full_path(other, "b.txt")]


def test_a_root_that_is_suddenly_empty_is_warned_about_and_nothing_is_marked_missing(w):
    for i in range(3):
        w.put(f"f{i}.txt", f"alpha {i}")
    w.run()
    before = w.picture()
    w.h.clear(w.root)
    report = w.run()
    assert report.roots[0].warnings == (WARN_ROOT_EMPTY,) and report.roots[0].held_back == 3 and report.roots[0].missing == 0
    assert w.picture() == before
    assert w.store.list_roots()[0].warnings == (WARN_ROOT_EMPTY,), "the warning is kept with the root for the status view"


def test_losing_most_files_at_once_is_warned_about_but_new_files_are_still_taken(w):
    for i in range(30):
        w.put(f"f{i:02d}.txt", f"alpha {i}")
    w.run()
    for i in range(20):
        w.h.delete(w.root, f"f{i:02d}.txt")
    w.put("new.txt", "fresh arrival")
    report = w.run()
    assert report.roots[0].warnings == (WARN_MASS_LOSS,) and report.roots[0].held_back == 20
    assert dict(w.store.state_counts()) == {"done": 31}, "nothing is marked missing while the warning stands"
    assert w.found("fresh") == ["new.txt"]


def test_nothing_is_purged_while_a_warning_stands(w):
    for i in range(30):
        w.put(f"f{i:02d}.txt", f"alpha {i}")
    w.put("early.txt", "beta gone early")
    w.run()
    w.h.delete(w.root, "early.txt")
    assert w.run().roots[0].missing == 1          # flagged in the normal way
    for i in range(20):
        w.h.delete(w.root, f"f{i:02d}.txt")       # then most of the folder disappears
    w.clock.now += 40 * DAY                       # and the retention time passes
    report = w.run()
    assert report.roots[0].warnings == (WARN_MASS_LOSS,) and report.roots[0].purged == 0
    assert w.found("beta") == ["early.txt"], "a folder that looks wrong must not lose anything for good"


def test_a_folder_that_cannot_be_read_keeps_what_was_known_below_it(w):
    w.put("open/a.txt", "alpha open")
    w.put("locked/b.txt", "beta locked")
    w.run()
    w.h.make_folder_unreadable(w.root, "locked")
    report = w.run()
    assert [s.path for s in report.roots[0].skipped] == [w.h.full_path(w.root, "locked")] and report.roots[0].missing == 0
    assert w.get("locked/b.txt").missing_since is None and w.found("beta") == ["locked/b.txt"]


def test_the_data_folder_is_never_indexed_when_it_lies_inside_a_root(w):
    w.put("docs/a.txt", "alpha")
    w.put("appdata/logs/run.txt", "beta in the program's own log")
    indexer = Indexer(w.store, w.h.source, w.reader, extra_excludes=(w.h.full_path(w.root, "appdata"), "elsewhere:/x"))
    assert indexer.rules_for(w.store.list_roots()[0]).exclude == ("appdata",)
    indexer.run()
    assert w.found("alpha") == ["docs/a.txt"] and w.found("beta") == [] and w.store.file_count() == 1


# ---- placeholders and policy -----------------------------------------------------------------------------

def test_a_cloud_placeholder_is_listed_not_read_and_read_once_it_is_downloaded(w):
    w.put("cloud.txt", "alpha in the cloud", offline=True)
    w.run()
    record = w.get("cloud.txt")
    assert (record.state, record.error) == (FileState.SKIPPED, OFFLINE_NOTE) and w.reader.calls == []
    assert w.found("cloud") == ["cloud.txt"] and w.found("alpha") == [], "the name is searchable; the content was never read"
    assert w.run().read == 0 and w.reader.calls == [], "a placeholder is not retried while it stays a placeholder"
    w.h.disk.roots[w.root]["cloud.txt"].offline = False     # downloaded: same size, same time
    assert w.run().read == 1 and w.found("alpha") == ["cloud.txt"]


def test_a_names_only_root_keeps_names_and_reads_nothing(w):
    private = w.add_root(POLICY_NAMES_ONLY)
    w.put("diary.txt", "alpha secret", root=private)
    w.put("open.txt", "alpha open")
    w.run()
    assert w.reader.calls == [w.h.full_path(w.root, "open.txt")]
    record = w.get("diary.txt", private)
    assert (record.state, record.error, record.policy) == (FileState.SKIPPED, POLICY_NOTE, POLICY_NAMES_ONLY)
    assert [r.path for r in w.store.search_words("alpha").rows] == [w.h.full_path(w.root, "open.txt")]
    assert [r.path for r in w.store.search_words("diary").rows] == [w.h.full_path(private, "diary.txt")]


# ---- what a reader can answer -----------------------------------------------------------------------------

@pytest.mark.parametrize("content,state,note", [
    ("!password", FileState.FAILED, "password_protected: needs a password"),
    ("!unsupported", FileState.SKIPPED, "no reader for this"),
    ("!crash", FileState.FAILED, "crashed: the reader raised RuntimeError: reader bug"),
    ("!none", FileState.FAILED, "other: the reader returned nothing"),
    ("!ocr", FileState.PARTIAL, "1 of 1 page(s) wait for OCR"),
    ("!mixed alpha", FileState.PARTIAL, "1 of 3 page(s) wait for OCR; 1 page(s) could not be read (first: page 3, bad object)"),
    ("!cut alpha", FileState.PARTIAL, "only the first part was read"),
])
def test_every_answer_becomes_a_state_with_its_reason(w, content, state, note):
    w.put("odd.txt", content)
    w.put("fine.txt", "beta fine")
    report = w.run()
    record = w.get("odd.txt")
    assert (record.state, record.error) == (state, note)
    assert w.get("fine.txt").state is FileState.DONE and report.read == 2, "one bad file must not stop the run"
    assert w.found("odd") == ["odd.txt"], "the name stays searchable whatever happened to the content"


def test_page_records_are_stored_for_the_ocr_queue(w):
    w.put("scan.txt", "!mixed alpha")
    w.run()
    pages = w.store.get_pages(w.get("scan.txt").row_id)
    assert [(p.page, p.verdict, p.needs_ocr) for p in pages] == [
        (1, PageVerdict.BORN_DIGITAL, False), (2, PageVerdict.NO_LAYER, True), (3, PageVerdict.UNREADABLE, False)]


def test_a_file_that_can_no_longer_be_read_keeps_no_stale_text(w):
    w.put("a.txt", "alpha readable")
    w.run()
    w.put("a.txt", "!password now")
    w.run()
    assert w.found("alpha") == [] and w.get("a.txt").state is FileState.FAILED
    assert w.store.get_pages(w.get("a.txt").row_id) == []


def test_failed_files_are_left_alone_until_asked_or_changed(w):
    w.put("a.txt", "!password")
    w.run()
    assert w.run().read == 0 and w.reader.calls == [], "a failed file is not retried on every run"
    w.h.disk.roots[w.root]["a.txt"].content = b"alpha now"   # same size and time on purpose: only a retry sees it
    report = w.run(retry_failed=True)
    assert report.read == 1 and w.reader.calls == [w.h.full_path(w.root, "a.txt")]


def test_a_file_that_vanishes_between_the_scan_and_the_read_is_left_for_the_next_scan(w):
    w.put("a.txt", "alpha")
    w.put("b.txt", "beta")
    real = w.reader._content
    w.reader._content = lambda path: None if path.endswith("a.txt") else real(path)
    report = w.run()
    assert report.vanished == 1 and report.done == 1 and w.get("a.txt").state is FileState.PENDING
    w.reader._content = real
    assert w.run().done == 1 and w.found("alpha") == ["a.txt"]


def test_outcome_of_leaves_stopped_and_vanished_files_waiting():
    assert outcome_of(Failure(FailureReason.STOPPED, "x"))[0] is None
    assert outcome_of(Failure(FailureReason.UNREACHABLE, "x"))[0] is None
    assert outcome_of(Extraction()) == (FileState.DONE, "")


# ---- stopping and resuming --------------------------------------------------------------------------------

def _fill(world, count=25):
    for i in range(count):
        world.put(f"d{i % 4}/f{i:02d}.txt", ["alpha", "beta", "gamma", "delta"][i % 4] + f" {i}")
    world.put("bad.txt", "!password")
    world.put("scan.txt", "!ocr")


def test_a_stop_before_anything_does_nothing(w):
    _fill(w)
    stop = CountingStop(after=0)
    report = w.run(stop)
    assert report.stopped and report.read == 0 and w.store.file_count() == 0


@pytest.mark.parametrize("after", list(range(1, 70, 4)))
def test_a_run_stopped_anywhere_and_resumed_equals_an_uninterrupted_run(w, after, tmp_path):
    _fill(w)
    twin = World(FakeStore())
    twin.clock = w.clock
    _fill(twin)
    twin.run()
    report = w.run(CountingStop(after=after))
    if report.stopped:
        counts = dict(w.store.state_counts())
        assert sum(counts.values()) <= 27
    resumed = w.run()
    assert not resumed.stopped and "pending" not in dict(w.store.state_counts())
    mine = {p[len(w.root):]: v for p, v in w.picture().items()}
    theirs = {p[len(twin.root):]: v for p, v in twin.picture().items()}
    assert mine == theirs, "stop and resume must end exactly where an uninterrupted run ends"
    assert w.store.file_count() == 27, "no duplicates"


def test_work_already_done_is_not_done_again_after_a_stop(w):
    _fill(w)
    first = w.run(CountingStop(after=40))
    assert first.stopped and 0 < first.read < 27
    already = first.read
    second = w.run()
    assert second.read == 27 - already, "the resumed run reads only what was still waiting"


def test_the_indexer_stops_between_files_even_when_the_reader_never_looks(w):
    _fill(w)
    real = w.reader.extract
    w.reader.extract = lambda record, stop=None: real(record, None)   # a reader that ignores stop requests
    report = w.run(CountingStop(after=40))
    assert report.stopped and 0 < report.read < 27
    assert dict(w.store.state_counts()).get("pending", 0) == 27 - report.read


def test_a_stop_during_the_scan_records_nothing_for_that_root(w):
    _fill(w)

    class StopDuringScan:
        looks = 0

        def is_set(self):
            self.looks += 1
            return self.looks > 3     # inside the file source's listing loop

        def wait(self, timeout=None):
            return self.is_set()

    report = w.run(StopDuringScan())
    assert report.stopped and w.store.file_count() == 0 and w.store.list_roots()[0].last_scan is None


# ---- settings changes --------------------------------------------------------------------------------------

def test_changed_settings_stop_the_run_until_the_user_chooses(w):
    w.put("a.txt", "alpha")
    w.put("b.txt", "!password")
    w.run()
    w.put("c.txt", "beta new")
    changed = Settings(chunk_chars=900)
    report = w.run(settings=changed)
    assert report.settings_changed == ("chunk_chars",) and report.roots == [] and report.read == 0
    assert w.get("c.txt") is None, "nothing at all is done until the choice is made"

    kept = w.run(settings=changed, on_settings_change=KEEP)
    assert kept.settings_changed == () and w.reader.calls == [w.h.full_path(w.root, "c.txt")], "keep: only new files are read"
    assert w.run(settings=changed).settings_changed == (), "the choice is remembered"

    again = w.run(settings=Settings(chunk_chars=700), on_settings_change=REREAD)
    assert again.read == 3 and sorted(w.reader.calls) == sorted(w.h.full_path(w.root, n) for n in ("a.txt", "b.txt", "c.txt"))


def test_reread_does_not_read_placeholders_or_names_only_files(w):
    private = w.add_root(POLICY_NAMES_ONLY)
    w.put("p.txt", "alpha private", root=private)
    w.put("cloud.txt", "alpha cloud", offline=True)
    w.put("a.txt", "alpha")
    w.run()
    w.run(settings=Settings(chunk_chars=900), on_settings_change=REREAD)
    assert w.reader.calls == [w.h.full_path(w.root, "a.txt")]


def test_an_unknown_choice_is_a_programming_error(w):
    w.put("a.txt", "alpha")
    w.run()
    with pytest.raises(ValueError):
        w.run(settings=Settings(chunk_chars=900), on_settings_change="whatever")


# ---- progress, rates, logs ---------------------------------------------------------------------------------

def test_progress_counts_only_go_up_and_end_complete(w):
    _fill(w)
    w.run()
    reads = [p for p in w.seen if p.phase == "read"]
    assert reads and [p.done for p in reads] == sorted(p.done for p in reads)
    assert reads[-1].done == reads[-1].total == 27 and reads[-1].failed == 1
    assert {p.phase for p in w.seen} == {"scan", "record", "read"}


def test_rates_and_percentages_survive_zero():
    empty = Progress("read", "r", 0, 0)
    assert empty.percent == 0.0 and empty.rate == 0.0 and empty.remaining_seconds == 0.0
    no_time = Progress("read", "r", 5, 10, elapsed=0.0)
    assert no_time.rate == 0.0 and no_time.remaining_seconds is None and no_time.percent == 50.0
    assert Progress("read", "r", 5, 10, elapsed=10.0).remaining_seconds == 10.0
    assert Progress("read", "r", 10, 10, elapsed=0.0).remaining_seconds == 0.0
    assert safe_rate(5, 0) == 0.0 and IndexReport().rate == 0.0


def test_the_log_holds_counts_and_reasons_but_no_text_and_no_names(w, caplog):
    w.put(f"tax return {helpers.CAFE}.txt", "alpha confidentialword 123-45-6789")
    w.put("locked diary.txt", "!password hunter2")
    with caplog.at_level(logging.DEBUG, logger="dugup"):
        w.run()
        w.h.make_unreachable(w.root)
        w.run()
    text = caplog.text
    assert "run finished" in text and "password_protected" in text
    for secret in ("confidentialword", "123-45-6789", "hunter2", "tax return", "locked diary", helpers.CAFE, w.root):
        assert secret not in text


def test_only_the_chosen_roots_are_scanned(w):
    other = w.add_root()
    w.put("a.txt", "alpha")
    w.put("b.txt", "beta", root=other)
    report = w.run(roots=[other])
    assert [r.path for r in report.roots] == [other] and w.get("a.txt") is None


def test_what_cannot_be_read_is_told_apart_from_what_is_worth_a_look():
    from dugup.indexer import cannot_be_read
    for note in ("corrupt: not a readable Office file (BadZipFile: File is not a zip file)",
                 "password_protected: the PDF needs a password",
                 "OCR failed on 1 of 1 page(s) (first: page 1, corrupt: the picture could not be opened (UnidentifiedImageError))",
                 "2 of 3 page(s) wait for OCR; 1 page(s) could not be read (first: page 3, corrupt: bad object)"):
        assert cannot_be_read(note), note
    for note in ("timed_out: the job did not finish within 60 s and was ended", "crashed: the worker ended without an answer",
                 "corrupt: word/document.xml is larger than the limit of 64,000,000 bytes", "out_of_memory: too much",
                 "OCR failed on 1 of 1 page(s) (first: page 1, timed_out: the job did not finish)",
                 "2 of 3 page(s) wait for OCR", "", "only the first part was read",
                 "corrupt: bad; timed_out: slow"):
        assert not cannot_be_read(note), note


def test_an_editing_lock_file_is_listed_by_name_and_never_read(w):
    from dugup.kinds import classify, is_lock_file
    from dugup.shapes import FileKind
    assert is_lock_file("C:\\Docs\\~$Report.docx") and is_lock_file("/d/.~lock.Report.odt#") and not is_lock_file("/d/Report~$.docx")
    assert classify("G:\\x\\~$Curriculum.docx") is FileKind.OTHER
    w.put("~$Report.txt", "alpha lock")
    w.put("Report.txt", "alpha real")
    w.run()
    assert w.found("alpha") == ["Report.txt"] and w.get("~$Report.txt").state is FileState.SKIPPED
    assert all("~$" not in path for path in w.reader.calls)


# ---- a file's type changed in the settings ([extensions]) --------------------------------------------------

def test_files_whose_type_the_settings_change_are_read_again_once(w):
    """Changing [extensions] changes no file on disk, so nothing else would notice: the next update does."""
    from dugup.kinds import DEFAULT_EXTENSIONS
    from dugup.shapes import FileKind
    w.put("notes.nfo", "alpha in a release note")
    w.put("a.txt", "beta untouched")
    w.run()
    assert w.get("notes.nfo").kind is FileKind.OTHER      # (the test reader reads every kind; real ones do not)
    as_text = Settings(extensions={**DEFAULT_EXTENSIONS, "nfo": FileKind.TEXT})
    report = w.run(settings=as_text)
    assert w.reader.calls == [w.h.full_path(w.root, "notes.nfo")], "only the retyped file is read again"
    assert report.roots[0].retyped == 1 and w.get("notes.nfo").kind is FileKind.TEXT and w.found("alpha") == ["notes.nfo"]
    report = w.run(settings=as_text)
    assert w.reader.calls == [] and report.roots[0].retyped == 0, "once"
    report = w.run()                                           # back to the default: back to names only
    assert report.roots[0].retyped == 1 and w.get("notes.nfo").kind is FileKind.OTHER
    assert w.reader.calls == [w.h.full_path(w.root, "notes.nfo")]


def test_old_office_files_set_aside_are_read_once_libreoffice_is_installed(w, monkeypatch):
    from dugup.extractors import office
    there = {"soffice": None}
    monkeypatch.setattr(office, "find_converter", lambda: there["soffice"])
    plain = w.reader.extract

    def reader(record, stop=None):           # the real Office reader's answer, as far as the indexer can tell
        if record.path.endswith(".doc") and there["soffice"] is None:
            return Failure(FailureReason.UNSUPPORTED, office.legacy_note("doc"))
        return plain(record, stop)

    monkeypatch.setattr(w.reader, "extract", reader)
    w.put("old.doc", "alpha from long ago")
    w.put("other.txt", "!unsupported")
    w.run()
    assert w.get("old.doc").state is FileState.SKIPPED and "Install LibreOffice" in w.get("old.doc").error
    w.run()
    assert w.get("old.doc").state is FileState.SKIPPED, "nothing changed: still set aside"
    there["soffice"] = "soffice"
    w.run()
    assert w.get("old.doc").state is FileState.DONE and w.found("alpha") == ["old.doc"]
    assert w.get("other.txt").state is FileState.SKIPPED, "only old Office files waiting for LibreOffice are queued again"
