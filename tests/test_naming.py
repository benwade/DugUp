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
        assert [os.path.basename(o) for o, _ in done.renamed] == ["a.jpg"], done.skipped
        assert sorted(why for _, why in done.skipped) == ["a file with the new name is there now", "it is not the file that was previewed (the size differs)", "it is not there now"]
        assert (folder / "Picture 0.jpg").exists() and not (folder / "a.jpg").exists()
        assert store.get_file(str(folder / "Picture 0.jpg")) is not None and store.get_file(str(folder / "a.jpg")) is None, "the index followed"
        assert lines[0].startswith("renaming\t") and lines[1].startswith("renamed\t"), "intent first, then the outcome"
        assert sum(1 for l in lines if l.startswith("left\t")) == 3
        pairs = naming.read_record("pattern\t{caption}\n" + "\n".join(lines))
        assert len(pairs) == 1 and pairs[0][:2] == (str(folder / "a.jpg"), str(folder / "Picture 0.jpg"))
        st = os.stat(folder / "Picture 0.jpg")
        assert pairs[0][2:] == (1, st.st_mtime_ns, st.st_ino, 0), "the file's identity (size, exact time, file id; no tolerance) is in the record"
        # a file put in the renamed one's place is not the one that was renamed: undo leaves it alone, even
        # when it has the same size and the same time (a different file id)
        kept = (folder / "Picture 0.jpg").read_bytes()
        other = folder / "other.jpg"
        other.write_bytes(b"y")
        os.utime(other, ns=(st.st_mtime_ns, st.st_mtime_ns))
        os.replace(other, folder / "Picture 0.jpg")
        assert "not the one that was renamed" in naming.undo(pairs, store).skipped[0][1]
        assert (folder / "Picture 0.jpg").read_bytes() == b"y", "and the replacement was not moved anywhere"
        # the original back in place (same bytes; a new file id, so the record's id is recorded afresh)
        (folder / "Picture 0.jpg").write_bytes(kept)
        pairs = [(pairs[0][0], pairs[0][1], 1, None, None, 0)]    # a record from before identities were kept
        back = naming.undo(pairs, store)
        assert back.renamed == [(str(folder / "Picture 0.jpg"), str(folder / "a.jpg"))] and (folder / "a.jpg").exists()
        assert store.get_file(str(folder / "a.jpg")) is not None
        assert naming.undo(pairs, store).skipped[0][1] == "it is not at its new name now"
        # an intent with no outcome (a crash between the lines) is still a pair: undo looks at the disk
        crashed = naming.read_record("renaming\t/p/x.jpg\tto\t/p/Y.jpg\t5\t1000000000\t77")
        assert crashed == [("/p/x.jpg", "/p/Y.jpg", 5, 1000000000, 77, 0)]
        assert naming.read_record("renamed\t/p/x.jpg\tto\t/p/Y.jpg\t5\t1.500") == [("/p/x.jpg", "/p/Y.jpg", 5, 1500000000, None, 1000000)], \
            "an older record, with seconds to three places, reads with a tolerance of a millisecond"
        assert naming.read_record("renaming\t/p/x.jpg\tto\t/p/Y.jpg\t5\t1.0\nleft\t/p/x.jpg\trefused") == []
        # Solace's acceptance test: a different file of the same size put in a previewed file's place, with its
        # time preserved to the nanosecond, is not renamed (the file id differs)
        (folder / "f.jpg").write_bytes(b"F")
        stf = os.stat(folder / "f.jpg")
        f = FileRecord(str(folder / "f.jpg"), stf.st_size, stf.st_mtime, kind=FileKind.IMAGE, state=FileState.DONE)
        store.upsert_file(f)
        plan_f = naming.plan([f], "{caption}", {f.path: "picture f"}, clock)
        assert plan_f[0].identity is not None and plan_f[0].identity[3] == stf.st_ino
        swap = folder / "swap.jpg"
        swap.write_bytes(b"G")
        os.utime(swap, ns=(stf.st_mtime_ns, stf.st_mtime_ns))
        os.replace(swap, folder / "f.jpg")
        held = naming.apply(plan_f, store)
        assert held.renamed == [] and "file id differs" in held.skipped[0][1] and (folder / "f.jpg").read_bytes() == b"G"
        # the protections as they stand at apply time, whatever the preview assumed
        (folder / "e.jpg").write_bytes(b"x")
        st = os.stat(folder / "e.jpg")
        e = FileRecord(str(folder / "e.jpg"), st.st_size, st.st_mtime, kind=FileKind.IMAGE, state=FileState.DONE)
        store.upsert_file(e)
        plan_e = naming.plan([e], "{caption}", {e.path: "picture e"}, clock)
        held = naming.apply(plan_e, store, protected=[str(folder)])
        assert held.renamed == [] and held.skipped[0][1] == "it is in a protected folder now" and (folder / "e.jpg").exists()
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


def test_a_rename_never_replaces_a_file_that_is_there(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"A")
    b.write_bytes(b"B")
    import pytest
    with pytest.raises(OSError):
        naming.rename_no_replace(str(a), str(b))
    assert a.read_bytes() == b"A" and b.read_bytes() == b"B", "neither file was touched"
    c = tmp_path / "c.txt"
    naming.rename_no_replace(str(a), str(c))
    assert c.read_bytes() == b"A" and not a.exists()
    with pytest.raises(OSError):
        naming.rename_no_replace(str(tmp_path / "missing.txt"), str(tmp_path / "d.txt"))
    assert not (tmp_path / "d.txt").exists(), "a failed rename leaves no placeholder behind"

