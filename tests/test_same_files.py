"""The same file in several places: content marks, taken while reading and for what was read before."""

import sqlite3
import threading

import pytest

from dugup import contentmark
from dugup.indexer import Indexer
from dugup.settings import Settings
from dugup.shapes import Failure, FailureReason, FileRecord, FileState, Root
from dugup.store import MIGRATIONS, SqliteStore

from fakes.fake_store import FakeStore
from test_indexer import Clock, DiskReader, World


class Marking(DiskReader):
    """A reader that can also mark a file's content, from the fake disk."""

    def __init__(self, disk):
        super().__init__(disk)
        self.marked = []

    def content_mark(self, record, stop=None):
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped")
        content = self._content(record.path)
        self.marked.append(record.path)
        if content is None:
            return Failure(FailureReason.UNREACHABLE, "the file was not there")
        if content.startswith(b"!nomark"):
            return Failure(FailureReason.TIMED_OUT, "the drive did not answer")
        return contentmark.of_bytes(content)


class Marked(World):
    def __init__(self, store, settings=None, readers=1):
        super().__init__(store, settings)
        self.reader = Marking(self.h.disk)
        self.more = [Marking(self.h.disk) for _ in range(readers - 1)]

    def indexer(self, settings=None):
        return Indexer(self.store, self.h.source, self.reader, settings or self.settings, self.seen.append, self.clock,
                       timer=Clock(0.0), extra_extractors=self.more)

    def copies(self, rel):
        found = self.store.copies_of([self.get(rel).row_id])
        return sorted(path[len(self.root) + 1:] for path in found.get(self.get(rel).row_id, ()))


@pytest.fixture(params=["fake", "sqlite"])
def w(request, tmp_path):
    if request.param == "fake":
        yield Marked(FakeStore())
    else:
        with SqliteStore(tmp_path / "index.sqlite") as store:
            yield Marked(store)


def test_a_mark_is_the_same_for_the_same_bytes_and_for_nothing_else(tmp_path):
    one, two, other = tmp_path / "a.bin", tmp_path / "b with a name é.bin", tmp_path / "c.bin"
    one.write_bytes(b"x" * 3_000_000)
    two.write_bytes(b"x" * 3_000_000)
    other.write_bytes(b"x" * 2_999_999 + b"y")
    assert contentmark.of_file(str(one)) == contentmark.of_file(str(two)) == contentmark.of_bytes(b"x" * 3_000_000)
    assert contentmark.of_file(str(one)) != contentmark.of_file(str(other)) and len(contentmark.of_file(str(one))) == 32
    stop = threading.Event()
    stop.set()
    assert contentmark.of_file(str(one), stop) is None


def test_the_same_file_in_three_places_is_known_whatever_it_is_called(w):
    w.put("Docs/report.txt", "alpha the same words")
    w.put("Backup/report.txt", "alpha the same words")
    w.put("Other/renamed copy.txt", "alpha the same words")
    w.put("Docs/different.txt", "alpha other words")
    report = w.run()
    assert report.done == 4 and report.marked == 0, "marked while read, not afterwards"
    assert w.copies("Docs/report.txt") == ["Backup/report.txt", "Other/renamed copy.txt"]
    assert w.copies("Other/renamed copy.txt") == ["Backup/report.txt", "Docs/report.txt"]
    assert w.copies("Docs/different.txt") == []
    assert tuple(w.store.copy_counts()) == (4, 1, 2)


def test_a_copy_that_is_edited_or_gone_is_no_longer_a_copy(w):
    w.put("a.txt", "alpha same")
    w.put("b.txt", "alpha same")
    w.put("c.txt", "alpha same")
    w.run()
    assert w.copies("a.txt") == ["b.txt", "c.txt"]
    w.put("b.txt", "alpha edited since")
    w.h.delete(w.root, "c.txt")
    w.run()
    assert w.copies("a.txt") == [] and w.copies("b.txt") == [], "an edited copy is another file; a missing one is not listed"


def test_files_read_before_marks_existed_are_marked_once_and_a_stop_loses_nothing(w):
    for i in range(30):
        w.put(f"f{i:02d}.txt", "alpha twin" if i % 2 else f"alpha single {i}")
    w.put("bad.txt", "!password")
    w.put("slowdrive.txt", "!nomark alpha")
    w.run(settings=Settings(content_marks=False))
    assert tuple(w.store.copy_counts()) == (0, 0, 0) and len(w.store.unmarked()) == 32
    w.reader.marked.clear()
    stop = StopAfter(w.reader, 10)
    first = w.run(stop)
    assert first.stopped and 0 < first.marked < 32 and first.read == 0, "nothing is read again, only marked"
    second = w.run()
    assert not second.stopped and first.marked + second.marked == 32 and w.store.unmarked() == []
    assert tuple(w.store.copy_counts()) == (32, 1, 14), "15 twins are one set; the failed file and the unmarkable one stand alone"
    w.reader.marked.clear()
    third = w.run()
    assert third.marked == 0 and w.reader.marked == [], "a file that could not be marked is not asked again every run"
    assert w.get("bad.txt").state is FileState.FAILED


class StopAfter:
    """A stop signal that sets itself once the reader has marked this many files."""

    def __init__(self, reader, count):
        self.reader, self.count = reader, count

    def is_set(self):
        return len(self.reader.marked) >= self.count


def test_marks_are_taken_side_by_side_too(tmp_path):
    with SqliteStore(tmp_path / "index.sqlite") as store:
        w = Marked(store, readers=4)
        for i in range(40):
            w.put(f"d{i % 4}/f{i}.txt", f"alpha pair {i // 2}")
        w.run(settings=Settings(content_marks=False))
        report = w.run()
        assert report.marked == 40 and tuple(store.copy_counts()) == (40, 20, 20)
        assert sum(1 for reader in (w.reader, *w.more) if reader.marked) > 1


def test_an_index_from_before_marks_opens_and_takes_them(tmp_path):
    path = tmp_path / "index.sqlite"
    conn = sqlite3.connect(path)
    for name, function in (("dugup_text", lambda t, z: t), ("dugup_clean", lambda t: t)):
        conn.create_function(name, -1, function)
    for number, migration in enumerate(MIGRATIONS[:6], start=1):
        migration(conn)
        conn.execute(f"PRAGMA user_version={number}")
    conn.commit()
    conn.close()
    with SqliteStore(path) as store:
        assert store.schema_version() == len(MIGRATIONS) >= 7
        one = store.upsert_file(FileRecord("/r/a.txt", 5, 1.0, state=FileState.DONE))
        two = store.upsert_file(FileRecord("/r/b.txt", 5, 1.0, state=FileState.DONE))
        assert store.content_of(one) is None and [r.path for r in store.unmarked()] == ["/r/a.txt", "/r/b.txt"]
        store.set_content(one, "abc")
        store.set_content(two, "abc")
        assert store.copies_of([one, two, 999]) == {one: ["/r/b.txt"], two: ["/r/a.txt"]}
        store.set_content(two, None)
        assert store.copies_of([one]) == {} and store.content_of(two) is None


def test_the_real_reader_marks_files_in_a_worker(tmp_path):
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    docs = tmp_path / "docs"
    (docs / "Backup").mkdir(parents=True)
    (docs / "a note.txt").write_text("workerword the same text", encoding="utf-8")
    (docs / "Backup" / "a note (1).txt").write_text("workerword the same text", encoding="utf-8")
    (docs / "another.txt").write_text("workerword other text", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    with LocalService(DataLayout(data).ensure(), Settings(index_readers=2)) as service:
        service.set_root(Root(str(docs)))
        assert service.run_indexing().done == 3
        assert tuple(service.store.copy_counts()) == (3, 1, 1)


def test_a_search_shows_the_same_file_once_with_its_other_places(w):
    from dugup.shapes import SearchFilters
    w.put("Docs/report.txt", "alpha the same words")
    w.put("Backup/Docs/report.txt", "alpha the same words")
    w.put("Other computers/PC 3/Docs/report.txt", "alpha the same words")
    w.put("Docs/notes.txt", "alpha different words")
    w.run()
    page = w.store.search_words("alpha")
    assert page.total == 2 and sorted(r.path[len(w.root) + 1:] for r in page.rows) == ["Docs/notes.txt", "Docs/report.txt"], \
        "the copy highest up and not in a backup folder is the one shown"
    shown = next(r for r in page.rows if r.path.endswith("report.txt"))
    assert [p[len(w.root) + 1:] for p in shown.also_at] == ["Backup/Docs/report.txt", "Other computers/PC 3/Docs/report.txt"]
    assert next(r for r in page.rows if r.path.endswith("notes.txt")).also_at == ()
    every = w.store.search_words("alpha", filters=SearchFilters(every_copy=True))
    assert every.total == 4 and len(every.rows) == 4 and all(r.also_at == () for r in every.rows)
    assert w.store.search_words("alpha", limit=1).total == 2 and len(w.store.search_words("alpha", limit=1, offset=1).rows) == 1


def test_a_search_in_one_folder_shows_the_copy_that_is_there(w):
    from dugup.shapes import SearchFilters
    w.put("Docs/report.txt", "alpha the same words")
    w.put("Archive/deep/down/report.txt", "alpha the same words")
    w.run()
    inside = w.store.search_words("alpha", filters=SearchFilters(folder=w.root + "/Archive", case_insensitive=False))
    assert inside.total == 1 and inside.rows[0].path.endswith("Archive/deep/down/report.txt")
    assert [p[len(w.root) + 1:] for p in inside.rows[0].also_at] == ["Docs/report.txt"], "and still says where else it is"


def test_a_copy_found_by_its_own_name_is_shown_and_a_missing_copy_gives_way(w):
    w.put("Docs/report.txt", "alpha the same words")
    w.put("Docs/sub/quarterly.txt", "alpha the same words")
    w.run()
    page = w.store.search_words("quarterly")
    assert page.total == 1 and page.rows[0].path.endswith("quarterly.txt"), "only this copy has the word, in its name"
    w.h.delete(w.root, "Docs/report.txt")
    w.run()
    page = w.store.search_words("alpha")
    assert page.total == 1 and page.rows[0].path.endswith("quarterly.txt") and page.rows[0].also_at == (), \
        "the copy that is still there is the one shown; a missing one is not listed as a place"
