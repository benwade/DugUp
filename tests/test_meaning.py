"""Searching by meaning: the sketches, the worker's answer, the fill of the index, and the measurement, with a stand-in model."""

import io
import zlib

import pytest

np = pytest.importorskip("numpy")

from dugup import embedding, meaning
from dugup.embedding_models import BY_CHOICE, present
from dugup.protocols import Limits
from dugup.shapes import Failure, FailureReason, FileRecord, FileState, SegmentIn, SegmentKind
from dugup.store import SqliteStore


def test_sketches_travel_as_text_and_come_back_exactly():
    vectors = np.random.default_rng(1).random((5, 7), dtype=np.float32)
    text, rows, dims = embedding.pack(vectors)
    assert (rows, dims) == (5, 7) and np.array_equal(embedding.unpack(text, rows, dims), vectors)


def test_the_pins_are_whole_and_the_sizes_say_what_they_are():
    for choice, items in BY_CHOICE.items():
        assert len(items) == 2 and all(len(i.sha256) == 64 and i.size > 0 and i.url.startswith("https://") for i in items)
        assert embedding.SHAPES[choice].dims in (384, 768)
    assert embedding.SIZES["full"] == 768 and 96 <= embedding.SIZES["compact"] < embedding.SIZES["full"]
    assert meaning.dims_for("larger", "compact") == 256 and meaning.dims_for("larger", "full") == 768
    assert meaning.dims_for("small", "compact") == 384, "the small model cannot be cut short"
    assert meaning.model_name("larger", 768) == "larger:768"


class StandInSandbox:
    """Answers the embed job with sketches made from the words themselves: texts that share words come out close."""

    calls = 0

    def __init__(self, fail_gpu=False):
        self.fail_gpu = fail_gpu
        self.closed = False

    def run(self, job, payload, limits, stop=None):
        StandInSandbox.calls += 1
        assert job == "dugup.jobs:embed" and isinstance(limits, Limits)
        if payload["gpu"] and self.fail_gpu:
            return {"failure": FailureReason.NOT_INSTALLED.value, "detail": "no card runtime"}
        dims = 768 if payload["choice"] == "larger" else 384
        keep = min(payload["dims"], dims) if payload["dims"] and payload["choice"] == "larger" else dims
        out = np.zeros((len(payload["texts"]), keep), dtype=np.float32)
        for row, text in enumerate(payload["texts"]):
            for word in text.lower().split():
                out[row, zlib.crc32(word.encode()) % keep] += 1.0
        out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)
        packed, rows, d = embedding.pack(out)
        return {"vectors": packed, "rows": rows, "dims": d, "device": "graphics card (stand-in)" if payload["gpu"] else "processor"}

    def close(self):
        self.closed = True


def filled(tmp_path):
    store = SqliteStore(tmp_path / "index.sqlite")
    texts = {"burnout.txt": "nurses on night shifts report burnout and fatigue",
             "budget.txt": "the quarterly budget memo for the department",
             "garden.txt": "tomatoes grow best with sun and water",
             "shifts.txt": "night shifts and the fatigue they bring to nurses"}
    for name, text in texts.items():
        row = store.upsert_file(FileRecord(f"/r/{name}", 10, 1.0, state=FileState.DONE))
        store.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, text)])
    return store


def test_the_measurement_times_each_model_and_device_and_answers_questions(tmp_path):
    store = filled(tmp_path)
    folder = tmp_path / "models"
    folder.mkdir()
    for items in BY_CHOICE.values():
        for item in items:
            (folder / item.name).write_bytes(b"stand-in")      # present, so nothing is fetched
    assert present(folder, "small") and present(folder, "larger")
    told = []
    clock = [0.0]

    def timer():
        clock[0] += 0.5
        return clock[0]

    found = meaning.measure(store, StandInSandbox, folder, pieces=10, questions=["nurse burnout at night"],
                            progress=told.append, timer=timer)
    assert not isinstance(found, Failure), found
    assert found.pieces == 4 and found.in_index == 4
    assert [(t.choice, t.device) for t in found.timings] == [("small", "processor"), ("small", "graphics card (stand-in)"),
                                                              ("larger", "processor"), ("larger", "graphics card (stand-in)")]
    assert all(t.per_second > 0 and t.load_seconds > 0 for t in found.timings)
    assert found.disk_for(768) == 4 * 768 * 4 and found.disk_for(128, 1) == 4 * 128
    by = {(a.choice, a.dims): a for a in found.answers}
    assert set(by) == {("small", 384), ("larger", 768), ("larger", 256), ("larger", 128)}, "each size once, from one run"
    top = by[("larger", 768)].hits
    assert {h.path.rsplit("/", 1)[-1] for h in top[:2]} == {"burnout.txt", "shifts.txt"}, "the texts that share its words come first"
    assert all(0.0 <= found.agreement[("nurse burnout at night", d)] <= 1.0 for d in (256, 128))
    assert any(p.phase == "measure" for p in told)


def test_a_card_that_cannot_be_used_is_a_note_not_a_failure_and_a_bad_answer_is_said(tmp_path):
    store = filled(tmp_path)
    folder = tmp_path / "models"
    folder.mkdir()
    for item in BY_CHOICE["small"]:
        (folder / item.name).write_bytes(b"stand-in")
    found = meaning.measure(store, lambda: StandInSandbox(fail_gpu=True), folder, choices=("small",), pieces=10)
    assert [t.device for t in found.timings] == ["processor"] and any("could not be used" in n for n in found.notes)

    class Broken(StandInSandbox):
        def run(self, job, payload, limits, stop=None):
            return Failure(FailureReason.CRASHED, "the worker died")

    assert meaning.measure(store, Broken, folder, choices=("small",), pieces=10, gpu=False).reason is FailureReason.CRASHED
    empty = SqliteStore(tmp_path / "empty.sqlite")
    assert meaning.measure(empty, StandInSandbox, folder, choices=("small",), pieces=10).reason is FailureReason.OTHER


def test_the_command_prints_the_report(tmp_path, monkeypatch):
    from dugup import cli, service as service_module
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    data = tmp_path / "data"
    data.mkdir()
    with LocalService(DataLayout(data).ensure()) as svc:
        row = svc.store.upsert_file(FileRecord("/r/a.txt", 10, 1.0, state=FileState.DONE))
        svc.store.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, "words about budgets")])
        folder = data / "models" / "meaning"
        folder.mkdir(parents=True)
        for items in BY_CHOICE.values():
            for item in items:
                (folder / item.name).write_bytes(b"stand-in")
    monkeypatch.setattr("dugup.sandbox.SubprocessSandbox", StandInSandbox)
    out = io.StringIO()
    code = cli.main(["meaning", "measure", "--pieces", "5", "--no-gpu", "--ask", "budget", "--data-dir", str(data)], stdout=out)
    text = out.getvalue()
    assert code == 0, text
    assert "Sample: 1 piece(s); the whole index holds 1." in text and "pieces/s" in text and "768 numbers" in text
    assert '"budget"  with larger, 768 numbers' in text and "a.txt" in text and "(keeps" in text


def filled_store(store, modified=None):
    texts = [("burnout.txt", "nurses on night shifts report burnout and fatigue"),
             ("budget.txt", "the quarterly budget memo for the department"),
             ("garden.txt", "tomatoes grow best with sun and water"),
             ("names only.txt", None)]
    for i, (name, text) in enumerate(texts):
        state = FileState.DONE if text else FileState.SKIPPED
        row = store.upsert_file(FileRecord(f"/r/{name}", 10, float(modified[i] if modified else i), state=state))
        if text:
            store.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, text), SegmentIn(2, 0, SegmentKind.TEXT, text + " again")])
    return store


def test_the_fill_sketches_the_most_recently_changed_first_passes_over_folders_that_do_not_ask_and_can_be_stopped(tmp_path):
    import threading
    store = filled_store(SqliteStore(tmp_path / "index.sqlite"))
    runner = meaning._Runner(StandInSandbox(), tmp_path, "larger", False)
    told = []
    report = meaning.fill(store, runner, "larger", 256, wanted=lambda p: "budget" not in p, progress=told.append)
    assert (report.model, report.files, report.pieces, report.left_alone, report.done, report.could) == ("larger:256", 2, 4, 1, 2, 3)
    assert report.device == "processor" and report.failure is None and not report.stopped
    assert [p.current for p in told if p.current] == ["/r/garden.txt", "/r/burnout.txt"], "most recently changed first; budget passed over"
    assert told[-1].phase == "sketch" and (told[-1].done, told[-1].total) == (2, 3)
    ids, prints = store.fingerprints("larger:256")
    assert prints.shape == (4, 96) and all(v[1] == 256 for v in store.sketches_of(list(ids)).values())
    # the folder now asks: the next run takes only what is left
    again = meaning.fill(store, runner, "larger", 256)
    assert (again.files, again.left_alone, again.done) == (1, 0, 3) and meaning.fill(store, runner, "larger", 256).files == 0
    # another size is another model: everything wants a sketch again, and a stop after the first file keeps that one
    stop = threading.Event()

    class StopAfterOne(StandInSandbox):
        def run(self, job, payload, limits, stop=None):
            answer = super().run(job, payload, limits, stop)
            if payload["texts"] != ["ready"]:       # the warm-up does not count
                stop.set()
            return answer

    halted = meaning.fill(store, meaning._Runner(StopAfterOne(), tmp_path, "larger", False), "larger", 768, stop=stop)
    assert halted.stopped and halted.files == 1 and store.sketch_counts("larger:768") == (1, 3)
    assert store.sketch_counts("larger:256") == (2, 3), "the file now sketched at full size no longer counts for compact"
    clock = [0.0]

    def timer():
        clock[0] += 10.0
        return clock[0]

    out_of_time = meaning.fill(store, meaning._Runner(StandInSandbox(), tmp_path, "larger", False), "larger", 768,
                               budget_seconds=5.0, timer=timer)
    assert out_of_time.out_of_time and out_of_time.files == 0

    class Broken(StandInSandbox):
        def run(self, job, payload, limits, stop=None):
            return Failure(FailureReason.CRASHED, "the worker died")

    failed = meaning.fill(store, meaning._Runner(Broken(), tmp_path, "larger", False), "larger", 768)
    assert failed.failure is not None and failed.failure.reason is FailureReason.CRASHED and failed.files == 0


def test_the_fill_command_and_status_and_the_index_run_goes_on_to_sketch(tmp_path, monkeypatch):
    from dugup import cli
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    data = tmp_path / "data"
    data.mkdir()
    with LocalService(DataLayout(data).ensure()) as svc:
        filled_store(svc.store)
        folder = data / "models" / "meaning"
        folder.mkdir(parents=True)
        for items in BY_CHOICE.values():
            for item in items:
                (folder / item.name).write_bytes(b"stand-in")
    monkeypatch.setattr("dugup.sandbox.SubprocessSandbox", StandInSandbox)
    monkeypatch.setattr(LocalService, "meaning_ready", lambda self: True)      # the runtime need not be installed here
    out = io.StringIO()
    assert cli.main(["meaning", "status", "--data-dir", str(data)], stdout=out) == 0
    assert "Sketches of meaning: 0 of 3 file(s) with text (0%), with larger:768." in out.getvalue()
    out = io.StringIO()
    code = cli.main(["meaning", "fill", "--no-gpu", "--data-dir", str(data)], stdout=out)
    text = out.getvalue()
    assert code == 0, text
    assert "Sketching the meaning of 3 file(s) with the larger model (larger:768)." in text
    assert "Sketched 3 file(s), 6 piece(s) of text on the processor" in text and "Now 3 of 3 file(s) with text have sketches (100%)." in text
    out = io.StringIO()
    assert cli.main(["meaning", "status", "--data-dir", str(data)], stdout=out) == 0 and "3 of 3" in out.getvalue()
    # a settings change makes everything want a new sketch, and `dugup index` goes on to it by itself
    out = io.StringIO()
    assert cli.main(["config", "set", "meaning_size", "compact", "--data-dir", str(data)], stdout=out) == 0, out.getvalue()
    root = tmp_path / "root"
    root.mkdir()
    (root / "note.txt").write_text("a note about the garden", encoding="utf-8")
    assert cli.main(["roots", "add", str(root), "--data-dir", str(data)], stdout=io.StringIO()) == 0
    out = io.StringIO()
    code = cli.main(["index", "--no-ocr", "--data-dir", str(data)], stdout=out)
    text = out.getvalue()
    assert "Now sketching the meaning of 4 file(s)" in text and "Now 4 of 4 file(s) with text have sketches" in text, text
    out = io.StringIO()
    cli.main(["index", "--no-ocr", "--data-dir", str(data)], stdout=out)
    assert "Now sketching" not in out.getvalue(), "nothing waits: the step is left out"
    out = io.StringIO()
    assert cli.main(["config", "set", "meaning_after_index", "false", "--data-dir", str(data)], stdout=out) == 0, out.getvalue()
    assert cli.main(["config", "set", "meaning_size", "full", "--data-dir", str(data)], stdout=out) == 0
    out = io.StringIO()
    cli.main(["index", "--no-ocr", "--data-dir", str(data)], stdout=out)
    assert "Now sketching" not in out.getvalue(), "the setting leaves the step out"


def test_an_index_run_does_not_fetch_the_model_by_itself(tmp_path):
    from dugup import cli
    data = tmp_path / "data"
    root = tmp_path / "root"
    root.mkdir()
    (root / "note.txt").write_text("a note", encoding="utf-8")
    assert cli.main(["roots", "add", str(root), "--data-dir", str(data)], stdout=io.StringIO()) == 0
    out = io.StringIO()
    assert cli.main(["index", "--no-ocr", "--data-dir", str(data)], stdout=out) == 0
    text = out.getvalue()
    assert "Sketches of meaning were not made:" in text and ("not fetched yet" in text or "not installed" in text), text


def test_searching_by_meaning_goes_through_the_same_search_and_the_command_says_how_far_the_sketches_reach(tmp_path, monkeypatch):
    from dugup import cli
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    from dugup.shapes import SearchFilters, WHERE_MEANING
    data = tmp_path / "data"
    data.mkdir()
    with LocalService(DataLayout(data).ensure()) as svc:
        filled_store(svc.store)
        row = svc.store.upsert_file(FileRecord("/r/late.txt", 10, 99.0, state=FileState.DONE))
        svc.store.replace_segments(row, [SegmentIn(0, 1, SegmentKind.TEXT, "unsketched so far")])
        folder = data / "models" / "meaning"
        folder.mkdir(parents=True)
        for item in BY_CHOICE["larger"]:
            (folder / item.name).write_bytes(b"stand-in")
    monkeypatch.setattr("dugup.sandbox.SubprocessSandbox", StandInSandbox)
    monkeypatch.setattr(LocalService, "meaning_ready", lambda self: True)
    with LocalService(DataLayout(data).ensure()) as svc:
        # nothing sketched yet: an honest empty page, not a failure
        page = svc.search("night shifts", filters=SearchFilters(where=WHERE_MEANING))
        assert page.total == 0 and page.rows == []
        report = svc.run_meaning(budget_seconds=None, gpu=False)
        assert report.files == 4 and svc.meaning_coverage() == (4, 4)
        page = svc.search("fatigue on night shifts", filters=SearchFilters(where=WHERE_MEANING))
        assert page.rows[0].path == "/r/burnout.txt" and page.rows[0].rank > page.rows[-1].rank
        assert svc.get_evidence(page.rows[0].evidence[0], 80).text.startswith("nurses on night shifts")
        asker = svc._asker
        assert svc.search("tomatoes", filters=SearchFilters(where=WHERE_MEANING)).rows[0].path == "/r/garden.txt"
        assert svc._asker is asker, "the worker that sketches questions is kept between searches"
        # a word search is untouched
        assert svc.search("tomatoes").rows[0].path == "/r/garden.txt"
    out = io.StringIO()
    code = cli.main(["search", "--meaning", "nurses and burnout", "--limit", "2", "--data-dir", str(data)], stdout=out)
    text = out.getvalue()
    assert code == 0, text
    assert "closest in meaning to: nurses and burnout" in text and "burnout.txt  (" in text, text
    assert "p.1: nurses on night shifts report burnout" in text, "the passage from its start, nothing marked"
    assert "More: add --offset 2" in text and "Sketches of meaning cover" not in text, "everything is sketched: no coverage line"
    with LocalService(DataLayout(data).ensure()) as svc:
        row = svc.store.upsert_file(FileRecord("/r/newer.txt", 10, 100.0, state=FileState.DONE))
        svc.store.replace_segments(row, [SegmentIn(0, 1, SegmentKind.TEXT, "newer still")])
    out = io.StringIO()
    cli.main(["search", "--meaning", "nurses", "--data-dir", str(data)], stdout=out)
    assert "(Sketches of meaning cover 4 of 5 file(s) with text so far; `dugup meaning fill` continues.)" in out.getvalue()


def test_searching_by_meaning_without_the_runtime_or_the_model_says_what_is_missing(tmp_path):
    from dugup import cli
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    from dugup.shapes import SearchFilters, WHERE_MEANING
    data = tmp_path / "data"
    data.mkdir()
    with LocalService(DataLayout(data).ensure()) as svc:
        filled_store(svc.store)
        page = svc.search("anything", filters=SearchFilters(where=WHERE_MEANING))
        assert isinstance(page, Failure) and page.reason is FailureReason.NOT_INSTALLED
        assert "not fetched yet" in page.detail or "not installed" in page.detail
    out = io.StringIO()
    assert cli.main(["search", "--meaning", "anything", "--data-dir", str(data)], stdout=out) == 1
    assert "Could not do that" in out.getvalue()
