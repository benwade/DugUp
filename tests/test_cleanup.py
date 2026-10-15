"""The duplicates report: exact copies on one drive, the keeper by the larger set, protected folders aside."""

import io

import pytest

from dugup import cleanup
from dugup.cli import main
from dugup.shapes import FileRecord, FileState
from dugup.store import SqliteStore

from fakes.fake_store import FakeStore


def fill(store):
    def put(path, mark, size=100, state=FileState.DONE):
        row = store.upsert_file(FileRecord(path, size, 1.0, state=state))
        store.set_content(row, mark)
        return row
    # Desktop kept in four places: the full one holds more files
    for i in range(5):
        put(f"G:\\My Drive\\Desktop\\d{i}.txt", f"desk{i}", 1000)
        put(f"G:\\Other computers\\PC (2)\\Desktop\\d{i}.txt", f"desk{i}", 1000)
    for i in range(5, 9):
        put(f"G:\\My Drive\\Desktop\\d{i}.txt", f"desk{i}", 1000)
    put("G:\\Other computers\\PC (4)\\Documents\\live.txt", "live", 50)      # this computer's live backup
    put("G:\\My Drive\\Documents\\live.txt", "live", 50)
    put("G:\\My Drive\\Documents\\also.txt", "also", 70)
    put("G:\\Other computers\\PC (4)\\Documents\\also.txt", "also", 70)
    put("G:\\Other computers\\PC (3)\\Documents\\also.txt", "also", 70)
    put("C:\\Users\\ben\\Documents\\live.txt", "live", 50)                   # another drive: not a duplicate
    put("G:\\My Drive\\empty.txt", "!12", 0)
    put("G:\\My Drive\\alone.txt", "alone", 10)
    gone = put("G:\\My Drive\\gone.txt", "desk0", 1000)
    store.upsert_file(FileRecord("G:\\My Drive\\gone.txt", 1000, 1.0, state=FileState.DONE, missing_since=5.0))
    store.set_content(gone, "desk0")


@pytest.fixture(params=["fake", "sqlite"])
def store(request, tmp_path):
    if request.param == "fake":
        yield FakeStore()
    else:
        with SqliteStore(tmp_path / "index.sqlite") as made:
            yield made


def test_the_keeper_is_in_the_larger_set_and_protected_copies_are_shown_aside(store):
    fill(store)
    found = cleanup.report(store, "G:\\", protected=["G:\\Other computers\\PC (4)"], case_insensitive=True)
    assert found.drive == "G:\\" and len(found.sets) == 6 and found.extra_files == 6
    desks = [s for s in found.sets if s.keep.record.path.endswith("d0.txt")]
    assert len(desks) == 1 and desks[0].keep.record.path == "G:\\My Drive\\Desktop\\d0.txt", "nine files beat five"
    assert [c.record.path for c in desks[0].extra] == ["G:\\Other computers\\PC (2)\\Desktop\\d0.txt"]
    assert desks[0].keep.folder_files == 9 and desks[0].extra[0].folder_files == 5
    also = next(s for s in found.sets if s.keep.record.path.endswith("also.txt"))
    assert also.keep.record.path == "G:\\My Drive\\Documents\\also.txt"
    assert [c.record.path for c in also.extra] == ["G:\\Other computers\\PC (3)\\Documents\\also.txt"]
    assert [c.record.path for c in also.aside] == ["G:\\Other computers\\PC (4)\\Documents\\also.txt"], "shown, never proposed"
    assert not any(s.keep.record.path.endswith("live.txt") for s in found.sets), \
        "its only other copy on this drive is protected, and the one on C: is another drive"
    assert not any("gone" in c.record.path for s in found.sets for c in [s.keep] + s.extra), "a missing file is no copy"
    assert found.wasted_bytes == 5 * 1000 + 70 and found.sets[0].wasted == 1000


def test_the_keeper_rule_falls_back_to_the_copy_higher_up_then_the_first_seen(store):
    def put(path, mark):
        store.set_content(store.upsert_file(FileRecord(path, 10, 1.0, state=FileState.DONE)), mark)
    put("G:\\a\\deep\\down\\x.txt", "x")
    put("G:\\b\\x.txt", "x")
    put("G:\\c\\x.txt", "x")
    found = cleanup.report(store, "G:\\", case_insensitive=True)
    assert found.sets[0].keep.record.path == "G:\\b\\x.txt", "the same set size: higher up, then the one seen first"
    assert [c.record.path for c in found.sets[0].extra] == ["G:\\c\\x.txt", "G:\\a\\deep\\down\\x.txt"]


def test_the_command_lists_saves_and_changes_nothing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with SqliteStore((data / "index.sqlite")) as store:
        pass
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    with LocalService(DataLayout(data).ensure()) as service:
        fill(service.store)
        service.store.set_root(__import__("dugup.shapes", fromlist=["Root"]).Root("G:\\"))
    out = io.StringIO()
    code = main(["duplicates", "G:\\", "--protect", "G:\\Other computers\\PC (4)", "--data-dir", str(data), "--rows", "2"], stdout=out)
    text = out.getvalue()
    assert code == 0 and "nothing is changed, DugUp never deletes" in text and "never proposed: G:\\Other computers\\PC (4)" in text
    assert "NO INDEXED FILE" not in text
    gone = io.StringIO()
    main(["duplicates", "G:\\", "--protect", "G:\\Nowhere", "--prefer", "G:\\My Drive", "--data-dir", str(data)], stdout=gone)
    assert "never proposed: G:\\Nowhere   <- NO INDEXED FILE LIES BELOW THIS" in gone.getvalue()
    assert "the proper home, kept when there is a choice: G:\\My Drive" in gone.getvalue()
    assert "6 extra copy(ies) of 6 file(s)" in text and "keep   G:\\My Drive\\Desktop\\d0.txt   (9 files in its folder)" in text
    assert "extra  G:\\Other computers\\PC (2)\\Desktop\\d0.txt   (5 files in its folder)" in text
    assert "... and 4 more set(s)" in text
    listing = tmp_path / "dups.txt"
    out = io.StringIO()
    code = main(["duplicates", "G:\\", "--protect", "G:\\Other computers\\PC (4)", "--data-dir", str(data), "--save", str(listing)], stdout=out)
    assert code == 0 and "The whole report is in" in out.getvalue()
    lines = listing.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].split("\t") == ["keep", "G:\\My Drive\\Desktop\\d0.txt", "1000", "9"]
    assert any(line.startswith("protected\tG:\\Other computers\\PC (4)\\Documents\\also.txt") for line in lines)
    assert lines.count("") == 6, "a blank line after each set"


def test_a_preferred_folder_wins_the_choice_and_a_protected_folder_with_nothing_below_is_flagged(store):
    def put(path, mark):
        store.set_content(store.upsert_file(FileRecord(path, 10, 1.0, state=FileState.DONE)), mark)
    for name in ("a.txt", "b.txt"):
        put(f"G:\\My Drive\\Desktop\\Alex's Classes\\{name}", name)
        put(f"G:\\My Drive\\Home-ALEX\\School\\Alex's Classes\\{name}", name)
    plain = cleanup.report(store, "G:\\", case_insensitive=True)
    assert plain.sets[0].keep.record.path.startswith("G:\\My Drive\\Desktop"), "the same set size: higher up wins"
    homed = cleanup.report(store, "G:\\", case_insensitive=True, preferred=["G:\\My Drive\\Home-ALEX"],
                           protected=["G:\\Other computers\\My Computer (4)"])
    assert all(s.keep.record.path.startswith("G:\\My Drive\\Home-ALEX") for s in homed.sets), "the proper home wins"
    assert homed.protected_empty == ("G:\\Other computers\\My Computer (4)",), "a protected folder that holds nothing is said"
    assert cleanup.report(store, "G:\\", case_insensitive=True, protected=["G:\\My Drive\\Desktop"]).protected_empty == ()


# ---- removing ----------------------------------------------------------------------------------------------

def real_world(tmp_path):
    """Real files on disk, indexed for real, with a mover that moves to a folder standing in for the trash."""
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    from dugup.shapes import Root
    drive = tmp_path / "drive"
    for folder in ("Docs", "Backup/Docs", "Old/Docs", "Laptop/Docs"):
        (drive / folder).mkdir(parents=True)
    for i in range(4):
        text = f"needle number {i} and more words"
        for folder in ("Docs", "Backup/Docs", "Old/Docs"):
            (drive / folder / f"n{i}.txt").write_text(text, encoding="utf-8")
    (drive / "Docs" / "only.txt").write_text("one of a kind", encoding="utf-8")
    (drive / "Laptop" / "Docs" / "n0.txt").write_text("needle number 0 and more words", encoding="utf-8")
    (drive / "Docs" / "extra.txt").write_text("in docs and backup", encoding="utf-8")
    (drive / "Backup" / "Docs" / "extra.txt").write_text("in docs and backup", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    service = LocalService(DataLayout(data).ensure())
    service.set_root(Root(str(drive)))
    assert service.run_indexing().done == 16
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    moved = []

    def mover(path):
        moved.append(path)
        os.replace(path, bin_ / os.path.basename(path).replace(".txt", f"-{len(moved)}.txt"))
        return ""

    return service, drive, data, mover, moved


import os


def test_extras_are_moved_only_after_the_keeper_is_checked_and_the_index_follows(tmp_path):
    service, drive, data, mover, moved = real_world(tmp_path)
    with service:
        found = service.duplicates(str(drive), protected=[str(drive / "Laptop")], preferred=[str(drive / "Docs")])
        assert found.extra_files == 9 and all(s.keep.record.path.startswith(str(drive / "Docs")) for s in found.sets)
        (drive / "Old" / "Docs" / "n1.txt").write_text("changed since the index was made", encoding="utf-8")
        (drive / "Backup" / "Docs" / "n2.txt").unlink()
        told, journal = [], []
        done = service.remove_duplicates(found, tell=lambda at, total, path: told.append((at, total)), mover=mover,
                                         journal=journal.append)
        assert len(done.moved) == 7 and not done.stopped and told[-1] == (9, 9)
        assert len(journal) == 9 and sum(1 for l in journal if l.startswith("moved\t")) == 7, "each outcome journalled as it happened"
        assert all(l.count("\n") == 0 and l.count("\t") in (2, 3) for l in journal)
        assert sorted(why for _, why in done.skipped) == ["it is not there now", "it no longer holds the same bytes as the copy to keep"]
        assert (drive / "Old" / "Docs" / "n1.txt").exists(), "a changed copy is left where it is"
        assert all((drive / "Docs" / f"n{i}.txt").exists() for i in range(4)) and (drive / "Laptop" / "Docs" / "n0.txt").exists()
        assert not (drive / "Backup" / "Docs" / "n0.txt").exists() and not (drive / "Old" / "Docs" / "n3.txt").exists()
        for path, _ in done.moved:
            assert service.store.get_file(path).missing_since is not None, "the index knows the copy is gone"
        assert service.search("needle").total == 4, "every file is still found, once"
        again = service.duplicates(str(drive), protected=[str(drive / "Laptop")], preferred=[str(drive / "Docs")])
        assert again.extra_files == 2, "the two left alone are still in the index as they were: the next scan sorts them out"
        assert service.run_indexing().read == 1 and service.duplicates(str(drive), protected=[str(drive / "Laptop")]).extra_files == 0


def test_a_stop_ends_between_files_and_keeps_what_was_moved(tmp_path):
    import threading
    service, drive, data, mover, moved = real_world(tmp_path)
    stop = threading.Event()

    def mover_then_stop(path):
        problem = mover(path)
        if len(moved) == 3:
            stop.set()
        return problem

    with service:
        found = service.duplicates(str(drive), protected=[str(drive / "Laptop")], preferred=[str(drive / "Docs")])
        done = service.remove_duplicates(found, stop=stop, mover=mover_then_stop)
        assert done.stopped and len(done.moved) == 3 and len(done.skipped) == 0
        assert service.duplicates(str(drive), protected=[str(drive / "Laptop")], preferred=[str(drive / "Docs")]).extra_files == 6


def test_the_command_shows_asks_records_and_a_no_moves_nothing(tmp_path, monkeypatch):
    from dugup import trash
    service, drive, data, mover, moved = real_world(tmp_path)
    service.close()
    monkeypatch.setattr(trash, "to_trash", lambda path, mover=None: mover(path))
    record = tmp_path / "moved.txt"
    out = io.StringIO()
    code = main(["duplicates", str(drive), "--protect", str(drive / "Laptop"), "--prefer", str(drive / "Docs"),
                 "--remove", "--data-dir", str(data)], stdout=out)
    assert code == 1 and "--remove needs --record" in out.getvalue()
    out = io.StringIO()
    from dugup.cli import build_parser, cmd_duplicates
    args = build_parser().parse_args(["duplicates", str(drive), "--protect", str(drive / "Laptop"), "--prefer", str(drive / "Docs"),
                                      "--remove", "--record", str(record), "--data-dir", str(data)])
    code = cmd_duplicates(args, None, out, {}, install_signals=False, answer=lambda prompt: "no")
    text = out.getvalue()
    assert code == 0 and "Nothing was moved." in text and "move   " in text and "keep   " in text and moved == []
    assert "Type yes to go on" not in text, "the question is asked, not printed"
    record.unlink()                              # claimed by the first run; the record is never written over
    out = io.StringIO()
    args = build_parser().parse_args(["duplicates", str(drive), "--protect", str(drive / "Laptop"), "--prefer", str(drive / "Docs"),
                                      "--remove", "--record", str(record), "--data-dir", str(data), "--yes"])
    monkeypatch.setattr(trash, "to_trash", lambda path, _mover=None: mover(path))
    code = cmd_duplicates(args, None, out, {}, install_signals=False)
    text = out.getvalue()
    assert code == 0 and "Moved 9 file(s) to the trash" in text and "left 0 alone" in text, text
    lines = record.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 9 and all(line.startswith("moved\t") and "\tkept\t" in line for line in lines)
    assert len(moved) == 9 and all((drive / "Docs" / f"n{i}.txt").exists() for i in range(4))


def test_the_trash_itself(tmp_path):
    from dugup import trash
    victim = tmp_path / "gone.txt"
    victim.write_text("x", encoding="utf-8")
    assert trash.to_trash(str(tmp_path / "nowhere.txt")) == "it is not there (or it is a folder)"
    assert trash.to_trash(str(tmp_path)) == "it is not there (or it is a folder)", "never a folder"
    assert trash.to_trash(str(victim), mover=lambda p: "") == "" and victim.exists(), "a mover of one's own"


def test_same_bytes_reads_afresh_and_the_fallback_folder_keeps_every_version(tmp_path, monkeypatch):
    from dugup import cleanup, trash
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"x" * 5000)
    b.write_bytes(b"x" * 5000)
    assert cleanup.same_bytes(str(a), str(b))
    stamp = os.stat(b)
    b.write_bytes(b"x" * 4999 + b"y")           # the same size ...
    os.utime(b, (stamp.st_atime, stamp.st_mtime))  # ... and the same time: filecmp would still say equal
    assert not cleanup.same_bytes(str(a), str(b))
    assert not cleanup.same_bytes(str(a), str(tmp_path / "nowhere.bin"))
    # the fallback folder: a run's own folder, and the same path twice in a run keeps both
    monkeypatch.setattr(trash, "FALLBACK_FOLDER", "removed")
    monkeypatch.setattr(trash, "_top_of", lambda p: (str(tmp_path), os.path.relpath(p, tmp_path)))   # "the drive" is the temp folder
    victim = tmp_path / "v" / "f.txt"
    victim.parent.mkdir()
    victim.write_text("one", encoding="utf-8")
    assert trash._fallback_move(str(victim)) == ""
    victim.write_text("two", encoding="utf-8")
    assert trash._fallback_move(str(victim)) == ""
    kept = sorted(p.read_text(encoding="utf-8") for p in (tmp_path / "removed").rglob("f*.txt"))
    assert kept == ["one", "two"], "nothing overwritten"
    assert trash._run_started in str(next((tmp_path / "removed").rglob("f.txt")))


def test_the_service_moves_files_to_the_trash_but_never_from_a_protected_folder(tmp_path):
    from dugup.datadir import DataLayout
    from dugup.service import LocalService
    from dugup.settings import Settings
    from dugup.shapes import FileRecord, FileState
    root = tmp_path / "root"
    (root / "Laptop").mkdir(parents=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "Laptop" / "b.txt").write_text("b", encoding="utf-8")
    moved, lines = [], []
    with LocalService(DataLayout(tmp_path / "data").ensure(), Settings(cleanup_protect=str(root / "Laptop"))) as svc:
        svc.store.upsert_file(FileRecord(str(root / "a.txt"), 1, 1.0, state=FileState.DONE))
        done = svc.to_trash([str(root / "a.txt"), str(root / "Laptop" / "b.txt"), str(root / "gone.txt")],
                            journal=lines.append, mover=lambda p: (moved.append(p), os.remove(p), "")[2])
    assert [os.path.basename(p) for p, _ in done.moved] == ["a.txt"] and moved == [str(root / "a.txt")]
    assert sorted(why for _, why in done.skipped) == ["it is in a protected folder", "it is not there now"]
    assert (root / "Laptop" / "b.txt").exists()
    assert lines[0].startswith("moving\t") and lines[1].startswith("trashed\t") and sum(1 for l in lines if l.startswith("left\t")) == 2
