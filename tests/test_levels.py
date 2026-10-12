"""How far indexing goes, folder by folder (shapes.LEVELS, Service.set_level, `dugup roots level`)."""

import io

import pytest

import helpers
from dugup import cli
from dugup.datadir import DataLayout
from dugup.indexer import POLICY_NOTE
from dugup.ocrqueue import OcrRunner
from dugup.protocols import PageInfo, PageVerdict
from dugup.service import LocalService
from dugup.settings import Settings
from dugup.shapes import (LEVEL_MEANING, LEVEL_NAMES, LEVEL_OCR, LEVEL_WORDS, LEVELS, POLICY_NAMES_ONLY, Failure, FileKind,
                          FileRecord, FileState, Root, SegmentIn, SegmentKind, level_for, level_reaches)
from dugup.store import SqliteStore
from fakes.fake_store import FakeStore


# ---- which level a file has ----------------------------------------------------------------------------------

def test_the_deepest_folder_with_a_level_decides():
    root = Root("/r", levels=(("Books", LEVEL_NAMES), ("Books/Keep", LEVEL_WORDS), ("Scans", LEVEL_OCR)))
    assert level_for(root, "/r/a.txt") == LEVEL_MEANING, "no level given: the root's, which is everything"
    assert level_for(root, "/r/Books/x.pdf") == LEVEL_NAMES
    assert level_for(root, "/r/Books/Keep/deep/x.pdf") == LEVEL_WORDS, "a folder further down wins over the one above"
    assert level_for(root, "/r/Books/Keeper/x.pdf") == LEVEL_NAMES, "a longer name that starts the same is another folder"
    assert level_for(root, "/r/Bookshelf/x.pdf") == LEVEL_MEANING
    assert level_for(root, "/r/Scans/x.png") == LEVEL_OCR
    assert level_for(Root("/r", POLICY_NAMES_ONLY), "/r/x") == LEVEL_NAMES, "the old names-only policy is the names level"
    assert level_for(Root("/r", levels=(("", LEVEL_WORDS),)), "/r/sub/x") == LEVEL_WORDS, "'' is the root itself"
    assert level_for(Root("/r", POLICY_NAMES_ONLY, levels=(("Open", LEVEL_OCR),)), "/r/Open/x") == LEVEL_OCR


def test_windows_spellings_and_case():
    root = Root("D:\\Mes Docs\\", levels=((f"Livres/{helpers.CAFE}", LEVEL_NAMES),))
    path = f"D:\\Mes Docs\\LIVRES\\{helpers.CAFE.upper()}\\a.pdf"
    assert level_for(root, path, case_insensitive=True) == LEVEL_NAMES
    assert level_for(root, path, case_insensitive=False) == LEVEL_MEANING
    assert level_for(root, f"D:\\Mes Docs\\Livres\\{helpers.CAFE}\\sub\\a.pdf") == LEVEL_NAMES


def test_levels_are_ordered():
    assert LEVELS == (LEVEL_NAMES, LEVEL_WORDS, LEVEL_OCR, LEVEL_MEANING)
    assert level_reaches(LEVEL_MEANING, LEVEL_OCR) and level_reaches(LEVEL_OCR, LEVEL_OCR)
    assert not level_reaches(LEVEL_WORDS, LEVEL_OCR) and not level_reaches(LEVEL_NAMES, LEVEL_WORDS)


# ---- kept by both stores --------------------------------------------------------------------------------------

@pytest.mark.parametrize("make", [lambda tmp: FakeStore(), lambda tmp: SqliteStore(tmp / "i.sqlite")], ids=["fake", "real"])
def test_a_store_keeps_levels_through_changes_and_scans(make, tmp_path):
    store = make(tmp_path)
    levels = (("Books", LEVEL_NAMES), (f"{helpers.CAFE}/{helpers.CJK}", LEVEL_WORDS))
    store.set_root(Root("/r", levels=levels, exclude=(".git",)))
    store.note_scan("/r", True, 5.0, ("root_empty",))
    kept = store.list_roots()[0]
    assert kept.levels == levels and kept.exclude == (".git",) and kept.last_scan == 5.0
    store.set_root(Root("/r"))
    assert store.list_roots()[0].levels == (), "the store keeps what it is given; keeping levels across edits is the service's job"


def test_an_index_from_before_levels_opens_with_none(tmp_path):
    import sqlite3
    from dugup import store as store_module
    path = tmp_path / "old.sqlite"
    old = store_module.MIGRATIONS[:5]
    conn = sqlite3.connect(path)
    for name, function in (("dugup_text", lambda t, z: t), ("dugup_clean", lambda t: t)):
        conn.create_function(name, -1, function)
    for step in old:
        step(conn)
    conn.execute("PRAGMA user_version = 5")
    conn.execute("INSERT INTO roots(path, path_raw, policy, include, exclude) VALUES('/r', NULL, 'open', '[]', ?)", ('[".git"]',))
    conn.commit()
    conn.close()
    store = SqliteStore(path)
    root = store.list_roots()[0]
    assert root.levels == () and root.exclude == (".git",)
    store.close()


# ---- the service ----------------------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    docs = tmp_path / "docs"
    for folder in ("Letters", "Books/Sub", "Scans"):
        (docs / folder).mkdir(parents=True)
    (docs / "Letters" / "roof.txt").write_text("the roof letter", encoding="utf-8")
    (docs / "Books" / "novel.txt").write_text("a long novel about a whale", encoding="utf-8")
    (docs / "Books" / "Sub" / "index.txt").write_text("the whale index", encoding="utf-8")
    (docs / "top.txt").write_text("top whale", encoding="utf-8")
    layout = DataLayout(tmp_path / "data").ensure()
    service = LocalService(layout, Settings())
    service.set_root(Root(str(docs)))
    service.run_indexing()

    class World:
        pass

    w = World()
    w.docs, w.service = docs, service
    w.names = lambda word: sorted(r.name for r in service.search(word).rows)
    yield w
    service.close()


def test_lowering_a_folder_to_names_removes_its_text_and_keeps_its_names(world):
    assert world.names("whale") == ["index.txt", "novel.txt", "top.txt"]
    result = world.service.set_level(str(world.docs / "Books"), LEVEL_NAMES)
    assert result["purged"] == 2 and result["queued"] == 0 and result["level"] == LEVEL_NAMES and result["folder"] == "Books"
    assert world.names("whale") == ["top.txt"], "the text is gone at once, not at the next run"
    assert world.names("novel") == ["novel.txt"], "the name is still found"
    report = world.service.run_indexing()
    assert world.names("whale") == ["top.txt"], "a later run does not read it again"
    (world.docs / "Books" / "new.txt").write_text("another whale", encoding="utf-8")
    world.service.run_indexing()
    assert world.names("whale") == ["top.txt"] and world.names("new") == ["new.txt"], "a new file there is listed by name only"


def test_a_folder_further_down_can_be_raised_again_and_inherit_takes_a_level_away(world):
    service = world.service
    service.set_level(str(world.docs / "Books"), LEVEL_NAMES)
    result = service.set_level(str(world.docs / "Books" / "Sub"), LEVEL_WORDS)
    assert result["queued"] == 1 and result["folder"] == "Books/Sub"
    service.run_indexing()
    assert world.names("whale") == ["index.txt", "top.txt"]
    assert service.set_level(str(world.docs / "Books" / "Sub"), None)["purged"] == 1, "back to following Books"
    assert world.names("whale") == ["top.txt"]
    back = service.set_level(str(world.docs / "Books"), None)
    assert back["queued"] == 2 and back["level"] == LEVEL_MEANING and service.list_roots()[0].levels == ()
    service.run_indexing()
    assert world.names("whale") == ["index.txt", "novel.txt", "top.txt"]


def test_the_root_itself_can_be_given_a_level_and_other_changes_keep_the_levels(world):
    service = world.service
    assert service.set_level(str(world.docs), LEVEL_NAMES)["purged"] == 4
    assert world.names("whale") == [] and world.names("roof") == ["roof.txt"]
    service.set_level(str(world.docs / "Letters"), LEVEL_OCR)
    root = service.list_roots()[0]
    assert dict(root.levels) == {"": LEVEL_NAMES, "Letters": LEVEL_OCR}
    service.set_root(Root(root.path, root.policy, root.include, (".git",)))        # a skip rule is added
    assert dict(service.list_roots()[0].levels) == {"": LEVEL_NAMES, "Letters": LEVEL_OCR}, "skip rules do not wipe levels"
    service.run_indexing()
    assert world.names("roof") == ["roof.txt"] and world.names("letter") == ["roof.txt"] and world.names("whale") == []


def test_wrong_requests_are_stated_failures(world, tmp_path):
    assert world.service.set_level(str(world.docs), "everything").reason.value == "unsupported"
    outside = world.service.set_level(str(tmp_path / "elsewhere"), LEVEL_WORDS)
    assert isinstance(outside, Failure) and "dugup roots add" in outside.detail
    assert world.service.list_roots()[0].levels == ()


# ---- OCR follows the level ------------------------------------------------------------------------------------

class Reader:
    def __init__(self):
        self.seen = []

    def ready(self):
        return "fake"

    def engine_id(self):
        return "fake 1"

    def probe(self, paths, stop=None):
        return [(800, 600, False) for _ in paths]

    def read_page(self, path, page, stop=None, **options):
        return self._one(path)

    def read(self, path, stop=None, **options):
        return self._one(path)

    def _one(self, path):
        from dugup.protocols import OcrResult
        self.seen.append(path)
        return OcrResult((), "fake", "1")


def test_ocr_leaves_pages_alone_where_the_level_stops_at_words():
    store = FakeStore()
    store.set_root(Root("/r", levels=(("Covers", LEVEL_WORDS),)))
    rows = {}
    for rel in ("Covers/a.png", "Scans/b.png"):
        rows[rel] = store.upsert_file(FileRecord(path=f"/r/{rel}", size=1, modified=1.0, kind=FileKind.IMAGE))
        store.set_pages(rows[rel], [PageInfo(1, PageVerdict.NO_LAYER, True)])
    root = store.list_roots()[0]
    wanted = lambda path: level_reaches(level_for(root, path), LEVEL_OCR)
    reader = Reader()
    runner = OcrRunner(store, reader, Settings(), wanted=wanted)
    report = runner.run()
    assert report.held_back == 1 and report.total == 1, "the cover is not even measured"
    waiting = [task.path for task in store.ocr_queue()]
    assert "/r/Covers/a.png" in waiting, "it stays queued, so raising the level later reads it"
    assert all("Covers" not in str(path) for path in reader.seen)
    assert OcrRunner(store, Reader(), Settings()).run().held_back == 0, "with no levels in play nothing is held back"


# ---- the command ----------------------------------------------------------------------------------------------

def test_level_command(tmp_path):
    data, docs = tmp_path / "data", tmp_path / helpers.AWKWARD_FOLDER
    (docs / "Books").mkdir(parents=True)
    (docs / "Books" / "novel.txt").write_text("a whale", encoding="utf-8")
    (docs / "note.txt").write_text("a whale too", encoding="utf-8")

    def run(*argv):
        out = io.StringIO()
        return cli.main([*argv, "--data-dir", str(data)], stdout=out, environ={}), out.getvalue()

    run("roots", "add", str(docs))
    run("index")
    code, text = run("roots", "level", str(docs / "Books"), "names")
    assert code == 0 and "level names" in text and "1 file(s) was removed" in text and "names stay searchable" in text
    assert "level names: Books" in run("roots", "list")[1]
    assert "novel.txt" not in run("search", "whale")[1] and "note.txt" in run("search", "whale")[1]
    code, text = run("roots", "level", str(docs / "Books"), "words")
    assert "1 file(s) now wait to be read" in text and "dugup index" in text and "no longer read" in text
    code, text = run("roots", "level", str(docs / "Books"), "inherit")
    assert code == 0 and "follows the folder above" in text and "level meaning" in text
    assert "level" not in run("roots", "list")[1].split("policy")[1].split("\n", 1)[1].split("skipping")[0]
    code, text = run("roots", "level", str(tmp_path / "nowhere"), "names")
    assert code == 1 and "not inside an indexed folder" in text


def test_the_service_holds_ocr_back_by_level(tmp_path):
    docs = tmp_path / "docs"
    (docs / "Covers").mkdir(parents=True)
    (docs / "Scans").mkdir()
    for rel in ("Covers/a.txt", "Scans/b.txt"):
        (docs / rel).write_text("words", encoding="utf-8")
    reader = Reader()
    with LocalService(DataLayout(tmp_path / "data").ensure(), Settings(), ocr_backend=reader) as service:
        service.set_root(Root(str(docs)))
        service.run_indexing()
        for record in service.store.list_files():
            service.store.set_pages(record.row_id, [PageInfo(1, PageVerdict.NO_LAYER, True)])   # as if each were a scan
        service.set_level(str(docs / "Covers"), LEVEL_WORDS)
        report = service.run_ocr()
        assert report.held_back == 1 and report.read == 1, report
        assert len(reader.seen) == 1 and "Scans" in str(reader.seen[0])
        service.set_level(str(docs / "Covers"), LEVEL_OCR)
        assert service.run_ocr().read == 1, "raised again: the page that waited is read"
