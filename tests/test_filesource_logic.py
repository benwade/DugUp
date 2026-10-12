"""The pure FileSource helpers: include/exclude rules and the comparison of two listings."""

import pytest

from dugup.filesource import SUGGESTED_EXCLUDES, compute_changes, excluded, matches_rules
from dugup.protocols import (WARN_MASS_LOSS, WARN_ROOT_EMPTY, ChangeSet, FileEntry, ScanRules, Skipped)


def e(path, size=1, modified=1.0, fid=None):
    return FileEntry(path, size, modified, fid)


def listing(*entries):
    return {x.path: x for x in entries}


# ---- rules ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("relative,expected", [
    ("a/b.txt", True), ("a/node_modules/x.js", False), ("node_modules/x.js", False),
    ("a/b.TMP", False), ("a/b.tmp.txt", True), ("a\\node_modules\\x.js", False),
])
def test_exclude_patterns_match_names_folders_and_whole_paths(relative, expected):
    rules = ScanRules(exclude=("node_modules", "*.tmp"))
    assert matches_rules(relative, rules) is expected


def test_include_patterns_limit_what_is_wanted_and_ignore_case():
    rules = ScanRules(include=("*.pdf", "docs"))
    assert matches_rules("x/y.PDF", rules)
    assert matches_rules("docs/readme.txt", rules)      # inside an included folder
    assert not matches_rules("x/y.txt", rules)


def test_exclude_wins_over_include():
    rules = ScanRules(include=("*.pdf",), exclude=("private",))
    assert not matches_rules("private/a.pdf", rules)


def test_no_rules_means_everything_and_an_empty_path_is_never_a_file():
    assert matches_rules("a/b", ScanRules())
    assert not matches_rules("", ScanRules())
    assert not matches_rules("///", ScanRules())


def test_an_empty_pattern_matches_nothing():
    assert matches_rules("a.txt", ScanRules(exclude=("",)))


# ---- changes -------------------------------------------------------------------------------------------

def test_identical_listings_give_an_empty_changeset():
    prev = listing(e("/r/a"), e("/r/b"))
    assert compute_changes(prev, [e("/r/a"), e("/r/b")]) == ChangeSet()


def test_added_changed_removed_are_sorted_by_path():
    prev = listing(e("/r/keep"), e("/r/edit", size=1), e("/r/gone2"), e("/r/gone1"))
    now = [e("/r/keep"), e("/r/edit", size=2), e("/r/new2"), e("/r/new1")]
    result = compute_changes(prev, now)
    assert [x.path for x in result.added] == ["/r/new1", "/r/new2"]
    assert [x.path for x in result.changed] == ["/r/edit"]
    assert result.removed == ("/r/gone1", "/r/gone2")


def test_a_new_modified_time_alone_is_a_change():
    assert [x.path for x in compute_changes(listing(e("/r/a", modified=1.0)), [e("/r/a", modified=2.0)]).changed] == ["/r/a"]


def test_same_file_id_at_a_new_path_is_a_move_not_add_plus_remove():
    result = compute_changes(listing(e("/r/old", fid="9")), [e("/r/new", fid="9")])
    assert result.moved == (("/r/old", e("/r/new", fid="9")),)
    assert result.added == () and result.removed == () and result.changed == ()


def test_a_moved_file_with_new_content_is_also_changed():
    result = compute_changes(listing(e("/r/old", size=1, fid="9")), [e("/r/new", size=5, fid="9")])
    assert len(result.moved) == 1 and [x.path for x in result.changed] == ["/r/new"]


def test_files_without_ids_never_count_as_moved():
    result = compute_changes(listing(e("/r/old")), [e("/r/new")])
    assert result.moved == () and [x.path for x in result.added] == ["/r/new"] and result.removed == ("/r/old",)


def test_an_ambiguous_id_does_not_guess():
    prev = listing(e("/r/a", fid="1"), e("/r/b", fid="1"))
    result = compute_changes(prev, [e("/r/c", fid="1")])
    assert result.moved == () and len(result.removed) == 2 and len(result.added) == 1


def test_a_swap_of_two_existing_paths_is_not_reported():
    prev = listing(e("/r/a", fid="1"), e("/r/b", fid="2"))
    result = compute_changes(prev, [e("/r/a", fid="2"), e("/r/b", fid="1")])
    # both paths still exist with equal size and time, so nothing differs (file ids are only used for vanished paths)
    assert result == ChangeSet()


def test_emptied_folder_warns_root_empty_and_still_lists_removals():
    prev = listing(*[e(f"/r/f{i}") for i in range(3)])
    result = compute_changes(prev, [])
    assert result.warnings == (WARN_ROOT_EMPTY,) and len(result.removed) == 3


def test_nothing_before_and_nothing_now_is_no_warning():
    assert compute_changes({}, []) == ChangeSet()


@pytest.mark.parametrize("lost,warns", [(0, False), (9, False), (10, True), (25, True)])
def test_mass_loss_needs_a_sizeable_listing_and_at_least_half_gone(lost, warns):
    prev = listing(*[e(f"/r/f{i:02d}") for i in range(20)])
    now = [e(f"/r/f{i:02d}") for i in range(lost, 20)] + [e("/r/extra")] if lost else [e(f"/r/f{i:02d}") for i in range(20)]
    result = compute_changes(prev, now)
    assert (WARN_MASS_LOSS in result.warnings) is warns


def test_a_small_listing_never_raises_mass_loss():
    prev = listing(*[e(f"/r/f{i}") for i in range(10)])
    assert WARN_MASS_LOSS not in compute_changes(prev, [e("/r/f9")]).warnings


def test_moved_files_do_not_count_as_loss():
    prev = listing(*[e(f"/r/f{i:02d}", fid=str(i)) for i in range(20)])
    now = [e(f"/r/moved/f{i:02d}", fid=str(i)) for i in range(20)]
    result = compute_changes(prev, now)
    assert len(result.moved) == 20 and result.warnings == ()


# ---- excluded folders ----------------------------------------------------------------------------------

@pytest.mark.parametrize("relative,expected", [
    ("node_modules", True), ("a/node_modules", True), ("a\\node_modules", True),
    ("a/node_modules/deep/x.js", True), ("a/node_modules_not", False), ("a/b", False), ("", False),
])
def test_excluded_tells_a_scanner_which_folders_not_to_enter(relative, expected):
    assert excluded(relative, ScanRules(exclude=("node_modules",))) is expected


def test_include_patterns_never_exclude_a_folder_so_a_scanner_still_descends():
    rules = ScanRules(include=("*.pdf",))
    assert not excluded("some/folder", rules)
    assert not matches_rules("some/folder/x.txt", rules)


def test_the_suggested_excludes_are_plain_names_that_work_as_patterns():
    rules = ScanRules(exclude=SUGGESTED_EXCLUDES)
    assert len(set(SUGGESTED_EXCLUDES)) == len(SUGGESTED_EXCLUDES)
    for name in SUGGESTED_EXCLUDES:
        assert excluded(f"work/{name}", rules), name
        assert not matches_rules(f"work/{name}/inside.txt", rules), name
    assert matches_rules("work/notes.txt", rules)


# ---- skipped folders -----------------------------------------------------------------------------------

def test_files_below_a_skipped_folder_are_kept_not_removed():
    prev = listing(e("/r/a/one"), e("/r/a/deep/two"), e("/r/b/three"))
    result = compute_changes(prev, [], skipped=[Skipped("/r/a", "permission denied")])
    assert result.removed == ("/r/b/three",)
    assert result.skipped == (Skipped("/r/a", "permission denied"),)


def test_a_skipped_folder_name_is_matched_whole_not_as_a_prefix():
    prev = listing(e("/r/a/one"), e("/r/ab/two"))
    result = compute_changes(prev, [], skipped=[Skipped("/r/a", "x")])
    assert result.removed == ("/r/ab/two",)


def test_backslash_paths_below_a_skipped_folder_are_kept_too():
    prev = listing(e("C:\\r\\a\\one"), e("C:\\r\\b\\two"))
    result = compute_changes(prev, [], skipped=[Skipped("C:\\r\\a", "x")])
    assert result.removed == ("C:\\r\\b\\two",)


def test_a_file_still_listed_below_a_skipped_folder_is_compared_as_usual():
    prev = listing(e("/r/a/one", size=1), e("/r/a/two"))
    result = compute_changes(prev, [e("/r/a/one", size=9)], skipped=[Skipped("/r/a", "broke mid-listing")])
    assert [x.path for x in result.changed] == ["/r/a/one"]
    assert result.removed == ()


def test_a_file_that_left_a_skipped_folder_is_not_matched_as_moved_from_it():
    prev = listing(e("/r/a/one", fid="7"))
    result = compute_changes(prev, [e("/r/b/one", fid="7")], skipped=[Skipped("/r/a", "x")])
    assert result.moved == () and [x.path for x in result.added] == ["/r/b/one"] and result.removed == ()


def test_files_below_a_skipped_folder_do_not_count_toward_mass_loss():
    prev = listing(*[e(f"/r/a/f{i:02d}") for i in range(30)], *[e(f"/r/b/f{i:02d}") for i in range(5)])
    now = [e(f"/r/b/f{i:02d}") for i in range(5)]
    assert WARN_MASS_LOSS in compute_changes(prev, now).warnings
    assert compute_changes(prev, now, skipped=[Skipped("/r/a", "x")]).warnings == ()


def test_an_unreadable_folder_alone_is_not_an_emptied_root():
    prev = listing(e("/r/a/one"), e("/r/a/two"))
    result = compute_changes(prev, [], skipped=[Skipped("/r/a", "x")])
    assert result.warnings == () and result.removed == ()


def test_skipped_folders_are_reported_sorted_by_path():
    skipped = [Skipped("/r/z", "x"), Skipped("/r/a", "y")]
    assert [s.path for s in compute_changes({}, [], skipped=skipped).skipped] == ["/r/a", "/r/z"]


def test_no_skipped_folders_gives_an_empty_skipped_tuple():
    assert compute_changes(listing(e("/r/a")), [e("/r/a")]).skipped == ()


def test_a_pattern_with_a_leading_slash_names_one_place_below_the_root():
    rules = ScanRules(exclude=("/Trash", "\\Old Stuff", "/a/b"))
    for skipped in ("Trash/x.txt", "trash/deep/y.txt", "Old Stuff/z.txt", "a/b/c.txt"):
        assert not matches_rules(skipped, rules) and excluded(skipped.rsplit("/", 1)[0], rules), skipped
    for kept in ("proj/Trash/x.txt", "Trash.txt", "x/Old Stuff/z.txt", "q/a/b/c.txt", "a/bc.txt"):
        assert matches_rules(kept, rules), kept
    assert not matches_rules("proj/Trash/x.txt", ScanRules(exclude=("Trash",))), "a bare name still matches anywhere"
    assert matches_rules("a.txt", ScanRules(exclude=("/",))), "a lone slash is an empty pattern and skips nothing"
