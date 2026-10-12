"""Looking at a folder before indexing it (survey.py, `dugup roots survey`) and the early size estimate of a run."""

import io

import pytest

import helpers
from dugup import cli, indexer as indexing
from dugup.cli import ProgressPrinter, progress_text
from dugup.indexer import Indexer, Progress
from dugup.shapes import Failure, Root
from dugup.survey import (CODE, DOCUMENTS, HERE, LEVELS, MEDIA, OTHER, PICTURES, TEXT, Rates, group_of, learn_rates, survey)
from fakes.fake_store import FakeStore
from test_indexer import World

FILES = [
    ("/r/Letters/roof.pdf", 4000, False), ("/r/Letters/2019/tax.docx", 3000, False), ("/r/Letters/notes.md", 10, False),
    ("/r/Letters/scan.JPG", 900, False),
    ("/r/Code/app/main.py", 50, False), ("/r/Code/app/.venv/pyvenv.cfg", 5, False),
    ("/r/Code/app/.venv/lib/site.py", 70, False), ("/r/Code/app/.venv/lib/readme.txt", 70, False),
    ("/r/Cloud/film.mp4", 9_000_000, True), ("/r/Cloud/contract.pdf", 500, True), ("/r/Cloud/local.pdf", 600, False),
    ("/r/top.txt", 7, False), ("/r/thing.xyz", 1, False),
]


def rows(found):
    return {row.folder: row for row in found.rows}


def test_each_kind_of_file_is_put_in_a_group_a_person_recognises():
    assert [group_of(p) for p in ("a.pdf", "a.DOCX", "a.md", "a.heic", "a.rs", "a.flac", "a.bin", "noext")] == \
        [DOCUMENTS, DOCUMENTS, TEXT, PICTURES, CODE, MEDIA, OTHER, OTHER]


def test_one_row_for_each_folder_below_largest_first_with_a_total():
    found = survey("/r", FILES)
    assert [row.folder for row in found.rows] == ["Cloud", "Letters", "Code", HERE]
    by = rows(found)
    letters = by["Letters"]
    assert (letters.files, letters.bytes) == (4, 7910), "files in folders further down count for the folder at the top"
    assert (letters.groups[DOCUMENTS], letters.groups[TEXT], letters.groups[PICTURES]) == (2, 1, 1)
    assert (letters.readable, letters.to_ocr, letters.cloud_only, letters.not_worth) == (3, 1, 0, 0)
    assert by[HERE].files == 2 and by[HERE].groups[OTHER] == 1
    assert found.total.files == len(FILES) and found.total.bytes == sum(size for _, size, _ in FILES)
    assert found.total.readable == sum(row.readable for row in found.rows)


def test_cloud_only_files_are_counted_and_never_planned_for_reading():
    cloud = rows(survey("/r", FILES))["Cloud"]
    assert cloud.cloud_only == 2 and cloud.readable == 1, "only the PDF that is on this machine would be read"
    names, words, ocr, meaning = cloud.cost(Rates())
    assert names == 3 * Rates().name, "a cloud-only file still costs its name"
    assert words == names + 1 * Rates().read


def test_downloaded_requirements_are_set_apart_and_cost_nothing():
    code = rows(survey("/r", FILES))["Code"]
    assert code.files == 4 and code.not_worth == 3, "everything inside the environment, readable or not"
    assert code.readable == 0 and code.cost(Rates())[0] == 1 * Rates().name


def test_each_level_costs_at_least_as_much_as_the_one_before():
    found = survey("/r", FILES)
    assert len(LEVELS) == 4
    for row in found.rows + (found.total,):
        cost = row.cost(found.rates)
        assert list(cost) == sorted(cost) and cost[0] >= 0
    letters = rows(found)["Letters"]
    r = found.rates
    assert letters.cost(r) == (4 * r.name, 4 * r.name + 3 * r.read, 4 * r.name + 3 * r.read + r.picture,
                               4 * r.name + 3 * r.read + r.picture + 4 * r.vector)


def test_windows_paths_odd_names_and_an_empty_folder():
    files = [("D:\\Mes Docs\\" + helpers.CAFE + "\\a.pdf", 5, False), ("D:\\Mes Docs\\" + helpers.CAFE.upper() + "\\b.pdf", 5, False),
             ("D:\\Mes Docs\\" + helpers.CJK + "\\c.txt", 5, False)]
    found = survey("D:\\Mes Docs\\", files)
    assert len(found.rows) == 2, "one row for a folder whatever the case of its name"
    assert sum(row.files for row in found.rows) == 3
    empty = survey("/nothing", [])
    assert empty.rows == () and empty.total.files == 0 and empty.total.cost(Rates()) == (0, 0, 0, 0)


def test_rates_are_learned_from_a_real_index_and_guessed_from_a_small_one():
    learned = learn_rates(71_000_000, 7131, 2791, 34174)
    assert learned.learned and 20_000 < learned.read < 30_000 and 3_000 < learned.vector < 6_000
    for small in (learn_rates(0, 0, 0, 0), learn_rates(5_000_000, 100, 50, 300), learn_rates(-1, 9000, 9000, 9000)):
        assert small == Rates() and not small.learned, "nothing to learn from: the starting guesses, and no division by zero"
    assert learn_rates(1_000, 5000, 5000, 0).read >= 1_000, "never a negative or silly rate"


# ---- the command ---------------------------------------------------------------------------------------------

def test_survey_command_shows_both_tables_and_changes_nothing(tmp_path):
    data = tmp_path / "data"
    docs = tmp_path / helpers.AWKWARD_FOLDER
    (docs / "Letters").mkdir(parents=True)
    (docs / "Letters" / f"{helpers.CAFE}.pdf").write_bytes(b"%PDF-1.4 " + bytes(3000))
    (docs / "Proj" / ".venv" / "lib").mkdir(parents=True)
    (docs / "Proj" / ".venv" / "pyvenv.cfg").write_text("home = x", encoding="utf-8")
    (docs / "Proj" / ".venv" / "lib" / "big.txt").write_text("x" * 9000, encoding="utf-8")
    (docs / "loose.txt").write_text("words", encoding="utf-8")

    def run(*argv):
        out = io.StringIO()
        return cli.main([*argv, "--data-dir", str(data)], stdout=out, environ={}), out.getvalue()

    before = sorted(p for p in docs.rglob("*"))
    code, text = run("roots", "survey", str(docs))
    assert code == 0, text
    assert "no file is opened" in text and "What is in" in text and "What the index would grow by" in text
    for word in ("Letters", "Proj", "TOTAL", "names", "words and OCR", "meaning as well", "starting guesses", "rough estimates"):
        assert word in text, word
    assert "dugup roots survey" in text and "Proj" in text.split("To look closer")[1]
    assert sorted(p for p in docs.rglob("*")) == before
    assert "no folders" in run("roots", "list")[1].lower() or str(docs) not in run("roots", "list")[1], "surveying adds no folder"
    code, text = run("roots", "survey", str(docs), "--rows", "1")
    assert "smaller folder(s), included in the total" in text
    code, text = run("roots", "survey", str(tmp_path / "missing"))
    assert code == 1 and "Could not do that" in text


# ---- the early estimate of a run -----------------------------------------------------------------------------

def test_a_large_run_says_early_where_the_index_is_heading(monkeypatch):
    monkeypatch.setattr(indexing, "ESTIMATE_FIRST", 10)
    monkeypatch.setattr(indexing, "ESTIMATE_STEP", 3)
    world = World(FakeStore())
    for n in range(100):
        world.put(f"f{n:03}.txt", f"words {n}")
    size = {"now": 1_000_000}
    read = []

    def size_now():
        return size["now"] + 2_000 * len(world.reader.calls)      # the index grows 2,000 bytes a file

    indexer = Indexer(world.store, world.h.source, world.reader, world.settings, world.seen.append, world.clock,
                      size_now=size_now)
    indexer.run()
    estimates = [p for p in world.seen if p.phase == "estimate"]
    assert [p.done for p in estimates] == [10, 30, 90], "early, then at 3 times as many, and so on; not on every file"
    for p in estimates:
        assert p.total == 100 and abs(p.projected_bytes - 1_200_000) < 5_000, "start + 100 files at 2,000 bytes each"


def test_a_small_run_or_an_unknown_size_gives_no_estimate(monkeypatch):
    monkeypatch.setattr(indexing, "ESTIMATE_FIRST", 10)
    world = World(FakeStore())
    for n in range(15):                                # fewer than twice the first mark: it would be over before it helped
        world.put(f"f{n}.txt")
    Indexer(world.store, world.h.source, world.reader, world.settings, world.seen.append, world.clock, size_now=lambda: 5).run()
    assert not [p for p in world.seen if p.phase == "estimate"]

    for broken in (None, lambda: 1_000, lambda: (_ for _ in ()).throw(OSError("gone"))):   # no size, no growth, an error
        other = World(FakeStore())
        for n in range(40):
            other.put(f"f{n}.txt")
        report = Indexer(other.store, other.h.source, other.reader, other.settings, other.seen.append, other.clock,
                         size_now=broken).run()
        assert not isinstance(report, Failure) and not [p for p in other.seen if p.phase == "estimate"]
        assert other.found("words") and len(other.found("words")) == 40, "the run itself is untouched"


def test_the_estimate_is_one_plain_line_that_stays_on_the_screen():
    line = progress_text(Progress("estimate", "D:\\x", 2000, 480_000, 3, 100.0, projected_bytes=8_400 * 1048576))
    assert line.startswith("[estimate] after 2,000 of 480,000 files") and "8,400 MB" in line and "Ctrl+C stops" in line
    assert "more" in line, "with a rate, the time left is said too"
    assert "more." not in progress_text(Progress("estimate", "r", 0, 10, projected_bytes=5.0)), "no rate: no time claimed"

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    screen = Terminal()
    printer = ProgressPrinter(screen, timer=lambda: 0.0)
    printer(Progress("read", "r", 1, 10, 0, 1.0))
    printer(Progress("estimate", "r", 2, 10, 0, 1.0, projected_bytes=1048576))
    printer(Progress("read", "r", 3, 10, 0, 1.0))
    text = screen.getvalue()
    assert "[estimate]" in text and text.split("[estimate]")[1].split("\n")[0], "printed whole"
    assert "\n[estimate]" in text and "\r[estimate]" not in text, "on a line of its own, not overwritten by the next progress"
