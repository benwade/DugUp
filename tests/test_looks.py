"""Pictures by likeness, measured: the worker's answer, the measurement with a stand-in model, and the command."""

import io
import os
import zlib

import pytest

np = pytest.importorskip("numpy")

from dugup import embedding, looks
from dugup.embedding_models import LOOKS_BY_CHOICE, looks_present
from dugup.protocols import Limits
from dugup.shapes import Failure, FailureReason, FileKind, FileRecord, FileState
from dugup.store import SqliteStore


def test_the_picture_pins_are_whole():
    items = LOOKS_BY_CHOICE["base"]
    assert len(items) == 3 and all(len(i.sha256) == 64 and i.size > 0 and i.url.startswith("https://") for i in items)
    assert {i.name for i in items} == set(embedding.LOOKS_FILES.values())


def test_a_picture_is_prepared_the_way_the_model_wants_it():
    PIL = pytest.importorskip("PIL")
    from PIL import Image
    wide = Image.new("RGB", (640, 200), (255, 0, 0))
    array = embedding.Looker.prepare(wide)
    assert array.shape == (3, embedding.LOOKS_SIDE, embedding.LOOKS_SIDE) and array.dtype == np.float32
    assert abs(float(array[0].mean()) - (1.0 - embedding.LOOKS_MEAN[0]) / embedding.LOOKS_STD[0]) < 0.01, "red, normalised"
    tall = Image.new("L", (100, 900), 0)
    assert embedding.Looker.prepare(tall).shape == (3, embedding.LOOKS_SIDE, embedding.LOOKS_SIDE), "grey, upscaled, cropped"


class StandInSandbox:
    """Answers the look job from the pictures' own pixels (their mean colour, spread into 512 numbers) and from
    the words of a sentence, so that red things are near "red" and like pictures are near each other."""

    def __init__(self, fail_gpu=False):
        self.fail_gpu = fail_gpu
        self.closed = False

    @staticmethod
    def _vector(parts):
        v = np.zeros(embedding.LOOKS_DIMS, dtype=np.float32)
        for part, weight in parts:
            v[zlib.crc32(part.encode()) % embedding.LOOKS_DIMS] += weight
        return v / max(float(np.linalg.norm(v)), 1e-9)

    def run(self, job, payload, limits, stop=None):
        assert job == "dugup.jobs:look" and isinstance(limits, Limits)
        if payload["gpu"] and self.fail_gpu:
            return {"failure": FailureReason.NOT_INSTALLED.value, "detail": "no card runtime"}
        device = "graphics card (stand-in)" if payload["gpu"] else "processor"
        if "sentences" in payload:
            rows = [self._vector([(w, 1.0) for w in s.lower().split()]) for s in payload["sentences"]]
            packed, n, d = embedding.pack(np.stack(rows))
            return {"vectors": packed, "rows": n, "dims": d, "device": device, "failed": {}}
        from PIL import Image
        rows, failed = [], {}
        for i, path in enumerate(payload["paths"]):
            try:
                with Image.open(path) as picture:
                    r, g, b = picture.convert("RGB").resize((1, 1)).getpixel((0, 0))
                name = "red" if r > 150 and g < 100 else "blue" if b > 150 and r < 100 else "white"
                rows.append(self._vector([(name, 1.0), ("picture", 0.3)]))
            except Exception as exc:
                failed[str(i)] = type(exc).__name__
                rows.append(np.zeros(embedding.LOOKS_DIMS, dtype=np.float32))
        packed, n, d = embedding.pack(np.stack(rows))
        return {"vectors": packed, "rows": n, "dims": d, "device": device, "failed": failed}

    def close(self):
        self.closed = True


def pictures(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image
    folder = tmp_path / "pics"
    folder.mkdir()
    store = SqliteStore(tmp_path / "index.sqlite")
    for name, colour in (("red1.png", (220, 20, 20)), ("red2.png", (200, 30, 30)), ("blue.png", (20, 20, 220)), ("white.png", (250, 250, 250))):
        Image.new("RGB", (40, 30), colour).save(folder / name)
        store.upsert_file(FileRecord(str(folder / name), 100, 1.0, kind=FileKind.IMAGE, state=FileState.DONE))
    (folder / "broken.jpg").write_bytes(b"no")
    (folder / "odd.xyz").write_bytes(b"no")
    store.upsert_file(FileRecord(str(folder / "odd.xyz"), 2, 1.0, kind=FileKind.IMAGE, state=FileState.DONE))
    store.upsert_file(FileRecord(str(folder / "broken.jpg"), 2, 1.0, kind=FileKind.IMAGE, state=FileState.DONE))
    store.upsert_file(FileRecord(str(folder / "note.txt"), 5, 1.0, state=FileState.DONE))
    return store, folder


def test_the_measurement_times_each_device_answers_sentences_and_finds_likes(tmp_path):
    store, folder = pictures(tmp_path)
    models = tmp_path / "models"
    models.mkdir()
    for item in LOOKS_BY_CHOICE["base"]:
        (models / item.name).write_bytes(b"stand-in")
    assert looks_present(models)
    told = []
    found = looks.measure(store, StandInSandbox, models, pictures=10, questions=["red"], like=[str(folder / "red1.png")],
                          progress=told.append)
    assert not isinstance(found, Failure), found
    assert found.pictures == 6 and found.in_index == 6, "pictures only; the note is not among them"
    assert [t.device for t in found.timings] == ["processor", "graphics card (stand-in)"]
    assert all(t.pictures == 4 and t.failed == 2 and t.load_seconds >= 0 for t in found.timings)
    assert found.disk_for(1) == 6 * embedding.LOOKS_DIMS
    by = {a.asked: a for a in found.answers}
    assert {os.path.basename(h.path) for h in by["red"].hits[:2]} == {"red1.png", "red2.png"}
    like = by[f"like {folder / 'red1.png'}"]
    assert os.path.basename(like.hits[0].path) == "red2.png" and all("red1" not in h.path for h in like.hits), "its likes, not itself"
    assert all("broken" not in h.path for a in found.answers for h in a.hits)
    assert sorted(p for ps in found.unread.values() for p in ps) == sorted([str(folder / "broken.jpg"), str(folder / "odd.xyz")])
    assert any(p.phase == "look" for p in told)
    # a card that cannot be used is a note; no pictures is a failure; an example that cannot be read is a note
    noted = looks.measure(store, lambda: StandInSandbox(fail_gpu=True), models, pictures=10, like=[str(folder / "broken.jpg")])
    assert [t.device for t in noted.timings] == ["processor"] and any("could not be used" in n for n in noted.notes)
    assert any("example could not be read" in n for n in noted.notes)
    empty = SqliteStore(tmp_path / "empty.sqlite")
    assert looks.measure(empty, StandInSandbox, models, pictures=10).reason is FailureReason.OTHER


def test_the_command_prints_the_report(tmp_path, monkeypatch):
    from dugup import cli
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    pytest.importorskip("PIL")
    from PIL import Image
    data = tmp_path / "data"
    data.mkdir()
    folder = tmp_path / "pics"
    folder.mkdir()
    Image.new("RGB", (40, 30), (220, 20, 20)).save(folder / "red.png")
    with LocalService(DataLayout(data).ensure()) as svc:
        svc.store.upsert_file(FileRecord(str(folder / "red.png"), 100, 1.0, kind=FileKind.IMAGE, state=FileState.DONE))
        models = data / "models" / "looks"
        models.mkdir(parents=True)
        for item in LOOKS_BY_CHOICE["base"]:
            (models / item.name).write_bytes(b"stand-in")
    monkeypatch.setattr("dugup.sandbox.SubprocessSandbox", StandInSandbox)
    out = io.StringIO()
    code = cli.main(["alike", "measure", "--pictures", "5", "--no-gpu", "--ask", "red", "--data-dir", str(data)], stdout=out)
    text = out.getvalue()
    assert code == 0, text
    assert "Sample: 1 picture(s); the index lists 1." in text and "pictures/s" in text and '"red"' in text and "red.png" in text
    assert "Could not be read" not in text


@pytest.mark.skipif(not os.environ.get("DUGUP_LOOKS_MODELS"), reason="set DUGUP_LOOKS_MODELS to a folder holding the picture model to run this")
def test_the_real_model_tells_a_red_circle_from_a_blue_square():
    from PIL import Image, ImageDraw
    looker = embedding.Looker(os.environ["DUGUP_LOOKS_MODELS"])
    red = Image.new("RGB", (300, 300), "white")
    ImageDraw.Draw(red).ellipse((50, 50, 250, 250), fill="red")
    blue = Image.new("RGB", (300, 300), "white")
    ImageDraw.Draw(blue).rectangle((50, 50, 250, 250), fill="blue")
    v = looker.look([embedding.Looker.prepare(red), embedding.Looker.prepare(blue)])
    q = looker.read(["a red circle", "a blue square"])
    scores = v @ q.T
    assert scores[0, 0] > scores[0, 1] and scores[1, 1] > scores[1, 0]
    assert abs(float(np.linalg.norm(v[0])) - 1.0) < 1e-4


def test_the_look_job_says_why_a_picture_could_not_be_read(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from dugup import jobs
    (tmp_path / "broken.jpg").write_bytes(b"no")
    (tmp_path / "odd.heic").write_bytes(b"no")
    monkeypatch.setattr("dugup.embedding.Looker.__init__", lambda self, folder, gpu=False, threads=0: setattr(self, "device", "processor"))
    monkeypatch.setattr("dugup.embedding.Looker.look", lambda self, pictures, batch=16: np.zeros((len(pictures), 512), dtype=np.float32))
    answer = jobs.look({"folder": str(tmp_path), "paths": [jobs.path_to_data(str(tmp_path / n)) for n in ("broken.jpg", "odd.heic", "gone.png")]})
    assert answer["rows"] == 3 and answer["failed"] == {"0": "damaged, or not really a .jpg",
                                                         "1": "a picture format this program cannot open (.heic)",
                                                         "2": "the file was not there"}
