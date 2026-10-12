"""How text is stored: once, compressed, with the word index agreeing with it after every kind of change."""

import os
import sqlite3

import pytest

import helpers
from dugup import store as store_module
from dugup.protocols import OcrLine, OcrResult, PageInfo, PageVerdict
from dugup.shapes import Box, FileKind, FileRecord, SegmentIn, SegmentKind
from dugup.store import TEXT_PLAIN, TEXT_ZLIB, SqliteStore, pack_text, unpack_text

LONG = " ".join(f"sentence number {n} talks about harbours, bakeries and lanterns in plain words" for n in range(400))


def add(store, path, texts=(), kind=FileKind.TEXT):
    row = store.upsert_file(FileRecord(path=path, size=10, modified=1.0, kind=kind))
    store.replace_segments(row, [SegmentIn(i + 1, 0, SegmentKind.TEXT, t) for i, t in enumerate(texts)])
    return row


def names(store, query):
    return sorted(r.name for r in store.search_words(query).rows)


def read(lines):
    return OcrResult(tuple(OcrLine(t, Box(1, 1 + i, 9, 2 + i)) for i, t in enumerate(lines)), "fake-ocr", "1.0")


def test_text_is_packed_only_when_that_makes_it_smaller():
    value, z = pack_text(LONG)
    assert z == TEXT_ZLIB and isinstance(value, bytes) and len(value) < len(LONG) / 5 and unpack_text(value, z) == LONG
    assert pack_text("short") == ("short", TEXT_PLAIN) and pack_text("") == ("", TEXT_PLAIN)
    assert pack_text(LONG, compress=False) == (LONG, TEXT_PLAIN)
    odd = f"{helpers.CAFE} {helpers.CJK} {helpers.EMOJI} " * 200
    assert unpack_text(*pack_text(odd)) == odd
    assert unpack_text(None, 0) == "" and unpack_text(b"bytes kept as text", 0) == "bytes kept as text"


def test_text_is_stored_once_and_comes_back_exactly(store):
    odd = f"needle {helpers.CAFE} {helpers.CJK} {helpers.EMOJI} " + LONG
    row = add(store, "/d/a.txt", [odd, "short needle"])
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master")}
    assert "fts_content" not in tables, "the full-text table keeps no second copy of the text"
    stored = store.conn.execute("SELECT z, typeof(text) FROM segments WHERE file_id=? AND kind='text' ORDER BY page", (row,)).fetchall()
    assert [tuple(r) for r in stored] == [(TEXT_ZLIB, "blob"), (TEXT_PLAIN, "text")]
    found = store.search_words("needle").rows[0]
    assert len(found.evidence) == 2
    for evidence in found.evidence:
        assert store.get_passage(evidence, context=0).text == "needle"
    whole = store.get_passage(next(e for e in found.evidence if e.page == 1), context=10 ** 6)
    assert whole.text == odd, "every character survives being packed and unpacked"
    assert names(store, helpers.CJK) == ["a.txt"]


def test_compression_can_be_turned_off_and_both_kinds_live_side_by_side(store_path):
    with SqliteStore(store_path, compress_text=False) as plain:
        add(plain, "/d/plain.txt", ["plainword " + LONG])
        assert plain.conn.execute("SELECT z FROM segments WHERE kind='text'").fetchone()[0] == TEXT_PLAIN
    with SqliteStore(store_path) as packed:
        add(packed, "/d/packed.txt", ["packedword " + LONG])
        assert names(packed, "plainword") == ["plain.txt"] and names(packed, "packedword") == ["packed.txt"]
        assert names(packed, "harbours") == ["packed.txt", "plain.txt"] and packed.check_word_index()


def test_compressed_text_makes_a_smaller_index(tmp_path):
    sizes = {}
    for label, compress in (("plain", False), ("packed", True)):
        path = tmp_path / f"{label}.sqlite"
        with SqliteStore(path, compress_text=compress) as store:
            for n in range(30):
                add(store, f"/d/file{n}.txt", [f"file {n} " + LONG])
            store.compact()
        sizes[label] = os.path.getsize(path)
    assert sizes["packed"] < sizes["plain"] * 0.7, sizes


def test_the_word_index_agrees_with_the_text_after_every_kind_of_change(store):
    def ok(step):
        assert store.check_word_index(), f"the word index disagrees with the stored text after: {step}"

    a = add(store, "/d/alpha report.txt", ["first needle " + LONG, "second haystack"])
    ok("adding a file")
    store.upsert_file(FileRecord(path="/d/alpha report.txt", size=99, modified=2.0, kind=FileKind.TEXT))
    ok("recording the same path again")
    store.replace_segments(a, [SegmentIn(1, 0, SegmentKind.TEXT, "replaced needle " + LONG)])
    ok("replacing its text")
    scan = add(store, f"/d/{helpers.CAFE} scan.pdf", ["typed page"], FileKind.PDF)
    store.set_pages(scan, [PageInfo(1, PageVerdict.BORN_DIGITAL, False), PageInfo(2, PageVerdict.NO_LAYER, True)])
    store.save_ocr(scan, 2, read(["scanned thimble", LONG[:300]]))
    ok("saving OCR text")
    store.save_ocr(scan, 2, read(["scanned thimble again"]))
    ok("saving OCR text for the same page again")
    store.replace_text(scan, [SegmentIn(1, 0, SegmentKind.TEXT, "typed page, read again")],
                       [PageInfo(1, PageVerdict.BORN_DIGITAL, False), PageInfo(2, PageVerdict.NO_LAYER, True)])
    ok("reading the file again while keeping its OCR text")
    assert names(store, "thimble") == [f"{helpers.CAFE} scan.pdf"]

    store.move_file("/d/alpha report.txt", "/e/omega summary.txt")
    ok("moving a file to a new name")
    assert names(store, "omega") == ["omega summary.txt"] and names(store, "alpha") == [], \
        "the new name is found and the old one is not, in the name and beside every piece of text"
    assert names(store, "replaced needle") == ["omega summary.txt"]
    store.move_file(f"/d/{helpers.CAFE} scan.pdf", "/e/renamed scan.pdf")
    ok("moving a file that has OCR text")
    assert names(store, "thimble renamed") == ["renamed scan.pdf"]

    store.set_missing(a, 5.0)
    ok("flagging a file missing")
    b = add(store, "/gone/b.txt", ["doomed words " + LONG])
    c = add(store, "/gone/sub/c.txt", ["more doomed words"])
    store.remove_files([b])
    ok("removing a file")
    store.purge_folder("/gone")
    ok("purging a folder")
    store.purge_missing("/e", 10.0)
    ok("purging missing files")
    assert names(store, "doomed") == [] and names(store, "omega") == [] and names(store, "thimble") == ["renamed scan.pdf"]
    store.replace_segments(scan, [])
    ok("emptying a file")
    store.compact()
    ok("compacting")
    assert store.file_count() == 1 and store.segment_count() == 0


def test_the_self_test_notices_a_word_index_that_disagrees(store):
    row = add(store, "/d/a.txt", ["needle " + LONG])
    assert store.check_word_index()
    store.conn.execute("UPDATE files SET name='changed behind its back.txt' WHERE id=?", (row,))
    assert not store.check_word_index(), "a name changed without telling the word index must be noticed"


def test_a_version_4_index_is_made_smaller_and_keeps_everything(store_path, monkeypatch):
    everything = list(store_module.MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", everything[:4])
    odd = f"oldword {helpers.CAFE} {helpers.CJK} " + LONG
    with SqliteStore(store_path) as old:       # filled the way version 4 filled it: the text in both tables
        assert old.schema_version() == 4
        c = old.conn
        c.execute("INSERT INTO files(id, path, name, folder, size, modified, kind, state) "
                  "VALUES(1, '/d/kept report.pdf', 'kept report.pdf', '/d', 1, 1.0, 'pdf', 'done')")
        rows = [(1, 0, 0, "name", ""), (2, 1, 0, "text", odd), (3, 2, 1000000, "ocr", "scanned thimble\nsecond line")]
        rows += [(10 + n, 3 + n, 0, "text", f"page {n} " + LONG) for n in range(40)]
        for seg_id, page, order, kind, text in rows:
            c.execute("INSERT INTO segments(id, file_id, page, ord, kind, text) VALUES(?,1,?,?,?,?)", (seg_id, page, order, kind, text))
            c.execute("INSERT INTO fts(rowid, name, body) VALUES(?,?,?)", (seg_id, "kept report.pdf", text))
        c.execute("INSERT INTO regions(segment_id, engine, label, x0, y0, x1, y1, text) VALUES(3, 'rapidocr 3.10.0', 'line', 1, 2, 3, 4, 'scanned thimble')")
        c.execute("INSERT INTO regions(segment_id, engine, label, x0, y0, x1, y1, text) VALUES(3, 'rapidocr 3.10.0', 'line', 5, 6, 7, 8, 'second line')")
        c.execute("INSERT INTO pages(file_id, page, verdict, needs_ocr, ocr, ocr_engine) VALUES(1, 2, 'no_layer', 0, 'done', 'rapidocr 3.10.0')")
        c.execute("VACUUM")
    before = os.path.getsize(store_path)
    monkeypatch.setattr(store_module, "MIGRATIONS", everything)
    with SqliteStore(store_path) as new:
        assert new.schema_version() == len(everything) and new.check_word_index()
        assert names(new, "oldword") == ["kept report.pdf"] and names(new, helpers.CJK) == ["kept report.pdf"]
        assert names(new, "kept") == ["kept report.pdf"], "the file name is still searchable"
        row = new.search_words("oldword").rows[0]
        assert new.get_passage(row.evidence[0], context=10 ** 6).text == odd
        read_by_ocr = new.search_words("second line").rows[0].evidence[0]
        assert read_by_ocr.box == Box(5, 6, 7, 8) and new.get_passage(read_by_ocr, 0).kind is SegmentKind.OCR
        assert new.conn.execute("SELECT COUNT(*) FROM segments WHERE z=1").fetchone()[0] >= 41
        new.compact()
    after = os.path.getsize(store_path)
    assert after < before * 0.5, (before, after)
    with SqliteStore(store_path) as again:
        assert again.schema_version() == len(everything) and names(again, "thimble") == ["kept report.pdf"]


def test_another_program_reading_the_file_is_told_plainly(store, store_path):
    """The stored text is compressed, so a plain SQLite tool cannot read it through the view. It can still
    read every other table; this documents that, so it is a choice and not a surprise."""
    add(store, "/d/a.txt", ["needle " + LONG])
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    other = sqlite3.connect(str(store_path))
    try:
        assert other.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="dugup_"):
            other.execute("SELECT * FROM fts_src").fetchall()
    finally:
        other.close()
