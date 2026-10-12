"""Conformance checks for Store (CONTRACTS.md part 6), step 1 subset.

Each check takes one fresh, empty store `s`. The same checks run against the real SqliteStore and the
in-memory fake. They test promises of the contract (honest counts, sorting before truncating, boundary-aware
purge, all-or-nothing transactions), not how any one engine does it: word stemming, for example, is not
part of the contract, so the checks avoid words whose stems differ.
"""

from __future__ import annotations

import random

import helpers
from dugup.protocols import OCR_DONE, OCR_FAILED, OCR_WAITING, OcrLine, OcrResult, PageInfo, PageVerdict
from dugup.shapes import Box, Evidence, FileKind, FileRecord, FileState, Root, SegmentIn, SegmentKind
from dugup.store import StoreError

from .common import CheckFailed, require


def add(s, path, text="", size=10, modified=1.0, kind=FileKind.TEXT, pages=None):
    """Add a file with one text segment, or one per entry of `pages`. Returns the row id."""
    row = s.upsert_file(FileRecord(path=path, size=size, modified=modified, kind=kind))
    texts = pages if pages is not None else ([text] if text else [])
    s.replace_segments(row, [SegmentIn(page=i + 1, order=i, kind=SegmentKind.TEXT, text=t) for i, t in enumerate(texts)])
    return row


def paths(page):
    return [r.path for r in page.rows]


def raises(exc_type, fn, what):
    try:
        fn()
    except exc_type:
        return
    except Exception as other:
        raise CheckFailed(f"{what}: raised {type(other).__name__}, expected {exc_type.__name__}")
    raise CheckFailed(f"{what}: expected {exc_type.__name__} but nothing was raised")


# ---- files ---------------------------------------------------------------------------------------------

def check_file_round_trip_and_update_in_place(s):
    rec = FileRecord(path="/d/a.txt", size=11, modified=5.5, kind=FileKind.PDF, file_id="77", policy="full")
    row = s.upsert_file(rec)
    require(isinstance(row, int) and rec.row_id == row, "upsert_file must return the row id and set it on the record")
    got = s.get_file("/d/a.txt")
    require(got is not None and (got.path, got.size, got.modified, got.kind, got.file_id, got.state, got.error, got.policy, got.row_id)
            == ("/d/a.txt", 11, 5.5, FileKind.PDF, "77", FileState.PENDING, "", "full", row), "get_file did not return what was stored")
    require(s.get_file("/d/none.txt") is None, "get_file of an unknown path must be None")
    again = s.upsert_file(FileRecord(path="/d/a.txt", size=99, modified=9.0, kind=FileKind.PDF))
    require(again == row and s.file_count() == 1, "adding the same path again must update it, not duplicate it")
    require(s.get_file("/d/a.txt").size == 99, "the update was not stored")


def check_states_round_trip(s):
    row = add(s, "/d/a.txt", "findable")
    s.set_state(row, FileState.FAILED, "timed out")
    got = s.get_file("/d/a.txt")
    require(got.state is FileState.FAILED and got.error == "timed out", "state and error note did not round trip")
    require(s.search_words("findable").rows[0].state == "failed", "a search row must carry the file's state")
    raises(ValueError, lambda: s.set_state(row, "done"), "set_state with a plain string")


def check_unknown_row_ids_are_errors_with_a_reason(s):
    raises(StoreError, lambda: s.set_state(424242, FileState.DONE), "set_state on an unknown id")
    raises(StoreError, lambda: s.replace_segments(424242, []), "replace_segments on an unknown id")


def check_folder_and_name_split(s):
    add(s, "/d/sub/a.txt", "zebra")
    add(s, "C:\\Users\\Ben\\b.txt", "zebra")
    rows = {r.path: r for r in s.search_words("zebra").rows}
    a, b = rows["/d/sub/a.txt"], rows["C:\\Users\\Ben\\b.txt"]
    require((a.folder, a.name) == ("/d/sub", "a.txt"), "a slash path was not split into folder and name")
    require((b.folder, b.name) == ("C:\\Users\\Ben", "b.txt"), "a Windows path was not split into folder and name")


def check_awkward_names_round_trip(s):
    path = f"/data/{helpers.AWKWARD_FOLDER}/{helpers.CAFE} {helpers.CJK}.txt"
    add(s, path, "marmalade")
    require(s.get_file(path) is not None and s.get_file(path).path == path, "an awkward path did not round trip")
    require(paths(s.search_words("marmalade")) == [path], "an awkward path did not come back from a search")


# ---- word search ---------------------------------------------------------------------------------------

def check_names_are_searchable_without_text(s):
    add(s, "/d/Quarterly-Report.pdf")
    page = s.search_words("quarterly")
    require(paths(page) == ["/d/Quarterly-Report.pdf"] and page.rows[0].matches >= 1, "a file name must be searchable even with no text")
    require(s.search_words("nonexistentword").total == 0, "a word that is nowhere must find nothing")


def check_case_and_accents_do_not_matter(s):
    add(s, "/d/with.txt", f"We met at the {helpers.CAFE} today")
    add(s, "/d/plain.txt", f"We met at the {helpers.CAFE_PLAIN} today")
    both = ["/d/plain.txt", "/d/with.txt"]
    require(sorted(paths(s.search_words(helpers.CAFE_PLAIN))) == both, "searching without the accent must find both")
    require(sorted(paths(s.search_words(helpers.CAFE))) == both, "searching with the accent must find both")
    require(sorted(paths(s.search_words("MET"))) == both, "upper case must find lower case")


def check_non_latin_words_are_found(s):
    for i, word in enumerate([helpers.GREEK, helpers.CYRILLIC, helpers.MUELLER, helpers.CJK]):
        add(s, f"/d/w{i}.txt", f"before {word} after")
    for i, word in enumerate([helpers.GREEK, helpers.CYRILLIC, helpers.MUELLER, helpers.CJK]):
        require(paths(s.search_words(word)) == [f"/d/w{i}.txt"], f"word number {i} (non-Latin or accented) was not found")


def check_every_word_must_match(s):
    add(s, "/d/one.txt", "red apple")
    add(s, "/d/two.txt", "red car")
    require(paths(s.search_words("red apple")) == ["/d/one.txt"], "all words must match, not any")
    require(sorted(paths(s.search_words("red"))) == ["/d/one.txt", "/d/two.txt"], "one word must find both files")


def check_one_row_per_file_with_a_match_count(s):
    add(s, "/d/many.txt", pages=["alpha one", "alpha two", "beta"])
    page = s.search_words("alpha")
    require(len(page.rows) == 1 and page.total == 1, "a file with several matching pages must be one row and count once")
    require(page.rows[0].matches == 2, "the row must say how many segments matched")


def check_blank_queries_give_an_empty_page(s):
    add(s, "/d/a.txt", "something")
    for query in ("", "   ", "\t\n"):
        page = s.search_words(query)
        require(page.rows == [] and page.total == 0, f"a blank query {query!r} must give an empty page")


def check_odd_queries_never_raise(s):
    add(s, "/d/a.txt", "something")
    for query in ('"', "'", "*", "a AND", "NEAR(", "col:x", "\\", "---", "(", "x OR y", "a b\"c"):
        try:
            s.search_words(query)
        except Exception as exc:
            raise CheckFailed(f"query {query!r} raised {type(exc).__name__}")


def check_total_and_truncation_are_honest(s):
    for i in range(25):
        add(s, f"/d/f{i:02d}.txt", "needle")
    first = s.search_words("needle", limit=10)
    require(len(first.rows) == 10 and first.total == 25 and first.truncated, "a first page of 10 of 25 must say total 25 and truncated")
    last = s.search_words("needle", limit=10, offset=20)
    require(len(last.rows) == 5 and last.total == 25 and not last.truncated, "the last page must hold 5 and not be truncated")
    zero = s.search_words("needle", limit=0)
    require(zero.rows == [] and zero.total == 25, "limit 0 must still report the true total")
    beyond = s.search_words("needle", offset=500)
    require(beyond.rows == [] and beyond.total == 25, "an offset past the end must be empty with the true total")
    raises(ValueError, lambda: s.search_words("needle", limit=-1), "negative limit")
    raises(ValueError, lambda: s.search_words("needle", offset=-1), "negative offset")


# ---- sorting -------------------------------------------------------------------------------------------

def check_sorting_happens_before_truncation(s):
    sizes = list(range(1, 41))
    random.Random(7).shuffle(sizes)
    for i, size in enumerate(sizes):
        add(s, f"/d/f{i:02d}.txt", "needle", size=size)
    top = s.search_words("needle", limit=5, sort="size", descending=True)
    require([r.size for r in top.rows] == [40, 39, 38, 37, 36], "the largest files must come first even when the list is cut")
    bottom = s.search_words("needle", limit=5, sort="size")
    require([r.size for r in bottom.rows] == [1, 2, 3, 4, 5], "the smallest files must come first even when the list is cut")
    whole = [r.file_id for r in s.search_words("needle", limit=100, sort="size").rows]
    stitched = []
    for offset in range(0, 40, 5):
        stitched += [r.file_id for r in s.search_words("needle", limit=5, offset=offset, sort="size").rows]
    require(stitched == whole, "paging through a sorted list must have no gaps and no repeats")


def check_name_sort_ignores_case_and_ties_are_stable(s):
    for name in ("b.txt", "A.txt", "c.txt", "B2.txt"):
        add(s, f"/d/{name}", "needle")
    require([r.name for r in s.search_words("needle", sort="name").rows] == ["A.txt", "b.txt", "B2.txt", "c.txt"],
            "sorting by name must ignore case")
    for i in range(10):
        add(s, f"/e/tie{i}.txt", "pin", size=5)
    for descending in (False, True):
        ids = [r.file_id for r in s.search_words("pin", sort="size", descending=descending).rows]
        require(ids == sorted(ids), "files with equal sort keys must keep a stable order (oldest row first)")


def check_every_sort_key_works_and_matches_sort_counts(s):
    add(s, "/d/three.txt", pages=["zed", "zed", "zed"], size=3, modified=3.0)
    add(s, "/d/one.txt", pages=["zed"], size=1, modified=1.0)
    add(s, "/d/two.txt", pages=["zed", "zed"], size=2, modified=2.0)
    for key in ("name", "folder", "size", "modified", "kind", "matches", "rank"):
        for descending in (False, True):
            require(sorted(paths(s.search_words("zed", sort=key, descending=descending))) == ["/d/one.txt", "/d/three.txt", "/d/two.txt"],
                    f"sort by {key} lost or invented files")
    require(paths(s.search_words("zed", sort="matches", descending=True)) == ["/d/three.txt", "/d/two.txt", "/d/one.txt"],
            "sort by matches is wrong")
    require(paths(s.search_words("zed", sort="modified")) == ["/d/one.txt", "/d/two.txt", "/d/three.txt"], "sort by modified is wrong")


def check_unknown_sort_keys_are_refused(s):
    add(s, "/d/a.txt", "needle")
    for bad in ("bogus", "size; DROP TABLE files", "name DESC", "", "f.id"):
        raises(ValueError, lambda bad=bad: s.search_words("needle", sort=bad), f"sort by {bad!r}")
    require(paths(s.search_words("needle")) == ["/d/a.txt"], "the store must still work after refused sorts")


# ---- segments ------------------------------------------------------------------------------------------

def check_replacing_segments_replaces_and_is_repeatable(s):
    row = add(s, "/d/doc.txt", pages=["oldword here", "second page"])
    texts = [SegmentIn(1, 0, SegmentKind.TEXT, "newword here")]
    s.replace_segments(row, texts)
    once = (s.segment_count(), paths(s.search_words("newword")))
    s.replace_segments(row, texts)
    require((s.segment_count(), paths(s.search_words("newword"))) == once == (1, ["/d/doc.txt"]), "replacing twice must equal replacing once")
    require(s.search_words("oldword").total == 0, "replaced text must be forgotten")
    require(paths(s.search_words("doc")) == ["/d/doc.txt"], "replacing text must never drop the file name from search")
    s.replace_segments(row, [])
    require(s.segment_count() == 0 and s.search_words("newword").total == 0, "replacing with nothing must clear the text")
    require(paths(s.search_words("doc")) == ["/d/doc.txt"], "the file name must stay searchable after the text is cleared")


def check_segment_counts_can_include_names(s):
    add(s, "/d/a.txt", pages=["one", "two"])
    require(s.segment_count() == 2, "segment_count must count text segments only by default")
    require(s.segment_count(include_names=True) == 3, "include_names must add one name segment per file")


def check_name_segments_cannot_be_written_by_callers(s):
    row = add(s, "/d/a.txt", "keepme")
    raises(ValueError, lambda: s.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, "fine"), SegmentIn(0, 1, SegmentKind.NAME, "x")]),
           "writing a name segment")
    require(paths(s.search_words("keepme")) == ["/d/a.txt"], "a refused write must leave the old text in place")


# ---- purge ---------------------------------------------------------------------------------------------

def check_purge_is_boundary_aware_and_complete(s):
    add(s, "/data/a/x.txt", "alphaword")
    add(s, "/data/a/sub/y.txt", "alphaword")
    add(s, "/data/ab/z.txt", "alphaword")
    add(s, "/data/a.txt", "alphaword")
    require(s.purge_folder("/data/a") == 2, "purging /data/a must remove exactly the two files below it")
    require(sorted(paths(s.search_words("alphaword"))) == ["/data/a.txt", "/data/ab/z.txt"], "purging /data/a touched a neighbour or left text behind")
    require(s.file_count() == 2 and s.get_file("/data/a/x.txt") is None, "purged files must be gone")
    require(s.purge_folder("/data/ab/") == 1, "a trailing separator must be accepted")
    require(s.purge_folder("/nowhere") == 0, "purging nothing must return 0")


def check_purge_handles_windows_separators(s):
    add(s, "C:\\Data\\a\\x.txt", "bravoword")
    add(s, "C:\\Data\\ab\\y.txt", "bravoword")
    require(s.purge_folder("C:\\Data\\a") == 1, "a Windows folder must purge its own files")
    require(paths(s.search_words("bravoword")) == ["C:\\Data\\ab\\y.txt"], "a Windows neighbour folder was touched")


def check_purge_case_rule_is_explicit(s):
    add(s, "/Data/X/f.txt", "charlieword")
    require(s.purge_folder("/data/x", case_insensitive=False) == 0, "a case-sensitive purge must not match a different case")
    require(s.purge_folder("/data/x", case_insensitive=True) == 1, "a case-insensitive purge must match a different case")


def check_folders_with_capital_non_ascii_letters_match_under_either_case_rule(s):
    folder = f"/Data/{chr(0xC4)}rzte {chr(0xC9)}COLE {helpers.CYRILLIC}"      # capital A-umlaut, capital E-acute, Cyrillic capitals
    row = add(s, f"{folder}/a.txt", "echoword")
    add(s, "/Data/other/b.txt", "echoword")
    for rule in (True, False):
        require([r.row_id for r in s.list_files(prefix=folder, case_insensitive=rule)] == [row],
                f"a folder with capital non-ASCII letters must find its own files (case_insensitive={rule})")
        require(dict(s.state_counts(folder, case_insensitive=rule)) == {"pending": 1}, "state_counts must find them too")
    require(s.purge_folder(folder, case_insensitive=True) == 1 and paths(s.search_words("echoword")) == ["/Data/other/b.txt"],
            "purging such a folder must remove its files")


def check_purge_with_non_ascii_folder(s):
    folder = f"/data/{helpers.AWKWARD_FOLDER}"
    add(s, f"{folder}/a.txt", "deltaword")
    add(s, "/data/other/b.txt", "deltaword")
    require(s.purge_folder(folder) == 1 and paths(s.search_words("deltaword")) == ["/data/other/b.txt"], "an awkward folder name did not purge cleanly")


# ---- fingerprint ---------------------------------------------------------------------------------------

def check_fingerprint_round_trips_and_reports_changes(s):
    require(s.get_fingerprint() is None, "a new store has no fingerprint")
    require(list(s.changed_fingerprint_keys({"model": "m1"})) == ["model"], "with nothing stored, every key is new")
    fp = {"model": "m1", "name": helpers.CAFE, "dim": 768}
    s.set_fingerprint(fp)
    require(dict(s.get_fingerprint()) == fp, "the fingerprint did not round trip")
    require(list(s.changed_fingerprint_keys(fp)) == [], "an identical fingerprint must report no change")
    require(list(s.changed_fingerprint_keys({"model": "m2", "dim": 768, "extra": 1})) == ["extra", "model", "name"],
            "changed, added and removed keys must all be reported, sorted")
    s.set_fingerprint({"a": None})
    require(list(s.changed_fingerprint_keys({})) == ["a"], "a key stored as None is not the same as a key that is missing")
    raises(ValueError, lambda: s.set_fingerprint({"bad": object()}), "a fingerprint with non-JSON values")


# ---- transactions --------------------------------------------------------------------------------------

def check_transactions_are_all_or_nothing(s):
    try:
        with s.transaction():
            row = s.upsert_file(FileRecord("/d/a.txt", 1, 1.0))
            s.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, "ghostword")])
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    require(s.get_file("/d/a.txt") is None and s.file_count() == 0 and s.search_words("ghostword").total == 0,
            "a failed transaction must leave nothing behind")
    with s.transaction():
        add(s, "/d/b.txt", "kept")
    require(paths(s.search_words("kept")) == ["/d/b.txt"], "a finished transaction must be kept")


def check_nested_transactions_roll_back_only_the_inner_part(s):
    with s.transaction():
        add(s, "/d/outer1.txt", "outerword")
        try:
            with s.transaction():
                add(s, "/d/inner.txt", "innerword")
                raise RuntimeError("inner fails")
        except RuntimeError:
            pass
        add(s, "/d/outer2.txt", "outerword")
    require(sorted(paths(s.search_words("outerword"))) == ["/d/outer1.txt", "/d/outer2.txt"], "the outer work must survive an inner failure")
    require(s.search_words("innerword").total == 0 and s.get_file("/d/inner.txt") is None, "the inner work must be undone")
    try:
        with s.transaction():
            add(s, "/d/x.txt", "lateword")
            with s.transaction():
                add(s, "/d/y.txt", "lateword")
            raise RuntimeError("outer fails after inner succeeded")
    except RuntimeError:
        pass
    require(s.search_words("lateword").total == 0, "an outer failure must undo finished inner work too")


# ---- evidence ------------------------------------------------------------------------------------------

def check_search_rows_carry_evidence(s):
    add(s, "/d/a.txt", pages=["first page words", "second page needle"])
    row = s.search_words("needle").rows[0]
    evidence = getattr(row, "evidence", None)
    require(evidence, "every search result must carry Evidence (file, page, span)")
    for ev in evidence:
        require(isinstance(ev, Evidence) and ev.file_id == row.file_id, "evidence must be about the row's own file")
        require(ev.page >= 0 and 0 <= ev.start <= ev.end, "evidence must have a sensible page and span")


def _shown(s, ev):
    passage = s.get_passage(ev, context=5)
    require(passage is not None, "get_passage must return the text behind fresh Evidence")
    return passage, passage.text[passage.start:passage.end]


def check_evidence_points_at_the_words(s):
    add(s, "/d/a.txt", pages=["first page words", "the second page holds a Needle in the middle"])
    add(s, "/d/Quarterly-Report.pdf")
    ev = s.search_words("needle").rows[0].evidence[0]
    require(ev.page == 2, "the best evidence must be on the page that holds the word")
    passage, shown = _shown(s, ev)
    require(shown.casefold() == "needle", f"the evidence span must cover the matched word, got {shown!r}")
    require(passage.page == 2 and passage.kind is SegmentKind.TEXT, "the passage must say which page and kind it is")
    require(len(passage.text) <= len("needle") + 10, "the passage must respect the context size")
    name_ev = s.search_words("quarterly").rows[0].evidence[0]
    passage, shown = _shown(s, name_ev)
    require(name_ev.page == 0 and passage.kind is SegmentKind.NAME and shown.casefold() == "quarterly",
            "a match in the file name must point into the name (page 0)")


def check_evidence_prefers_text_to_the_name(s):
    add(s, "/d/needle.txt", pages=["plain words", "a needle here"])
    ev = s.search_words("needle").rows[0].evidence
    require(ev[0].page == 2, "when the text and the name both match, the text comes first")
    require(any(e.page == 0 for e in ev), "the name match is still listed")
    require(all(e.page != 1 for e in ev), "a page that matched only through the file name is not where the words are")


def check_old_evidence_is_not_shown_for_new_text(s):
    row = add(s, "/d/a.txt", pages=["the needle is here"])
    ev = s.search_words("needle").rows[0].evidence[0]
    s.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, "entirely different words")])
    require(s.get_passage(ev) is None, "Evidence from before a re-read must not show the new text")
    require(s.get_passage(Evidence(row, 1, 0, 3)) is None, "Evidence with no segment handle has no passage")


# ---- listing, moving, missing ---------------------------------------------------------------------------

def check_list_files_filters_and_walks(s):
    rows = [add(s, f"/data/a/f{i}.txt", "w") for i in range(5)]
    add(s, "/data/ab/z.txt", "w")
    s.set_state(rows[1], FileState.DONE)
    s.set_state(rows[3], FileState.DONE)
    s.set_missing(rows[4], 100.0)
    under = s.list_files(prefix="/data/a")
    require([r.row_id for r in under] == rows, "list_files must give the files below a folder, in row order, and no neighbour")
    require(len(s.list_files()) == 6, "list_files with no filter must give every file")
    require([r.row_id for r in s.list_files(prefix="/data/a", state=FileState.DONE)] == [rows[1], rows[3]], "the state filter is wrong")
    require([r.row_id for r in s.list_files(prefix="/data/a", missing=True)] == [rows[4]], "missing=True must give only missing files")
    require(rows[4] not in [r.row_id for r in s.list_files(missing=False)], "missing=False must leave missing files out")
    walked, after = [], 0
    while True:
        batch = s.list_files(prefix="/data/a", limit=2, after_row=after)
        if not batch:
            break
        walked += [r.row_id for r in batch]
        after = batch[-1].row_id
    require(walked == rows, "walking with limit and after_row must visit every file once")
    raises(ValueError, lambda: s.list_files(state="done"), "list_files with a plain string state")


def check_move_keeps_text_and_state(s):
    row = add(s, "/d/old name.txt", pages=["keepword here"])
    s.set_state(row, FileState.DONE)
    s.set_pages(row, [PageInfo(1, PageVerdict.BORN_DIGITAL, False)])
    s.set_missing(row, 5.0)
    require(s.move_file("/d/old name.txt", "/e/fresh title.txt") == row, "move_file must return the same row id")
    got = s.get_file("/e/fresh title.txt")
    require(got is not None and got.row_id == row and got.state is FileState.DONE, "a moved file keeps its row and state")
    require(got.missing_since is None, "a file found at a new path is no longer missing")
    require(s.get_file("/d/old name.txt") is None and s.file_count() == 1, "the old path must be gone, with no duplicate")
    require(paths(s.search_words("keepword")) == ["/e/fresh title.txt"], "a moved file keeps its text")
    require(len(s.get_pages(row)) == 1, "a moved file keeps its page records")
    require(paths(s.search_words("fresh")) == ["/e/fresh title.txt"], "the new name must be searchable")
    require(s.search_words("old").total == 0, "the old name must no longer be found")
    hit = s.search_words("keepword").rows[0]
    require((hit.folder, hit.name) == ("/e", "fresh title.txt"), "folder and name must follow the move")


def check_move_refuses_unknown_and_taken_paths(s):
    add(s, "/d/a.txt", "one")
    add(s, "/d/b.txt", "two")
    raises(StoreError, lambda: s.move_file("/d/none.txt", "/d/c.txt"), "moving an unknown file")
    raises(StoreError, lambda: s.move_file("/d/a.txt", "/d/b.txt"), "moving onto an existing path")
    require(sorted(r.path for r in s.list_files()) == ["/d/a.txt", "/d/b.txt"], "a refused move must change nothing")


def check_missing_files_are_kept_and_flagged(s):
    row = add(s, "/d/gone.txt", "ghostword")
    s.set_state(row, FileState.DONE)
    other = add(s, "/d/here.txt", "ghostword")
    s.set_state(other, FileState.DONE)
    s.set_missing(row, 1000.0)
    got = s.get_file("/d/gone.txt")
    require(got.missing_since == 1000.0 and got.state is FileState.DONE, "missing is a flag with a date; the state is kept")
    hits = {r.path: r for r in s.search_words("ghostword").rows}
    require(set(hits) == {"/d/gone.txt", "/d/here.txt"}, "a missing file must stay searchable until it is purged")
    require(hits["/d/gone.txt"].missing_since == 1000.0 and hits["/d/here.txt"].missing_since is None,
            "a search row must say when its file went missing")
    require(dict(s.state_counts()) == {"missing": 1, "done": 1}, "a missing file is counted as missing, not under its old state")
    s.set_missing(row, None)
    require(s.get_file("/d/gone.txt").missing_since is None and dict(s.state_counts()) == {"done": 2}, "clearing the flag restores the file")
    raises(StoreError, lambda: s.set_missing(424242, 1.0), "set_missing on an unknown id")


def check_purge_missing_respects_age_and_folder(s):
    old = add(s, "/data/a/old.txt", "agedword")
    new = add(s, "/data/a/new.txt", "agedword")
    add(s, "/data/a/present.txt", "agedword")
    elsewhere = add(s, "/data/ab/old.txt", "agedword")
    s.set_missing(old, 100.0)
    s.set_missing(new, 900.0)
    s.set_missing(elsewhere, 100.0)
    require(s.purge_missing("/data/a", before=500.0) == 1, "only files missing since before the date, below the folder, are purged")
    require(sorted(paths(s.search_words("agedword"))) == ["/data/a/new.txt", "/data/a/present.txt", "/data/ab/old.txt"],
            "purge_missing removed the wrong files or left text behind")


def check_remove_files_takes_them_out_completely(s):
    keep = add(s, "/d/keep.txt", "foxtrotword")
    gone = add(s, "/d/gone.pdf", pages=["foxtrotword one", "foxtrotword two"], kind=FileKind.PDF)
    s.set_pages(gone, [PageInfo(1, PageVerdict.BORN_DIGITAL, False)])
    require(s.remove_files([gone, 424242]) == 1, "remove_files must count the files that were there and ignore unknown ids")
    require(paths(s.search_words("foxtrotword")) == ["/d/keep.txt"], "a removed file must leave no text behind")
    require(s.search_words("gone").total == 0, "a removed file's name must not be found")
    require(s.get_file("/d/gone.pdf") is None and s.file_count() == 1 and list(s.get_pages(gone)) == [], "the record and its pages must be gone")
    require(s.remove_files([]) == 0 and s.get_file("/d/keep.txt").row_id == keep, "removing nothing changes nothing")


def check_state_counts_by_folder(s):
    a = add(s, "/data/a/x.txt")
    add(s, "/data/a/y.txt")
    add(s, "/data/ab/z.txt")
    s.set_state(a, FileState.FAILED, "why")
    require(dict(s.state_counts("/data/a")) == {"failed": 1, "pending": 1}, "state_counts for a folder is wrong")
    require(dict(s.state_counts()) == {"failed": 1, "pending": 2}, "state_counts for everything is wrong")
    require(dict(s.state_counts("/nowhere")) == {}, "state_counts of an unknown folder is empty")


# ---- pages ---------------------------------------------------------------------------------------------

def check_pages_round_trip_and_replace(s):
    row = add(s, "/d/doc.pdf", kind=FileKind.PDF)
    pages = [PageInfo(2, PageVerdict.NO_LAYER, True), PageInfo(1, PageVerdict.BORN_DIGITAL, False),
             PageInfo(3, PageVerdict.UNREADABLE, False, f"damaged {helpers.CAFE}")]
    s.set_pages(row, pages)
    require(list(s.get_pages(row)) == sorted(pages, key=lambda p: p.page), "page records must round trip, in page order")
    s.set_pages(row, pages[:1])
    require(list(s.get_pages(row)) == pages[:1], "set_pages must replace, not add")
    require(list(s.get_pages(424242)) == [], "an unknown file has no pages")
    raises(StoreError, lambda: s.set_pages(424242, pages), "set_pages on an unknown id")
    s.purge_folder("/d")
    require(list(s.get_pages(row)) == [], "purging a file must remove its page records")


# ---- OCR -----------------------------------------------------------------------------------------------

def _scan(s, path="/d/scan.pdf", kind=FileKind.PDF):
    row = add(s, path, kind=kind)
    s.set_pages(row, [PageInfo(1, PageVerdict.BORN_DIGITAL, False), PageInfo(2, PageVerdict.NO_LAYER, True),
                      PageInfo(3, PageVerdict.NO_LAYER, True)])
    return row


def _read(*lines):
    return OcrResult(tuple(OcrLine(text, Box(*box), 0.9) for text, box in lines), "fake-ocr", "1.0")


def check_ocr_queue_lists_waiting_pages(s):
    row = _scan(s)
    gone = _scan(s, "/d/gone.pdf")
    s.set_missing(gone, 5.0)
    other = _scan(s, "/e/photo.jpg", FileKind.IMAGE)
    tasks = list(s.ocr_queue())
    require([(t.row_id, t.page) for t in tasks] == [(row, 2), (row, 3), (other, 2), (other, 3)],
            "the queue must hold the pages that need OCR of files that are not missing, in file then page order")
    require(tasks[0].path == "/d/scan.pdf" and tasks[0].kind is FileKind.PDF and tasks[0].width is None,
            "a task must carry the file's path and kind, and no shape before one is recorded")
    require([(t.row_id, t.page) for t in s.ocr_queue("/e")] == [(other, 2), (other, 3)], "the queue must respect the folder asked for")
    s.set_page_shape(other, 2, 640, 480, True)
    task = next(t for t in s.ocr_queue("/e") if t.page == 2)
    require((task.width, task.height, task.camera) == (640, 480, True), "a recorded shape must come back with the task")
    raises(StoreError, lambda: s.set_page_shape(other, 99, 1, 1, False), "set_page_shape on an unknown page")
    raises(StoreError, lambda: s.save_ocr(row, 99, _read()), "save_ocr on an unknown page")
    raises(StoreError, lambda: s.fail_ocr(424242, 1, "x"), "fail_ocr on an unknown file")


def check_ocr_text_is_searchable_with_a_box(s):
    row = _scan(s)
    s.save_ocr(row, 2, _read(("Antonio's Trattoria", (100, 100, 600, 160)), ("  ", (0, 0, 10, 10)),
                             ("open daily", (100, 700, 400, 760))))
    require([(t.row_id, t.page) for t in s.ocr_queue()] == [(row, 3)], "a page that was read must leave the queue")
    require(not s.get_pages(row)[1].needs_ocr, "a page that was read no longer needs OCR")
    require(list(s.ocr_status(row)) == [(2, OCR_DONE, ""), (3, OCR_WAITING, "")], "ocr_status must say what became of each page")
    found = s.search_words("daily").rows
    require(len(found) == 1 and found[0].file_id == row, "text read by OCR must be searchable")
    ev = found[0].evidence[0]
    passage = s.get_passage(ev, context=0)
    require(passage is not None and passage.kind is SegmentKind.OCR and passage.page == 2
            and passage.text.casefold() == "daily", "the evidence must point at the word, on its page, as OCR text")
    require(ev.box == Box(100, 700, 400, 760), "evidence from OCR must carry the box of the line the word is on")
    require(s.search_words("antonio's").rows[0].evidence[0].box == Box(100, 100, 600, 160), "the first line has its own box")
    s.save_ocr(row, 3, _read(("a b", (1, 1, 9, 9)), ("c d", (1, 11, 9, 19)), ("e zz", (1, 21, 9, 29))))
    require(s.search_words("zz").rows[0].evidence[0].box == Box(1, 21, 9, 29),
            "the last word of the last line must get that line's box (lines are joined by one line break)")
    s.save_ocr(row, 3, _read())
    s.save_ocr(row, 2, _read(("entirely different", (1, 1, 9, 9))))
    require(s.search_words("daily").total == 0 and s.search_words("different").total == 1,
            "saving OCR for a page again must replace what was stored, not add to it")
    require(s.segment_count() == 1, "one page read twice holds one piece of OCR text")


def check_ocr_blank_and_failed_pages_leave_the_queue(s):
    row = _scan(s)
    s.save_ocr(row, 2, _read())
    require(s.segment_count() == 0, "a page with nothing on it stores no text")
    s.fail_ocr(row, 3, f"timed_out: too slow {helpers.CAFE}")
    require(list(s.ocr_queue()) == [], "a blank page and a failed page both leave the queue")
    require(list(s.ocr_status(row)) == [(2, OCR_DONE, ""), (3, OCR_FAILED, f"timed_out: too slow {helpers.CAFE}")],
            "a failed page must keep its reason")
    require(list(s.retry_ocr("/elsewhere")) == [], "retry must respect the folder asked for")
    require(list(s.retry_ocr()) == [row], "retry must name the files it put back")
    require([(t.row_id, t.page) for t in s.ocr_queue()] == [(row, 3)], "a retried page waits again; a read page does not")
    require(list(s.retry_ocr()) == [], "nothing is left to retry")


def check_reread_clears_ocr(s):
    row = _scan(s)
    s.save_ocr(row, 2, _read(("needle", (1, 1, 9, 9))))
    s.fail_ocr(row, 3, "crashed: x")
    s.set_page_shape(row, 3, 10, 10, False)
    s.replace_segments(row, [])
    s.set_pages(row, [PageInfo(1, PageVerdict.NO_LAYER, False), PageInfo(2, PageVerdict.NO_LAYER, False), PageInfo(3, PageVerdict.NO_LAYER, True)])
    require(s.search_words("needle").total == 0, "reading a file again must drop the OCR text of the old content")
    tasks = list(s.ocr_queue())
    require([(t.page, t.width) for t in tasks] == [(3, None)], "after a re-read the pages wait afresh, with no old shape or failure")
    require(list(s.ocr_status(row)) == [(3, OCR_WAITING, "")], "old OCR outcomes must not survive a re-read")


# ---- roots ---------------------------------------------------------------------------------------------

def check_roots_round_trip_and_keep_their_status(s):
    require(list(s.list_roots()) == [], "a new store has no roots")
    folder = f"/data/{helpers.AWKWARD_FOLDER}"
    s.set_root(Root(folder, "open", ("*.pdf",), (".git", f"{helpers.CAFE}*")))
    s.set_root(Root("/data/b"))
    got = {r.path: r for r in s.list_roots()}
    require(set(got) == {folder, "/data/b"}, "roots did not round trip")
    require((got[folder].policy, got[folder].include, got[folder].exclude) == ("open", ("*.pdf",), (".git", f"{helpers.CAFE}*")),
            "a root's policy and rules did not round trip")
    require(got[folder].reachable and got[folder].last_scan is None, "a new root has not been scanned")
    s.note_scan(folder, False, 123.5, ["root_empty"])
    s.set_root(Root(folder, "names_only"))
    again = {r.path: r for r in s.list_roots()}[folder]
    require((again.policy, again.include, again.exclude) == ("names_only", (), ()), "set_root must update policy and rules")
    require((again.reachable, again.last_scan, again.warnings) == (False, 123.5, ("root_empty",)),
            "changing a root's settings must keep what the last scan found")
    raises(StoreError, lambda: s.note_scan("/not/a/root", True, 1.0), "note_scan for a folder that is not a root")
    require(s.remove_root("/data/b") is True and s.remove_root("/data/b") is False, "remove_root must say whether it removed anything")
    require([r.path for r in s.list_roots()] == [folder], "the removed root must be gone and the other kept")


CHECKS = {
    "file_round_trip_and_update_in_place": check_file_round_trip_and_update_in_place,
    "states_round_trip": check_states_round_trip,
    "unknown_row_ids_are_errors_with_a_reason": check_unknown_row_ids_are_errors_with_a_reason,
    "folder_and_name_split": check_folder_and_name_split,
    "awkward_names_round_trip": check_awkward_names_round_trip,
    "names_are_searchable_without_text": check_names_are_searchable_without_text,
    "case_and_accents_do_not_matter": check_case_and_accents_do_not_matter,
    "non_latin_words_are_found": check_non_latin_words_are_found,
    "every_word_must_match": check_every_word_must_match,
    "one_row_per_file_with_a_match_count": check_one_row_per_file_with_a_match_count,
    "blank_queries_give_an_empty_page": check_blank_queries_give_an_empty_page,
    "odd_queries_never_raise": check_odd_queries_never_raise,
    "total_and_truncation_are_honest": check_total_and_truncation_are_honest,
    "sorting_happens_before_truncation": check_sorting_happens_before_truncation,
    "name_sort_ignores_case_and_ties_are_stable": check_name_sort_ignores_case_and_ties_are_stable,
    "every_sort_key_works": check_every_sort_key_works_and_matches_sort_counts,
    "unknown_sort_keys_are_refused": check_unknown_sort_keys_are_refused,
    "replacing_segments_replaces_and_is_repeatable": check_replacing_segments_replaces_and_is_repeatable,
    "segment_counts_can_include_names": check_segment_counts_can_include_names,
    "name_segments_cannot_be_written_by_callers": check_name_segments_cannot_be_written_by_callers,
    "purge_is_boundary_aware_and_complete": check_purge_is_boundary_aware_and_complete,
    "purge_handles_windows_separators": check_purge_handles_windows_separators,
    "folders_with_capital_non_ascii_letters_match": check_folders_with_capital_non_ascii_letters_match_under_either_case_rule,
    "search_rows_carry_evidence": check_search_rows_carry_evidence,
    "evidence_points_at_the_words": check_evidence_points_at_the_words,
    "evidence_prefers_text_to_the_name": check_evidence_prefers_text_to_the_name,
    "old_evidence_is_not_shown_for_new_text": check_old_evidence_is_not_shown_for_new_text,
    "list_files_filters_and_walks": check_list_files_filters_and_walks,
    "move_keeps_text_and_state": check_move_keeps_text_and_state,
    "move_refuses_unknown_and_taken_paths": check_move_refuses_unknown_and_taken_paths,
    "missing_files_are_kept_and_flagged": check_missing_files_are_kept_and_flagged,
    "purge_missing_respects_age_and_folder": check_purge_missing_respects_age_and_folder,
    "state_counts_by_folder": check_state_counts_by_folder,
    "remove_files_takes_them_out_completely": check_remove_files_takes_them_out_completely,
    "pages_round_trip_and_replace": check_pages_round_trip_and_replace,
    "ocr_queue_lists_waiting_pages": check_ocr_queue_lists_waiting_pages,
    "ocr_text_is_searchable_with_a_box": check_ocr_text_is_searchable_with_a_box,
    "ocr_blank_and_failed_pages_leave_the_queue": check_ocr_blank_and_failed_pages_leave_the_queue,
    "reread_clears_ocr": check_reread_clears_ocr,
    "roots_round_trip_and_keep_their_status": check_roots_round_trip_and_keep_their_status,
    "purge_case_rule_is_explicit": check_purge_case_rule_is_explicit,
    "purge_with_non_ascii_folder": check_purge_with_non_ascii_folder,
    "fingerprint_round_trips_and_reports_changes": check_fingerprint_round_trips_and_reports_changes,
    "transactions_are_all_or_nothing": check_transactions_are_all_or_nothing,
    "nested_transactions_roll_back_only_the_inner_part": check_nested_transactions_roll_back_only_the_inner_part,
}
