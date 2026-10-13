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
        put(f"G:\\My Drive\\Rivera-ALEX\\School\\Alex's Classes\\{name}", name)
    plain = cleanup.report(store, "G:\\", case_insensitive=True)
    assert plain.sets[0].keep.record.path.startswith("G:\\My Drive\\Desktop"), "the same set size: higher up wins"
    homed = cleanup.report(store, "G:\\", case_insensitive=True, preferred=["G:\\My Drive\\Rivera-ALEX"],
                           protected=["G:\\Other computers\\My Computer (4)"])
    assert all(s.keep.record.path.startswith("G:\\My Drive\\Rivera-ALEX") for s in homed.sets), "the proper home wins"
    assert homed.protected_empty == ("G:\\Other computers\\My Computer (4)",), "a protected folder that holds nothing is said"
    assert cleanup.report(store, "G:\\", case_insensitive=True, protected=["G:\\My Drive\\Desktop"]).protected_empty == ()
