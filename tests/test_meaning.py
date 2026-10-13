"""Searching by meaning, measured: the sketches, the worker's answer, and the measurement with a stand-in model."""

import io

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
    assert embedding.SIZES["full"] == 768 and embedding.SIZES["compact"] < embedding.SIZES["balanced"] < embedding.SIZES["full"]


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
                out[row, hash(word) % keep] += 1.0
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
