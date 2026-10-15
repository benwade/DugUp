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
        packed, n, d = embedding.pack(np.stack(rows) if rows else np.zeros((0, embedding.LOOKS_DIMS), dtype=np.float32))
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


@pytest.fixture(params=["fake", "sqlite"])
def picture_store(request, tmp_path):
    from fakes.fake_store import FakeStore
    if request.param == "fake":
        yield FakeStore()
    else:
        with SqliteStore(tmp_path / "index.sqlite") as made:
            yield made


def test_the_fill_looks_with_readers_side_by_side_notes_unread_pictures_and_continues_where_it_stopped(picture_store, tmp_path):
    import threading
    store = picture_store
    pytest.importorskip("PIL")
    from PIL import Image
    folder = tmp_path / "pics"
    folder.mkdir()
    for n in range(70):
        Image.new("RGB", (20, 20), (220, 20, 20) if n % 2 else (20, 20, 220)).save(folder / f"p{n:02d}.png")
        store.upsert_file(FileRecord(str(folder / f"p{n:02d}.png"), 100, float(n), kind=FileKind.IMAGE, state=FileState.DONE))
    (folder / "bad.png").write_bytes(b"no")
    store.upsert_file(FileRecord(str(folder / "bad.png"), 2, 100.0, kind=FileKind.IMAGE, state=FileState.DONE))
    (folder / "other" ).mkdir()
    Image.new("RGB", (20, 20)).save(folder / "other" / "skip.png")
    store.upsert_file(FileRecord(str(folder / "other" / "skip.png"), 100, 200.0, kind=FileKind.IMAGE, state=FileState.DONE))
    store.upsert_file(FileRecord(str(folder / "note.txt"), 5, 300.0, state=FileState.DONE))
    runners = [looks._Runner(StandInSandbox(), tmp_path, False) for _ in range(3)]
    told = []
    report = looks.fill(store, runners, wanted=lambda p: "other" not in p, progress=told.append, batch=8)
    assert (report.pictures, report.unread, report.left_alone, report.readers) == (70, 1, 1, 3)
    assert (report.done, report.could) == (71, 72) and report.failure is None and not report.stopped
    assert told[0].current.endswith("bad.png") or told[0].done > 0, "progress is reported batch by batch"
    assert store.look_counts(report.model) == (71, 72, 1) and store.unread_pictures(report.model) == [(str(folder / "bad.png"), "UnidentifiedImageError")]   # the stand-in's own words
    ids, prints = store.look_fingerprints(report.model)
    assert prints.shape == (70, 96) and len(set(ids)) == 70, "one fingerprint a readable picture"
    # the next run takes only what is left; the unread one is not tried again
    again = looks.fill(store, runners, batch=8)
    assert (again.pictures, again.unread, again.left_alone) == (1, 0, 0), "the folder now asks; bad.png is left as noted"
    # a changed picture wants a new look; an unchanged one does not
    changed = store.get_file(str(folder / "p01.png"))
    store.upsert_file(FileRecord(changed.path, 101, changed.modified, kind=FileKind.IMAGE, state=FileState.DONE))
    store.upsert_file(FileRecord(str(folder / "p02.png"), 100, 2.0, kind=FileKind.IMAGE, state=FileState.DONE))
    assert [os.path.basename(r.path) for r in store.unlooked(report.model)] == ["p01.png"]
    # a stop between batches keeps what was done
    stop = threading.Event()

    class StopAfterFirst(StandInSandbox):
        def run(self, job, payload, limits, stop=None):
            answer = super().run(job, payload, limits, stop)
            if payload.get("paths"):
                stop_event.set()
            return answer

    stop_event = stop
    for n in range(70, 90):
        Image.new("RGB", (20, 20)).save(folder / f"q{n}.png")
        store.upsert_file(FileRecord(str(folder / f"q{n}.png"), 100, float(n), kind=FileKind.IMAGE, state=FileState.DONE))
    halted = looks.fill(store, [looks._Runner(StopAfterFirst(), tmp_path, False)], stop=stop, batch=8)
    assert halted.stopped and 0 < halted.pictures < 21
    # out of time, out of room, a broken worker
    clock = [0.0]

    def timer():
        clock[0] += 10.0
        return clock[0]

    assert looks.fill(store, runners, budget_seconds=5.0, timer=timer).out_of_time
    assert looks.fill(store, runners, room=lambda: "no room").paused == "no room"

    class Broken(StandInSandbox):
        def run(self, job, payload, limits, stop=None):
            return Failure(FailureReason.CRASHED, "the worker died")

    failed = looks.fill(store, [looks._Runner(Broken(), tmp_path, False)])
    assert failed.failure is not None and failed.failure.reason is FailureReason.CRASHED


def test_searching_by_looks_finds_the_closest_pictures_with_filters_and_leaves_the_example_out(picture_store, tmp_path):
    from dugup import sketches
    from dugup.shapes import SearchFilters
    store = picture_store
    model = looks.model_name()
    rng = np.random.default_rng(5)

    def unit(seed):
        v = rng.standard_normal(embedding.LOOKS_DIMS).astype(np.float32)
        return v / np.linalg.norm(v)

    question = unit(0)
    far = unit(1)
    rows = []
    for name, w, when in (("a.png", 0.9, 10.0), ("b.png", 0.6, 20.0), ("c.png", 0.2, 30.0), ("d.png", 0.95, 5.0)):
        r = store.upsert_file(FileRecord(f"/pics/{name}", 10, when, kind=FileKind.IMAGE, state=FileState.DONE))
        v = w * question + (1 - w) * far
        v = v / np.linalg.norm(v)
        data, scales = sketches.quantize(v[None, :])
        rows.append((r, scales[0], data, sketches.fingerprint_of(v), ""))
    rows.append((store.upsert_file(FileRecord("/pics/bad.png", 1, 1.0, kind=FileKind.IMAGE, state=FileState.DONE)), 0.0, None, None, "damaged"))
    store.save_looks(model, rows)
    page = store.search_looks(question, model)
    assert [r.name for r in page.rows] == ["d.png", "a.png", "b.png", "c.png"] and page.total == 4 and page.rows[0].rank > 0.9
    assert [r.name for r in store.search_looks(question, model, not_file=rows[3][0]).rows] == ["a.png", "b.png", "c.png"]
    assert [r.name for r in store.search_looks(question, model, limit=2, offset=1).rows] == ["a.png", "b.png"]
    assert [r.name for r in store.search_looks(question, model, filters=SearchFilters(modified_after=15.0)).rows] == ["b.png", "c.png"]
    assert [r.name for r in store.search_looks(question, model, sort="modified", descending=True).rows] == ["c.png", "b.png", "a.png", "d.png"]
    assert store.search_looks(question, "other:model").total == 0
    like = store.look_of(rows[0][0])
    assert like is not None and float(like @ question) > 0.85 and abs(float(np.linalg.norm(like)) - 1.0) < 0.02
    assert store.look_of(rows[4][0]) is None
    if hasattr(store, "remove_files"):
        store.set_missing(rows[3][0], 1.0)
        assert [r.name for r in store.search_looks(question, model).rows][0] == "a.png", "a missing picture is not found"


def test_the_fill_and_like_commands_the_search_by_looks_and_the_index_run_going_on_to_pictures(tmp_path, monkeypatch):
    from dugup import cli
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    from dugup.shapes import SearchFilters, WHERE_LOOKS
    pytest.importorskip("PIL")
    from PIL import Image
    data = tmp_path / "data"
    root = tmp_path / "root"
    root.mkdir()
    for name, colour in (("red.png", (220, 20, 20)), ("blue.png", (20, 20, 220)), ("red2.png", (210, 25, 25))):
        Image.new("RGB", (30, 30), colour).save(root / name)
    (root / "note.txt").write_text("words", encoding="utf-8")
    assert cli.main(["roots", "add", str(root), "--data-dir", str(data)], stdout=io.StringIO()) == 0
    out = io.StringIO()
    assert cli.main(["index", "--no-ocr", "--no-meaning", "--data-dir", str(data)], stdout=out) == 0
    assert "Pictures were not sketched: the picture model is not fetched yet" in out.getvalue() or "not installed" in out.getvalue()
    models = data / "models" / "looks"
    models.mkdir(parents=True)
    for item in LOOKS_BY_CHOICE["base"]:
        (models / item.name).write_bytes(b"stand-in")
    monkeypatch.setattr("dugup.sandbox.SubprocessSandbox", StandInSandbox)
    monkeypatch.setattr(LocalService, "looks_ready", lambda self: True)
    out = io.StringIO()
    assert cli.main(["alike", "status", "--data-dir", str(data)], stdout=out) == 0 and "Sketches of pictures: 0 of 3 (0%)" in out.getvalue()
    out = io.StringIO()
    code = cli.main(["alike", "fill", "--no-gpu", "--readers", "2", "--data-dir", str(data)], stdout=out)
    text = out.getvalue()
    assert code == 0, text
    assert "Looking at 3 picture(s) with 2 reader(s) side by side" in text and "Looked at 3 picture(s) on the processor, 2 reader(s)" in text
    assert "Now 3 of 3 picture(s) have sketches (100%)." in text
    out = io.StringIO()
    assert cli.main(["search", "--looks", "red", "--data-dir", str(data)], stdout=out) == 0
    text = out.getvalue()
    assert "that look most like: red" in text and text.index("red.png") < text.index("blue.png") and "Sketches cover" not in text
    out = io.StringIO()
    assert cli.main(["alike", "like", str(root / "red.png"), "--data-dir", str(data)], stdout=out) == 0
    text = out.getvalue()
    assert "that look most like: red.png" in text and "red2.png" in text and "  1. red.png" not in text, "its likes, not itself"
    Image.new("RGB", (30, 30), (230, 10, 10)).save(tmp_path / "outside.png")
    out = io.StringIO()
    assert cli.main(["alike", "like", str(tmp_path / "outside.png"), "--data-dir", str(data)], stdout=out) == 0
    assert "red.png" in out.getvalue(), "a picture not in the index is read and sketched now"
    with LocalService(DataLayout(data).ensure()) as svc:
        page = svc.search("red", filters=SearchFilters(where=WHERE_LOOKS))
        assert page.rows[0].name in ("red.png", "red2.png") and page.total == 3
        assert isinstance(svc.search_like(99999), Failure)
    # an update goes on to new pictures by itself
    Image.new("RGB", (30, 30), (20, 220, 20)).save(root / "green.png")
    out = io.StringIO()
    assert cli.main(["index", "--no-ocr", "--no-meaning", "--data-dir", str(data)], stdout=out) == 0
    assert "Now looking at 1 picture(s)" in out.getvalue() and "Now 4 of 4 picture(s) have sketches" in out.getvalue()
    out = io.StringIO()
    cli.main(["index", "--no-ocr", "--no-meaning", "--no-looks", "--data-dir", str(data)], stdout=out)
    assert "Now looking at" not in out.getvalue()


def test_near_duplicate_sets_have_a_keeper_by_rule_leave_protected_pictures_aside_and_the_command_lists_and_removes(tmp_path, monkeypatch):
    from dugup import alikesets, cli, sketches
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    from dugup.settings import Settings
    pytest.importorskip("PIL")
    from PIL import Image
    root = tmp_path / "root"
    for sub in ("Album", "Laptop", "Downloads"):
        (root / sub).mkdir(parents=True)
    rng = np.random.default_rng(9)

    def unit():
        v = rng.standard_normal(embedding.LOOKS_DIMS).astype(np.float32)
        return v / np.linalg.norm(v)

    scene, other = unit(), unit()
    shots = {
        "Album/photo.jpg": (scene, (80, 60), 1.0),          # the original: the larger file
        "Downloads/photo (1).jpg": (0.99 * scene + 0.02 * other, (40, 30), 2.0),
        "Downloads/photo (2).jpg": (0.9 * scene + 0.2 * other, (40, 30), 3.0),
        "Laptop/photo.jpg": (scene, (40, 30), 4.0),         # protected: shown aside, never an extra
        "Album/elsewhere.jpg": (other, (40, 30), 5.0),
    }
    with LocalService(DataLayout(tmp_path / "data").ensure(), Settings(cleanup_protect=str(root / "Laptop"))) as svc:
        store = svc.store
        rows = []
        for name, (v, size, when) in shots.items():
            Image.new("RGB", size, (200, 10, 10)).save(root / name, quality=95)
            record = FileRecord(str(root / name), os.path.getsize(root / name), when, kind=FileKind.IMAGE, state=FileState.DONE)
            store.upsert_file(record)
            v = v / np.linalg.norm(v)
            data, scales = sketches.quantize(v[None, :])
            rows.append((record.row_id, scales[0], data, sketches.fingerprint_of(v), ""))
        store.save_looks(looks.model_name(), rows)
        told = []
        found = svc.alike_pictures(0.97, progress=lambda d, t: told.append((d, t)))
        assert found.pictures == 5 and len(found.sets) == 1 and told[-1] == (5, 5)
        same = found.sets[0]
        assert same.keep.record.path == str(root / "Album" / "photo.jpg") and same.keep.why == "the larger file"
        assert [os.path.basename(m.record.path) for m in same.extra] == ["photo (1).jpg", "photo (2).jpg"], "closest to the keeper first"
        assert all(0.97 <= m.closeness <= 1.0 for m in same.extra)
        assert [os.path.basename(m.record.path) for m in same.aside] == ["photo.jpg"] and same.aside[0].protected
        assert found.extra_pictures == 2 and found.bytes_extra == sum(m.record.size for m in same.extra)
        # a preferred folder wins over size; a stricter threshold loses the farther shot
        liked = svc.alike_pictures(0.97, preferred=[str(root / "Downloads")])
        assert liked.sets[0].keep.why == "in a preferred folder" and liked.sets[0].keep.record.path.startswith(str(root / "Downloads"))
        assert [os.path.basename(m.record.path) for m in svc.alike_pictures(0.995).sets[0].extra] == ["photo (1).jpg"], "a stricter threshold loses the farther shot"
        assert alikesets.choose_keeper([FileRecord("/a/x.jpg", 5, 2.0), FileRecord("/a/y.jpg", 5, 1.0)], (), lambda p: p)[1] == "the older one"
        assert alikesets.choose_keeper([FileRecord("/a/x.jpg", 5, 1.0), FileRecord("/a/longer.jpg", 5, 1.0)], (), lambda p: p)[0].path == "/a/x.jpg"
    out = io.StringIO()
    data = tmp_path / "data"
    assert cli.main(["alike", "duplicates", "--protect", str(root / "Laptop"), "--data-dir", str(data)], stdout=out) == 0
    text = out.getvalue()
    assert "1 set(s), 2 extra picture(s)" in text and "keep   " in text and "(the larger file)" in text and "kept aside (protected)" in text
    assert "Nothing was moved" in text
    out = io.StringIO()
    assert cli.main(["alike", "duplicates", "--remove", "--data-dir", str(data)], stdout=out) == 1 and "needs --record" in out.getvalue()
    moved = []
    monkeypatch.setattr("dugup.trash.to_trash", lambda path, mover=None, _m=moved: (_m.append(path), os.remove(path), "")[2])
    record = tmp_path / "moved.txt"
    out = io.StringIO()
    assert cli.main(["alike", "duplicates", "--protect", str(root / "Laptop"), "--remove", "--record", str(record), "--yes",
                     "--data-dir", str(data)], stdout=out) == 0
    assert sorted(os.path.basename(p) for p in moved) == ["photo (1).jpg", "photo (2).jpg"] and (root / "Laptop" / "photo.jpg").exists()
    assert "Moved 2 picture(s) to the trash" in out.getvalue() and record.read_text(encoding="utf-8-sig").count("trashed\t") == 2
