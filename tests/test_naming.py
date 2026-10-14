"""Naming pictures by what they show: the pattern, the safe words, the plan, applying with a record, undo,
and the command with a stand-in captioner."""

import io
import os

import pytest

from dugup import naming
from dugup.shapes import Failure, FailureReason, FileKind, FileRecord, FileState
from dugup.store import SqliteStore


def test_patterns_are_checked_and_sentences_become_safe_words():
    assert naming.check_pattern("{caption} {taken}") == ""
    assert "unknown field" in naming.check_pattern("{caption} {colour}")
    assert "names no field" in naming.check_pattern("photo")
    assert "cannot" in naming.check_pattern("{caption}: {taken}")
    assert naming.safe_words("A green car parked in front of a yellow building.") == "Green car parked in front of yellow building"
    assert naming.safe_words("There is a dog on the beach") == "Dog on beach"
    assert naming.safe_words("a photo of two dogs playing in snow by a lake at dusk, with trees and a big red barn") == \
        "Two dogs playing in snow by lake at dusk with trees", "cut at a whole phrase (a word more to finish it), never on a dangling word"
    assert naming.safe_words("A woman in a red dress stands in front of a body of water near the pier") == "Woman in red dress stands in front of body of water", "a noun that needs its of-phrase is kept whole"
    assert naming.safe_words("A man in a car with a hat on.") == "Man in car with hat on", "a sentence that ends on a particle is left as written"
    for name in ("IMG_20220321_224244.jpg", "20220401_105609.jpg", "DSC_0012.JPG", "Screenshot 2025-03-01 101010.png", "PXL_20230101_123.jpg",
                 "IMG_1234 (2).jpg", "image.png", "1.jpg", "20240916_220851 copy.jpg", "a9f3c2d1e8b7a6c5d4e3f2a1.jpg"):
        assert naming.is_meaningless(name), name
    for name in ("Alex Rivera MA License BACK.jpg", "Grandma_Rivera.jpg", "Brain-blue.jpg", "Shot on OnePlus.jpg", "Alex.jpg", "holiday 2024 day 3.jpg"):
        assert not naming.is_meaningless(name), name
    assert naming.safe_words("") == "" and naming.safe_words("the the the") == "The the the", "nothing but filler: kept rather than empty"
    assert naming.safe_name('what: "a" <name>?') == "what a name" and naming.safe_name("con") == "con_" and naming.safe_name("...") == "picture"
    assert len(naming.safe_name("x" * 500)) == naming.MAX_NAME


def test_the_plan_fills_the_pattern_numbers_clashes_and_leaves_protected_and_unchanged_files_alone(tmp_path):
    folder = tmp_path / "Holiday"
    folder.mkdir()
    (tmp_path / "Laptop").mkdir()
    records = []
    for name, when in (("IMG_1.jpg", 1.0), ("IMG_2.jpg", 2.0), ("IMG_3.png", 3.0), ("Dog on beach 1970-01-01.jpg", 4.0), ("Grandma at the lake.jpg", 6.0)):
        (folder / name).write_bytes(b"x")
        records.append(FileRecord(str(folder / name), 1, when, kind=FileKind.IMAGE, state=FileState.DONE))
    (tmp_path / "Laptop" / "IMG_9.jpg").write_bytes(b"x")
    records.append(FileRecord(str(tmp_path / "Laptop" / "IMG_9.jpg"), 1, 5.0, kind=FileKind.IMAGE, state=FileState.DONE))
    captions = {records[0].path: "a dog on the beach", records[1].path: "A dog on the beach.", records[2].path: "",
                records[3].path: "a dog on the beach", records[4].path: "an old woman", records[5].path: "a laptop"}
    clock = lambda modified: "1970-01-01 00-00"
    changes = naming.plan(records, "{caption} {taken}", captions, clock, protected=[str(tmp_path / "Laptop")])
    by = {os.path.basename(c.old): c for c in changes}
    assert by["IMG_1.jpg"].new == str(folder / "Dog on beach 1970-01-01 (1).jpg"), "its twin wants the same name: numbered"
    assert by["IMG_2.jpg"].new == str(folder / "Dog on beach 1970-01-01 (2).jpg")
    assert by["IMG_3.png"].new == "" and by["IMG_3.png"].why == "no caption could be made for it"
    assert by["Dog on beach 1970-01-01.jpg"].new == "" and "already" in by["Dog on beach 1970-01-01.jpg"].why
    assert by["IMG_9.jpg"].new == "" and by["IMG_9.jpg"].why == "in a protected folder"
    assert by["Grandma at the lake.jpg"].new == "" and by["Grandma at the lake.jpg"].why == "its name already says something"
    assert naming.plan(records[4:5], "{caption} {taken}", captions, clock, every=True)[0].new == str(folder / "Old woman 1970-01-01.jpg")
    # a name that is free but held by an existing file on disk gets a number too; other fields fill
    (folder / "Dog on beach 1970-01-01 (1).jpg").write_bytes(b"there")
    again = naming.plan(records[:1], "{caption} {taken}", captions, clock)
    assert again[0].new == str(folder / "Dog on beach 1970-01-01 (2).jpg")
    other = naming.plan(records[:1], "{folder} {original} {time}", captions, clock)
    assert other[0].new == str(folder / "Holiday IMG_1 00-00.jpg")
    assert naming.plan(records[:1], "{original}.{ext}", captions, clock)[0].new == "", "the same name again is left alone"
    assert naming.plan(records[:1], "{original}-copy.{ext}", captions, clock)[0].new == str(folder / "IMG_1-copy.jpg")


def test_apply_renames_what_is_unchanged_records_each_step_and_undo_puts_the_names_back(tmp_path):
    folder = tmp_path / "pics"
    folder.mkdir()
    with SqliteStore(tmp_path / "index.sqlite") as store:
        records = []
        for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
            (folder / name).write_bytes(b"x")
            stat = os.stat(folder / name)
            record = FileRecord(str(folder / name), stat.st_size, stat.st_mtime, kind=FileKind.IMAGE, state=FileState.DONE)
            store.upsert_file(record)
            records.append(record)
        clock = lambda modified: "2020-02-02 10-10"
        captions = {r.path: f"picture {n}" for n, r in enumerate(records)}
        changes = naming.plan(records, "{caption}", captions, clock)
        (folder / "b.jpg").write_bytes(b"changed since")           # bigger: changed since the preview
        (folder / "c.jpg").unlink()
        (folder / "Picture 3.jpg").write_bytes(b"taken")            # d's new name is now taken
        lines = []
        done = naming.apply(changes, store, journal=lines.append)
        assert [os.path.basename(o) for o, _ in done.renamed] == ["a.jpg"]
        assert sorted(why for _, why in done.skipped) == ["a file with the new name is there now", "it changed since the preview", "it is not there now"]
        assert (folder / "Picture 0.jpg").exists() and not (folder / "a.jpg").exists()
        assert store.get_file(str(folder / "Picture 0.jpg")) is not None and store.get_file(str(folder / "a.jpg")) is None, "the index followed"
        assert lines[0].startswith("renamed\t") and sum(1 for l in lines if l.startswith("left\t")) == 3
        pairs = naming.read_record("pattern\t{caption}\n" + "\n".join(lines))
        assert pairs == [(str(folder / "a.jpg"), str(folder / "Picture 0.jpg"))]
        back = naming.undo(pairs, store)
        assert back.renamed == [(str(folder / "Picture 0.jpg"), str(folder / "a.jpg"))] and (folder / "a.jpg").exists()
        assert store.get_file(str(folder / "a.jpg")) is not None
        assert naming.undo(pairs, store).skipped[0][1] == "it is not at its new name now"
        import threading
        stop = threading.Event()
        stop.set()
        assert naming.apply(changes, store, stop=stop).stopped


class StandInCaptions:
    """Answers the caption job from the file's name: IMG_7 is "a cat number seven"."""

    def __init__(self):
        self.closed = False

    def run(self, job, payload, limits, stop=None):
        from dugup.jobs import path_from_data
        assert job == "dugup.jobs:caption"
        captions, failed = [], {}
        for i, p in enumerate(payload["paths"]):
            path = path_from_data(p)
            if not os.path.exists(path):
                failed[str(i)] = "the file was not there"
                captions.append("")
            else:
                captions.append("a cat number " + os.path.splitext(os.path.basename(path))[0].split("_")[-1])
        return {"captions": captions, "failed": failed, "device": "processor"}

    def close(self):
        self.closed = True


def test_the_rename_command_previews_applies_with_a_record_and_undoes(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image
    from dugup import cli
    from dugup.embedding_models import CAPTIONS_BY_CHOICE
    from dugup.service import LocalService
    data = tmp_path / "data"
    root = tmp_path / "root"
    (root / "trip").mkdir(parents=True)
    for name in ("IMG_7.jpg", "IMG_8.jpg"):
        Image.new("RGB", (20, 20), (200, 10, 10)).save(root / "trip" / name)
    (root / "trip" / "note.txt").write_text("not a picture", encoding="utf-8")
    assert cli.main(["roots", "add", str(root), "--data-dir", str(data)], stdout=io.StringIO()) == 0
    assert cli.main(["index", "--no-ocr", "--no-meaning", "--no-looks", "--data-dir", str(data)], stdout=io.StringIO()) == 0
    fetched = []
    monkeypatch.setattr("dugup.models.fetch", lambda item, folder, progress=None, stop=None: (fetched.append(item.name), None)[1])
    monkeypatch.setattr(LocalService, "captions_ready", lambda self: "the captioning model is not fetched yet (1,042 MB)")
    out = io.StringIO()
    assert cli.main(["rename", str(root / "trip"), "--data-dir", str(data)], stdout=out) == 1
    assert len(fetched) == 5 and "not fetched yet" in out.getvalue(), "the model is fetched on first use; the stand-in fetch leaves nothing"
    models = data / "models" / "captions"
    models.mkdir(parents=True)
    for item in CAPTIONS_BY_CHOICE["base"]:
        (models / item.name).write_bytes(b"stand-in")
    monkeypatch.setattr("dugup.sandbox.SubprocessSandbox", StandInCaptions)
    monkeypatch.setattr(LocalService, "captions_ready", lambda self: True)
    out = io.StringIO()
    assert cli.main(["rename", str(root / "trip"), "--pattern", "{caption} {original}", "--data-dir", str(data)], stdout=out) == 0
    text = out.getvalue()
    assert "Captioned 2 picture(s)" in text and "-> Cat number 7 IMG_7.jpg" in text and "2 picture(s) would be renamed" in text
    assert "Nothing was renamed" in text and (root / "trip" / "IMG_7.jpg").exists()
    out = io.StringIO()
    assert cli.main(["rename", str(root / "trip"), "--apply", "--data-dir", str(data)], stdout=out) == 1 and "needs --record" in out.getvalue()
    record = tmp_path / "renames.txt"
    out = io.StringIO()
    code = cli.main(["rename", str(root / "trip"), "--pattern", "{caption} {original}", "--apply", "--record", str(record), "--yes",
                     "--data-dir", str(data)], stdout=out)
    assert code == 0, out.getvalue()
    assert "Renamed 2 picture(s)" in out.getvalue() and (root / "trip" / "Cat number 7 IMG_7.jpg").exists()
    assert "renamed\t" in record.read_text(encoding="utf-8-sig")
    out = io.StringIO()
    assert cli.main(["search", "cat number 7", "--data-dir", str(data)], stdout=out) == 0 and "Cat number 7 IMG_7.jpg" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["rename", "--undo", str(record), "--data-dir", str(data)], stdout=out) == 0
    assert "Put back 2 name(s)" in out.getvalue() and (root / "trip" / "IMG_7.jpg").exists()
    out = io.StringIO()
    assert cli.main(["rename", str(root / "trip"), "--pattern", "{nope}", "--data-dir", str(data)], stdout=out) == 1
    assert "unknown field" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["rename", str(tmp_path / "elsewhere"), "--data-dir", str(data)], stdout=out) == 1
    assert "not inside an indexed folder" in out.getvalue()
