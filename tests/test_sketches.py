"""Sketches of meaning kept as bytes, found in two stages, and the store's part in it."""

import pytest

np = pytest.importorskip("numpy")

from dugup import sketches
from dugup.shapes import FileRecord, FileState, SegmentIn, SegmentKind
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
    ids, matrix = store.fingerprints("larger")
    assert list(ids) == [seg for seg, _, _ in pieces] and matrix.shape == (2, sketches.FINGERPRINT)
    found = store.sketches_of(list(ids) + [999])
    assert set(found) == set(ids) and found[ids[0]][0] == one and found[ids[0]][1] == 768
    back = sketches.dequantize(found[ids[1]][3], 768, [found[ids[1]][2]])[0]
    assert np.abs(back - vectors[1]).max() < 0.02
    store.replace_segments(one, [SegmentIn(1, 0, SegmentKind.TEXT, "alpha changed")])
    assert store.sketch_counts("larger") == (0, 2) and store.fingerprints("larger")[1].shape[0] == 0, "new text, no sketches"
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
        assert store.fingerprints("larger")[1].shape[0] == 1
        store.set_missing(row, 5.0)
        assert store.fingerprints("larger")[1].shape[0] == 0 and store.sketch_counts("larger") == (0, 0)
        store.set_missing(row, None)
        store.remove_files([row])
        assert store.conn.execute("SELECT COUNT(*) FROM sketches").fetchone()[0] == 0, "the cascade"
        store.check_word_index()
