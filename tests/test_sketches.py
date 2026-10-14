"""Sketches of meaning kept as bytes, found in two stages, and the store's part in it."""

import pytest

np = pytest.importorskip("numpy")

from dugup import sketches
from dugup.shapes import FileKind, FileRecord, FileState, SegmentIn, SegmentKind
from dugup.store import SqliteStore

from fakes.fake_store import FakeStore


def unit(rows, dims, seed=0):
    v = np.random.default_rng(seed).standard_normal((rows, dims)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_bytes_keep_closeness_to_two_places():
    docs, question = unit(50, 768, 1), unit(1, 768, 2)[0]
    data, scales = sketches.quantize(docs)
    assert len(data) == 50 * 768 and len(scales) == 50
    back = sketches.dequantize(data, 768, scales)
    assert np.abs(sketches.closeness(back, question) - docs @ question).max() < 0.01


def test_the_fingerprint_scan_keeps_the_best_within_the_candidates():
    docs = unit(5000, 768, 3)
    question = docs[123] * 0.9 + unit(1, 768, 4)[0] * 0.1      # near one of them
    truth = np.argsort(-(docs @ question))[:10]
    prints = np.frombuffer(b"".join(sketches.fingerprint_of(v) for v in docs), dtype=np.int8).reshape(5000, sketches.FINGERPRINT)
    kept = sketches.scan(prints, question, keep=800)
    assert len(kept) == 800 and len(set(kept)) == 800 and kept[0] == 123
    assert len(set(truth) & set(kept)) >= 7, "most of the true best ten are among the 800 the scan hands on (random data is the worst case)"
    assert len(sketches.scan(prints[:3], question, keep=800)) == 3, "asked for more than there are: all of them"
    assert len(sketches.scan(prints, question, keep=0)) == 0
    # by files: a file with many pages near the question takes a few places, not the whole list
    files = np.arange(5000, dtype=np.int64) // 50          # 100 files of 50 pieces
    files[:2000] = 0                                        # one file holds 2000 pieces, among them 123
    rows = sketches.scan_files(prints, files, question, keep=10)
    assert rows[0] == 123 and len(set(files[rows])) == 10 and max(np.bincount(files[rows])) == sketches.PIECES_EACH
    assert len(rows) <= 10 * sketches.PIECES_EACH
    everything = sketches.scan_files(prints, files, question, keep=1000)
    assert len(set(files[everything])) == 61, "every file is in when more are asked for than there are"
    assert len(sketches.scan_files(prints[:0], files[:0], question)) == 0


@pytest.fixture(params=["fake", "sqlite"])
def store(request, tmp_path):
    if request.param == "fake":
        yield FakeStore()
    else:
        with SqliteStore(tmp_path / "index.sqlite") as made:
            yield made


def test_the_store_keeps_sketches_by_model_and_forgets_them_when_the_text_changes(store):
    one = store.upsert_file(FileRecord("/r/a.txt", 5, 10.0, state=FileState.DONE))
    two = store.upsert_file(FileRecord("/r/b.txt", 5, 20.0, state=FileState.DONE))
    names = store.upsert_file(FileRecord("/r/names only.txt", 5, 30.0, state=FileState.SKIPPED))
    store.replace_segments(one, [SegmentIn(1, 0, SegmentKind.TEXT, "alpha one"), SegmentIn(2, 0, SegmentKind.TEXT, "alpha two")])
    store.replace_segments(two, [SegmentIn(1, 0, SegmentKind.TEXT, "beta")])
    assert [r.path for r in store.unsketched("larger")] == ["/r/b.txt", "/r/a.txt"], "most recently changed first; no text, not asked"
    assert store.sketch_counts("larger") == (0, 2)
    pieces = store.text_of(one)
    assert [(p, t) for _, p, t in pieces] == [(1, "alpha one"), (2, "alpha two")]
    vectors = unit(2, 768, 5)
    data, scales = sketches.quantize(vectors)
    rows = [(seg, scales[i], data[i * 768:(i + 1) * 768], sketches.fingerprint_of(vectors[i])) for i, (seg, _, _) in enumerate(pieces)]
    store.save_sketches(one, "larger", 768, rows)
    assert store.sketch_counts("larger") == (1, 2) and [r.path for r in store.unsketched("larger")] == ["/r/b.txt"]
    assert store.unsketched("small") and store.sketch_counts("small") == (0, 2), "another model's sketches are another thing"
    ids, files, matrix = store.fingerprints("larger")
    assert list(ids) == [seg for seg, _, _ in pieces] and list(files) == [one, one] and matrix.shape == (2, sketches.FINGERPRINT)
    found = store.sketches_of(list(ids) + [999])
    assert set(found) == set(ids) and found[ids[0]][0] == one and found[ids[0]][1] == 768
    back = sketches.dequantize(found[ids[1]][3], 768, [found[ids[1]][2]])[0]
    assert np.abs(back - vectors[1]).max() < 0.02
    store.replace_segments(one, [SegmentIn(1, 0, SegmentKind.TEXT, "alpha changed")])
    assert store.sketch_counts("larger") == (0, 2) and store.fingerprints("larger")[2].shape[0] == 0, "new text, no sketches"
    seg = store.text_of(one)[0][0]
    store.save_sketches(one, "larger", 768, [(seg,) + rows[0][1:]])
    assert store.sketch_counts("larger") == (1, 2)
    store.forget_sketches(one)
    assert store.sketch_counts("larger") == (0, 2)
    with pytest.raises(Exception):
        store.save_sketches(12345, "larger", 768, [])


def test_a_missing_file_s_sketches_are_not_scanned_and_a_removed_file_s_are_gone(tmp_path):
    with SqliteStore(tmp_path / "index.sqlite") as store:
        row = store.upsert_file(FileRecord("/r/a.txt", 5, 10.0, state=FileState.DONE))
        store.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, "alpha")])
        seg = store.text_of(row)[0][0]
        v = unit(1, 768, 6)
        data, scales = sketches.quantize(v)
        store.save_sketches(row, "larger", 768, [(seg, scales[0], data, sketches.fingerprint_of(v[0]))])
        assert store.fingerprints("larger")[2].shape[0] == 1
        store.set_missing(row, 5.0)
        assert store.fingerprints("larger")[2].shape[0] == 0 and store.sketch_counts("larger") == (0, 0)
        store.set_missing(row, None)
        store.remove_files([row])
        assert store.conn.execute("SELECT COUNT(*) FROM sketches").fetchone()[0] == 0, "the cascade"
        store.check_word_index()


def sketched(store, row, vectors, model="larger:768"):
    pieces = store.text_of(row)
    assert len(pieces) == len(vectors)
    data, scales = sketches.quantize(vectors)
    dims = vectors.shape[1]
    store.save_sketches(row, model, dims, [(seg, scales[i], data[i * dims:(i + 1) * dims], sketches.fingerprint_of(vectors[i]))
                                           for i, (seg, _, _) in enumerate(pieces)])


def test_searching_by_meaning_gives_one_row_per_file_with_its_closest_passage_and_pages_like_a_word_search(tmp_path):
    from dugup.shapes import SearchFilters, WHERE_MEANING
    with SqliteStore(tmp_path / "index.sqlite", prefer_under="/main") as store:
        base = unit(6, 768, 7)
        a = store.upsert_file(FileRecord("/main/a.txt", 5, 10.0, state=FileState.DONE))
        b = store.upsert_file(FileRecord("/main/b.pdf", 5, 20.0, state=FileState.DONE, kind=FileKind.PDF))
        c = store.upsert_file(FileRecord("/other/c.txt", 5, 30.0, state=FileState.DONE))
        store.replace_segments(a, [SegmentIn(0, 1, SegmentKind.TEXT, "a first"), SegmentIn(0, 2, SegmentKind.TEXT, "a second, the close one")])
        store.replace_segments(b, [SegmentIn(3, 1, SegmentKind.TEXT, "b on page three")])
        store.replace_segments(c, [SegmentIn(0, 1, SegmentKind.TEXT, "c far away")])
        question = base[0]
        near = lambda w: (w * question + (1 - w) * base[5]) / np.linalg.norm(w * question + (1 - w) * base[5])
        sketched(store, a, np.stack([near(0.2), near(0.9)]))
        sketched(store, b, np.stack([near(0.6)]))
        sketched(store, c, np.stack([near(0.4)]))
        page = store.search_meaning(question, "larger:768", limit=10)
        assert [r.path for r in page.rows] == ["/main/a.txt", "/main/b.pdf", "/other/c.txt"] and page.total == 3
        assert page.rows[0].rank > page.rows[1].rank > page.rows[2].rank > 0.3 and page.rows[0].matches == 1
        ev = page.rows[0].evidence[0]
        assert store.get_passage(ev, 5).text == "a second, " and ev.page == 0, "the closest piece, shown from its start"
        assert page.rows[1].evidence[0].page == 3
        # paged as a word search is paged: the total is honest, the offset continues
        second = store.search_meaning(question, "larger:768", limit=2, offset=2)
        assert [r.path for r in second.rows] == ["/other/c.txt"] and second.total == 3 and not second.truncated
        assert store.search_meaning(question, "larger:768", limit=2).truncated
        # the file filters narrow it; the word filters do not apply
        only = store.search_meaning(question, "larger:768", filters=SearchFilters(folder="/other", where=WHERE_MEANING))
        assert [r.path for r in only.rows] == ["/other/c.txt"] and only.total == 1
        assert store.search_meaning(question, "larger:768", filters=SearchFilters(kinds=("pdf",))).total == 1
        assert store.search_meaning(question, "larger:768", filters=SearchFilters(modified_after=25.0)).total == 1
        # another model's sketches are not this search's
        assert store.search_meaning(question, "small:384").total == 0
        # a file kept in two places is shown once, in the main place, with the other place listed
        twin = store.upsert_file(FileRecord("/other/backup/a.txt", 5, 10.0, state=FileState.DONE))
        store.replace_segments(twin, [SegmentIn(0, 1, SegmentKind.TEXT, "a first"), SegmentIn(0, 2, SegmentKind.TEXT, "a second, the close one")])
        sketched(store, twin, np.stack([near(0.2), near(0.95)]))
        store.set_content(a, b"mark-a")
        store.set_content(twin, b"mark-a")
        grouped = store.search_meaning(question, "larger:768")
        assert [r.path for r in grouped.rows][0] == "/main/a.txt" and grouped.total == 3
        assert grouped.rows[0].also_at == ("/other/backup/a.txt",) and grouped.rows[0].evidence[0].file_id == a
        every = store.search_meaning(question, "larger:768", filters=SearchFilters(every_copy=True))
        assert every.total == 4 and [r.path for r in every.rows][:2] == ["/other/backup/a.txt", "/main/a.txt"]
        # new text takes a file's sketches with it, and the cached fingerprints notice
        store.replace_segments(c, [SegmentIn(0, 1, SegmentKind.TEXT, "c rewritten")])
        assert store.search_meaning(question, "larger:768").total == 2
        with pytest.raises(ValueError):
            store.search_meaning(question, "larger:768", limit=-1)
