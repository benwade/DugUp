"""The same file in several places: content marks, taken while reading and for what was read before."""

import sqlite3
import threading

import pytest

from dugup import contentmark
from dugup.indexer import Indexer
from dugup.settings import Settings
from dugup.shapes import Failure, FailureReason, FileRecord, FileState, Root, SegmentIn, SegmentKind
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


def test_the_copy_on_your_own_drive_is_the_one_shown(tmp_path):
    """Ben's case: a file on C: and its backup on a cloud drive are one file, and C: is where he works."""
    with SqliteStore(tmp_path / "index.sqlite", prefer_under="C:\\") as store:
        rows = {}
        for path in ("G:\\My Drive\\Docs\\mottos.txt", "C:\\Users\\ben\\Documents\\Deep\\Down\\mottos.txt",
                     "G:\\Other computers\\PC (3)\\Docs\\mottos.txt", "G:\\It's\\notes.txt"):
            rows[path] = store.upsert_file(FileRecord(path, 5, 1.0, state=FileState.DONE))
            store.replace_segments(rows[path], [SegmentIn(0, 0, SegmentKind.TEXT, "alpha words")])
            store.set_content(rows[path], "other" if path.endswith("notes.txt") else "same")
        page = store.search_words("alpha")
        shown = next(r for r in page.rows if r.name == "mottos.txt")
        assert page.total == 2 and shown.path.startswith("C:\\"), "deeper down, and still the one shown"
        assert shown.also_at == ("G:\\My Drive\\Docs\\mottos.txt", "G:\\Other computers\\PC (3)\\Docs\\mottos.txt")
        store.prefer_under = "G:\\It's"                 # a quote in a folder name is only a character
        assert store.search_words("alpha").total == 2
        store.prefer_under = ""
        assert next(r for r in store.search_words("alpha").rows if r.name == "mottos.txt").path == "G:\\My Drive\\Docs\\mottos.txt"


def test_the_main_place_is_the_setting_or_the_drive_of_the_home_folder():
    from dugup.service import main_place
    assert main_place(Settings(), home="C:\\Users\\ben") == "C:\\"
    assert main_place(Settings(), home="/home/ben") == "" and main_place(Settings(), home="\\\\server\\share\\ben") == ""
    assert main_place(Settings(main_place=" D:\\Work "), home="C:\\Users\\ben") == "D:\\Work"


# ---- read once ----------------------------------------------------------------------------------------------

def test_a_file_whose_content_was_read_before_takes_its_text_and_is_not_read_again(w):
    w.put("Docs/report.txt", "alpha the same words")
    w.put("Docs/mixed.txt", "!mixed beta pages")
    w.run()
    assert sorted(p.rsplit("/", 1)[-1] for p in w.reader.calls) == ["mixed.txt", "report.txt"]
    w.put("Backup/report copy.txt", "alpha the same words")
    w.put("Backup/mixed copy.txt", "!mixed beta pages")
    w.put("Backup/new.txt", "alpha something new")
    report = w.run()
    assert [p.rsplit("/", 1)[-1] for p in w.reader.calls] == ["new.txt"], "only the new content went through a reader"
    assert (report.read, report.copied, report.done, report.partial) == (3, 2, 2, 1)
    assert w.found("alpha") == ["Backup/new.txt", "Docs/report.txt"] and w.copies("Docs/report.txt") == ["Backup/report copy.txt"]
    copy, original = w.get("Backup/mixed copy.txt"), w.get("Docs/mixed.txt")
    assert (copy.state, copy.error) == (original.state, original.error) and copy.state is FileState.PARTIAL
    assert w.store.get_pages(copy.row_id) == w.store.get_pages(original.row_id), "the pages that wait for OCR wait here too"
    from dugup.shapes import SearchFilters
    both = w.store.search_words("beta", filters=SearchFilters(every_copy=True))
    assert sorted(r.name for r in both.rows) == ["mixed copy.txt", "mixed.txt"], "the copy is found by the words, under its own name"
    assert w.store.search_words("copy").total == 2, "and by its own name"


def test_two_new_copies_in_one_run_and_read_once_switched_off(w):
    for i in range(6):
        w.put(f"d{i}/same.txt", "alpha all the same")
    report = w.run()
    assert report.done == 6 and report.copied == 5 and len(w.reader.calls) == 1, "one at a time: the first is read, the rest copied"
    w.put("later/same.txt", "alpha all the same")
    off = w.run(settings=Settings(read_once=False))
    assert off.copied == 0 and len(w.reader.calls) == 1 and tuple(w.store.copy_counts()) == (7, 1, 6)


def test_an_edited_copy_is_read_and_a_copy_of_a_failed_file_is_tried_itself(w):
    w.put("a.txt", "alpha first")
    w.put("locked.txt", "!password")
    w.run()
    w.put("b.txt", "alpha first")
    w.put("locked again.txt", "!password")
    report = w.run()
    assert report.copied == 1 and w.get("locked again.txt").state is FileState.FAILED and len(w.reader.calls) == 1
    w.put("b.txt", "alpha changed since")
    again = w.run()
    assert again.copied == 0 and [p.rsplit("/", 1)[-1] for p in w.reader.calls] == ["b.txt"]
    assert w.found("changed") == ["b.txt"] and w.found("first") == ["a.txt"]


def test_copying_what_was_read_copies_ocr_text_and_its_boxes(tmp_path):
    from dugup.protocols import OcrLine, OcrResult, PageInfo, PageVerdict
    from dugup.shapes import Box
    for make in (lambda: FakeStore(), lambda: SqliteStore(tmp_path / "index.sqlite")):
        store = make()
        one = store.upsert_file(FileRecord("/r/scan.pdf", 5, 1.0, state=FileState.PARTIAL))
        two = store.upsert_file(FileRecord("/r/Backup/scan (1).pdf", 5, 1.0))
        pages = [PageInfo(1, PageVerdict.BORN_DIGITAL, False), PageInfo(2, PageVerdict.NO_LAYER, True), PageInfo(3, PageVerdict.NO_LAYER, True)]
        store.replace_text(one, [SegmentIn(1, 0, SegmentKind.TEXT, "typed alpha")], pages)
        store.save_ocr(one, 2, OcrResult((OcrLine("scanned beta", Box(1, 2, 30, 40), 0.9),), "engine", "1"))
        store.copy_reading(one, two)
        assert store.get_pages(two) == store.get_pages(one) and store.ocr_status(two) == store.ocr_status(one)
        rows = {r.path: r for r in store.search_words("beta", filters=None).rows}
        assert len(rows) == 2 and all(r.evidence[0].box == Box(1, 2, 30, 40) for r in rows.values()), "the line's box comes along"
        assert [t.row_id for t in store.ocr_queue()] == [one, two] and all(t.page == 3 for t in store.ocr_queue())
        store.save_ocr(one, 3, OcrResult((OcrLine("gamma last", Box(5, 6, 7, 8), 0.8),), "engine", "1"))
        store.copy_page(one, two, 3)
        assert [t.row_id for t in store.ocr_queue()] == [] and len(store.search_words("gamma").rows) == 2
        store.fail_ocr(one, 3, "corrupt: bad")
        store.copy_page(one, two, 3)
        assert store.ocr_status(two) == store.ocr_status(one)
        if hasattr(store, "check_word_index"):
            store.check_word_index()
        if hasattr(store, "close"):
            store.close()


# ---- OCR once -----------------------------------------------------------------------------------------------

def test_the_same_page_in_several_files_is_read_by_ocr_once_and_used_for_each(tmp_path):
    from dugup.ocrqueue import OcrRunner
    from test_ocrqueue import ScriptedOcr, picture, scan, state
    with SqliteStore(tmp_path / "index.sqlite") as store:
        rows = {path: scan(store, path) for path in ("/r/Docs/scan.pdf", "/r/Backup/scan.pdf", "/r/PC 3/scan (1).pdf")}
        for row in rows.values():
            store.set_content(row, "samebytes")
        other = scan(store, "/r/Docs/different.pdf")
        store.set_content(other, "otherbytes")
        photo, twin = picture(store, "/r/a photo.jpg"), picture(store, "/r/a photo copy.jpg")
        store.set_content(photo, "photobytes")
        store.set_content(twin, "photobytes")
        ocr = ScriptedOcr(shapes={})
        report = OcrRunner(store, ocr, Settings()).run()
        assert report.total == 10 and report.read == 10 and report.copied == 5 and report.failed == 0
        assert len(ocr.calls) == 5, "two pages of the scan, two of the other, one photo: each content once"
        for path in rows:
            assert state(store, path) == (FileState.DONE, ""), path
        assert state(store, "/r/a photo copy.jpg") == (FileState.DONE, "")
        from dugup.shapes import SearchFilters
        every = store.search_words("words", filters=SearchFilters(every_copy=True, where="ocr"))
        assert every.total == 6, "every copy is found by what OCR read"
        assert all(r.evidence and r.evidence[0].box is not None for r in every.rows)
        assert OcrRunner(store, ocr, Settings()).run().total == 0


def test_a_page_that_fails_fails_for_each_copy_and_an_unreachable_copy_hands_over_to_another(tmp_path):
    from dugup.ocrqueue import OcrRunner
    from test_ocrqueue import ScriptedOcr, picture, state
    with SqliteStore(tmp_path / "index.sqlite") as store:
        for path in ("/r/bad.jpg", "/r/Backup/bad.jpg"):
            store.set_content(picture(store, path), "badbytes")
        for path in ("/r/away.jpg", "/r/Backup/here.jpg", "/r/Other/here too.jpg"):
            store.set_content(picture(store, path), "awaybytes")
        ocr = ScriptedOcr(shapes={}, answers={
            ("/r/bad.jpg", 1): Failure(FailureReason.CORRUPT, "the picture could not be opened"),
            ("/r/away.jpg", 1): Failure(FailureReason.UNREACHABLE, "the drive is away")})
        report = OcrRunner(store, ocr, Settings()).run()
        assert report.failed == 2 and "corrupt" in state(store, "/r/Backup/bad.jpg")[1], "the same bytes fail the same way"
        assert ("/r/Backup/bad.jpg", 1) not in ocr.calls
        assert state(store, "/r/Backup/here.jpg") == (FileState.DONE, "") and state(store, "/r/Other/here too.jpg") == (FileState.DONE, "")
        assert state(store, "/r/away.jpg")[0] is FileState.PARTIAL and report.left == 1, "the one out of reach keeps waiting"
        assert sorted(p for p, _ in ocr.calls) == ["/r/Backup/here.jpg", "/r/away.jpg", "/r/bad.jpg"]


def test_read_once_off_reads_every_copy(tmp_path):
    from dugup.ocrqueue import OcrRunner
    from test_ocrqueue import ScriptedOcr, picture
    with SqliteStore(tmp_path / "index.sqlite") as store:
        for path in ("/r/a.jpg", "/r/b.jpg"):
            store.set_content(picture(store, path), "same")
        ocr = ScriptedOcr(shapes={})
        report = OcrRunner(store, ocr, Settings(read_once=False)).run()
        assert report.read == 2 and report.copied == 0 and len(ocr.calls) == 2


def test_empty_files_are_not_copies_and_the_same_bytes_under_another_ending_are_read_for_themselves(w):
    """Found by the generated tree: an empty text file is fine, an empty PDF is damaged, and both are no bytes."""
    w.put("empty one.txt", "")
    w.put("empty two.txt", "")
    w.put("notes.txt", "alpha words")
    w.run()
    assert w.copies("empty one.txt") == [] and tuple(w.store.copy_counts())[1:] == (0, 0)
    other_kind = w.put("notes.pdf", "alpha words")
    report = w.run()
    assert report.copied == 0 and [p.rsplit("/", 1)[-1] for p in w.reader.calls] == ["notes.pdf"], \
        "a PDF with the bytes of a text file goes to the PDF reader, whatever the text reader made of them"
