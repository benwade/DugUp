"""Choosing what to index, folder by folder: the logic (picker.py) and the service calls behind the window."""

import pytest

import helpers
from dugup.datadir import DataLayout
from dugup.filesource import ScanRules, excluded, matches_rules
from dugup.picker import Guess, Node, Plan, Tree, build, guess, literal, plan_from_guess, plan_from_root
from dugup.service import LocalService
from dugup.settings import Settings
from dugup.shapes import (LEVEL_MEANING, LEVEL_NAMES, LEVEL_OCR, LEVEL_WORDS, POLICY_NAMES_ONLY, Failure, Root, level_for)
from dugup.survey import Rates

FILES = (
    [("/r/Letters/roof.pdf", 4000, False), ("/r/Letters/2019/tax.docx", 3000, False), ("/r/Letters/scan.jpg", 900, False),
     ("/r/Books/a.epub", 10, False), ("/r/Books/b.pdf", 10, False),
     ("/r/Proj/.venv/pyvenv.cfg", 5, False), ("/r/Proj/.venv/lib/x.py", 5, False), ("/r/Proj/readme.md", 5, False),
     ("/r/Trash/old.txt", 1, False), ("/r/Videos/film.mp4", 99_999, False),
     ("/r/Cloud/a.pdf", 5, True), ("/r/Cloud/b.pdf", 5, True), ("/r/top.txt", 7, False)]
    + [(f"/r/Pics/2020/p{n}.jpg", 100, False) for n in range(30)]
    + [(f"/r/Mixed/doc{n}.pdf", 10, False) for n in range(5)] + [(f"/r/Mixed/scan{n}.png", 10, False) for n in range(15)])


@pytest.fixture
def tree():
    return build("/r", FILES)


def test_the_tree_counts_what_each_folder_holds_and_sets_tool_data_apart(tree):
    top = tree.top
    assert [c.name for c in top.sorted_children()] == ["Videos", "Letters", "Pics", "Mixed", "Books", "Cloud", "Proj", "Trash"], \
        "largest first, not counting tool data"
    assert top.children["proj"].total.groups["code"] == 0, "tool files are kept out of what a person sees"
    assert top.total.files == len(FILES) and top.own.files == 1, "the root counts everything; one file lies directly in it"
    letters = top.children["letters"]
    assert letters.total.files == 3 and letters.own.files == 2 and letters.children["2019"].total.files == 1
    proj = top.children["proj"]
    assert proj.children[".venv"].tool and not proj.tool
    assert proj.total.not_worth == 2 and proj.total.readable == 1
    assert tree.tool_files == 2 and tree.tool_patterns == ("Proj/.venv",)
    assert [c.name for c in proj.sorted_children()] == [] and [c.name for c in proj.sorted_children(with_tools=True)] == [".venv"]


def test_the_starting_guesses_and_their_reasons(tree):
    plan = plan_from_guess(tree, Settings())
    seen = {c.name: (plan.ticked(c.rel), plan.level(c.rel)) for c in tree.top.sorted_children()}
    assert seen == {
        "Letters": (True, LEVEL_MEANING), "Mixed": (True, LEVEL_MEANING), "Proj": (True, LEVEL_MEANING),
        "Pics": (True, LEVEL_WORDS), "Books": (True, LEVEL_NAMES), "Videos": (True, LEVEL_NAMES), "Cloud": (True, LEVEL_NAMES),
        "Trash": (False, LEVEL_MEANING)}
    reason = lambda name: plan.reasons[name.casefold()]
    assert "book library" in reason("Books") and "a guess" in reason("Books")
    assert "mostly pictures" in reason("Pics") and "Words and scans" in reason("Pics")
    assert "scans are read too" in reason("Mixed"), "a folder with documents and pictures together gets OCR"
    assert "cloud" in reason("Cloud") and "trash" in reason("Trash") and "nothing here that can be read" in reason("Videos")


def test_the_guesses_follow_the_settings():
    node = build("/r", [(f"/r/Shelf/b{n}.pdf", 1, False) for n in range(3)]).top.children["shelf"]
    assert guess(node, ["shelf"], LEVEL_WORDS).level == LEVEL_WORDS
    assert guess(node, ["shelf"], "").level == LEVEL_MEANING, "an empty level makes no guess about books"
    assert guess(node, ["SHELF"]).level == LEVEL_NAMES, "names are compared without regard to case"
    pics = build("/r", [(f"/r/P/p{n}.jpg", 1, False) for n in range(10)] + [("/r/P/a.pdf", 1, False)]).top.children["p"]
    assert guess(pics, picture_min=20).level == LEVEL_MEANING, "too few pictures to call it a picture folder"
    assert guess(pics, picture_min=5).level == LEVEL_WORDS and guess(pics, picture_min=5, picture_fraction=0.95).level == LEVEL_MEANING
    tools = build("/r", [("/r/T/.git/a", 1, False), ("/r/T/.git/b", 1, False)]).top.children["t"]
    assert guess(tools).ticked is False


def test_a_folder_follows_the_one_above_until_it_is_given_its_own_choice(tree):
    plan = Plan(tree)
    assert plan.ticked("Letters/2019") and plan.level("Letters/2019") == LEVEL_MEANING and plan.own_level("Letters/2019") is None
    plan.set_level("Letters", LEVEL_WORDS)
    assert plan.level("Letters/2019") == LEVEL_WORDS
    plan.set_level("Letters/2019", LEVEL_OCR)
    assert plan.level("Letters/2019") == LEVEL_OCR and plan.level("Letters") == LEVEL_WORDS
    plan.set_level("Letters/2019", LEVEL_WORDS)
    assert plan.own_level("Letters/2019") is None, "the same as the folder above: nothing to remember"
    plan.set_ticked("Letters", False)
    assert not plan.ticked("Letters/2019") and plan.own_tick("Letters/2019"), "out because the folder above is out"
    plan.set_ticked("", False)
    assert plan.ticked(""), "the root itself cannot be taken out here"
    steps = [plan.next_level("Pics") for _ in range(4)]
    assert steps == [LEVEL_NAMES, LEVEL_WORDS, LEVEL_OCR, LEVEL_MEANING], "one click steps to the next level, round and round"


def test_the_estimate_follows_the_choices(tree):
    rates = Rates()
    plan = Plan(tree)
    everything = plan.cost(rates)
    assert everything > 0 and plan.cost(rates, "Proj/.venv") == 0, "tool data is left out and costs nothing"
    plan.set_ticked("Letters", False)
    without = plan.cost(rates)
    assert without == everything - tree.top.children["letters"].total.cost(rates)[3]
    plan.set_level("Pics", LEVEL_NAMES)
    assert plan.cost(rates, "Pics") == 30 * rates.name < without
    plan.skip_tool = False
    assert plan.cost(rates, "Proj") > tree.top.children["proj"].own.cost(rates)[3], "tool files are counted again when they are let in"


def test_the_choices_become_skip_rules_and_levels_that_do_what_was_chosen(tree):
    plan = plan_from_guess(tree, Settings())
    plan.set_ticked("Letters/2019", False)
    plan.set_level("Mixed", LEVEL_OCR)
    root = plan.to_root()
    assert root.path == "/r" and set(root.exclude) == {"Proj/.venv", "/Trash", "/Letters/2019"}
    assert dict(root.levels) == {"Books": LEVEL_NAMES, "Pics": LEVEL_WORDS, "Videos": LEVEL_NAMES, "Cloud": LEVEL_NAMES, "Mixed": LEVEL_OCR}
    rules = ScanRules((), root.exclude)
    wanted = {path: matches_rules(path[3:], rules) for path, _, _ in FILES}
    assert not wanted["/r/Trash/old.txt"] and not wanted["/r/Letters/2019/tax.docx"] and not wanted["/r/Proj/.venv/lib/x.py"]
    assert wanted["/r/Letters/roof.pdf"] and wanted["/r/Proj/readme.md"] and wanted["/r/top.txt"]
    assert level_for(root, "/r/Pics/2020/p1.jpg") == LEVEL_WORDS and level_for(root, "/r/Letters/roof.pdf") == LEVEL_MEANING


def test_a_folder_out_needs_one_rule_and_levels_below_it_are_dropped(tree):
    plan = Plan(tree)
    plan.set_level("Letters/2019", LEVEL_NAMES)
    plan.set_ticked("Letters/2019", False)
    plan.set_ticked("Letters", False)
    root = plan.to_root()
    assert [rule for rule in root.exclude if "Letters" in rule] == ["/Letters"] and root.levels == ()
    plan.set_level("", LEVEL_WORDS)
    assert plan.to_root().levels == (("", LEVEL_WORDS),), "a level for the root itself"


def test_folder_names_with_pattern_characters_mean_exactly_that_folder():
    odd = "Tax [2019]*"
    files = [(f"/r/{odd}/a.pdf", 1, False), ("/r/Tax 2/b.pdf", 1, False), ("/r/Tax x2019y/c.pdf", 1, False),
             (f"/r/{helpers.CAFE} {helpers.CJK}/d.pdf", 1, False)]
    plan = Plan(build("/r", files))
    plan.set_ticked(odd, False)
    plan.set_ticked(f"{helpers.CAFE} {helpers.CJK}", False)
    rules = ScanRules((), plan.to_root().exclude)
    assert literal(odd) == "/Tax [[]2019][*]"
    assert excluded(f"{odd}/a.pdf", rules) and excluded(f"{helpers.CAFE} {helpers.CJK}/d.pdf", rules)
    assert not excluded("Tax 2/b.pdf", rules) and not excluded("Tax x2019y/c.pdf", rules), "no other folder is caught"


def test_an_indexed_folder_opens_with_its_current_choices_and_round_trips(tree):
    first = plan_from_guess(tree, Settings())
    first.set_ticked("Letters/2019", False)
    root = first.to_root()
    again = plan_from_root(tree, root)
    assert again.to_root() == root, "opening and applying unchanged changes nothing"
    assert not again.ticked("Trash") and not again.ticked("Letters/2019") and again.level("Pics") == LEVEL_WORDS and again.skip_tool
    older = Root("/r", exclude=("*.tmp", "Trash", "/Videos"), levels=(("", LEVEL_WORDS),))
    plan = plan_from_root(tree, older)
    assert plan.kept_rules == ("*.tmp", "Trash") and not plan.ticked("Videos") and plan.base_level == LEVEL_WORDS
    assert not plan.skip_tool, "tool data was not being skipped, and the picker shows that"
    assert plan.still_skipped() == ["Trash"], "ticked here, but the older rule 'Trash' still leaves it out: said, not hidden"
    plan.set_ticked("Videos", True)
    assert "/Videos" not in plan.to_root().exclude and "*.tmp" in plan.to_root().exclude, "rules the picker did not make are kept"
    assert plan_from_root(tree, Root("/r", POLICY_NAMES_ONLY)).base_level == LEVEL_NAMES


def test_an_empty_folder_and_windows_paths():
    empty = plan_from_guess(build("/nothing", []), Settings())
    assert empty.to_root() == Root("/nothing") and empty.cost(Rates()) == 0 and empty.counts() == (0, 0)
    tree = build("D:\\Mes Docs\\", [("D:\\Mes Docs\\Sub Dir\\a.pdf", 1, False), ("D:\\Mes Docs\\SUB DIR\\b.pdf", 1, False)])
    assert tree.root == "D:\\Mes Docs" and len(tree.top.children) == 1 and tree.top.children["sub dir"].total.files == 2
    plan = Plan(tree)
    plan.set_ticked("Sub Dir", False)
    assert plan.to_root().exclude == ("/Sub Dir",)


# ---- through the service --------------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    docs = tmp_path / helpers.AWKWARD_FOLDER
    for folder in ("Letters", "Books", "Trash", "Proj/.venv"):
        (docs / folder).mkdir(parents=True)
    (docs / "Letters" / "roof.txt").write_text("the roof letter whale", encoding="utf-8")
    (docs / "Books" / "novel.txt").write_text("a novel about a whale", encoding="utf-8")
    (docs / "Trash" / "old.txt").write_text("an old whale", encoding="utf-8")
    (docs / "Proj" / ".venv" / "pyvenv.cfg").write_text("home = x", encoding="utf-8")
    (docs / "Proj" / ".venv" / "note.txt").write_text("a packaged whale", encoding="utf-8")
    (docs / "Proj" / "readme.txt").write_text("the project whale", encoding="utf-8")
    with LocalService(DataLayout(tmp_path / "data").ensure(), Settings()) as service:
        service.docs = docs
        service.names = lambda word: sorted(r.name for r in service.search(word).rows)
        yield service


def test_a_new_folder_is_planned_applied_and_indexed_as_chosen(world):
    material = world.plan_folder(str(world.docs))
    plan = material["plan"]
    assert material["known"] is False and not plan.ticked("Trash") and plan.level("Books") == LEVEL_NAMES
    assert world.list_roots() == [], "planning adds nothing"
    root = world.apply_plan(plan)
    assert not isinstance(root, Failure) and "/Trash" in root.exclude and dict(root.levels) == {"Books": LEVEL_NAMES}
    world.run_indexing()
    assert world.names("whale") == ["readme.txt", "roof.txt"], "not the trash, not the tool data, not the book's text"
    assert world.names("novel") == ["novel.txt"], "the book is found by its name"


def test_changing_the_choices_of_an_indexed_folder_takes_effect_at_once(world):
    world.apply_plan(world.plan_folder(str(world.docs))["plan"])
    world.run_indexing()
    material = world.plan_folder(str(world.docs))
    plan = material["plan"]
    assert material["known"] is True and plan.to_root().exclude == world.list_roots()[0].exclude
    plan.set_ticked("Letters", False)
    plan.set_level("Books", None)
    plan.set_ticked("Trash", True)
    world.apply_plan(plan)
    assert world.names("whale") == ["readme.txt"], "Letters left the index at once"
    assert world.list_roots()[0].levels == (), "the level that was taken away is gone, not kept from before"
    world.run_indexing()
    assert world.names("whale") == ["novel.txt", "old.txt", "readme.txt"]
    fresh = material["fresh"]()
    assert not fresh.ticked("Trash") and fresh.level("Books") == LEVEL_NAMES, "the starting guesses can be had again"


def test_folders_that_cannot_be_planned_are_stated_failures(world, tmp_path):
    assert world.plan_folder(str(tmp_path / "missing")).reason.value == "unreachable"
    world.apply_plan(world.plan_folder(str(world.docs))["plan"])
    inside = world.plan_folder(str(world.docs / "Letters"))
    assert isinstance(inside, Failure) and "overlaps" in inside.detail and "inside that one" in inside.detail
