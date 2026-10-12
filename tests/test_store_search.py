import helpers
import os
import random

import pytest

from dugup.shapes import FileKind, FileRecord, FileState, SegmentIn, SegmentKind
from dugup.store import SqliteStore, build_match
from helpers import AWKWARD_FOLDER, CAFE, CAFE_PLAIN, CJK, CYRILLIC, EMOJI, GREEK, MUELLER


def add(store, path, text="", size=10, modified=1.0, kind=FileKind.TEXT, pages=None):
    """Add a file with one text segment (or several, one per entry in `pages`)."""
    row = store.upsert_file(FileRecord(path=path, size=size, modified=modified, kind=kind))
    texts = pages if pages is not None else ([text] if text else [])
    store.replace_segments(row, [SegmentIn(page=i + 1, order=i, kind=SegmentKind.TEXT, text=t) for i, t in enumerate(texts)])
    return row


def paths(page):
    return [r.path for r in page.rows]


# ---- words ---------------------------------------------------------------------------

def test_finds_a_word_in_text_ignoring_case(store):
    add(store, "/d/a.txt", "The Quarterly Invoice is attached")
    assert paths(store.search_words("invoice")) == ["/d/a.txt"]
    assert paths(store.search_words("QUARTERLY")) == ["/d/a.txt"]


def test_accents_are_folded_in_both_directions(store):
    add(store, "/d/with-accent.txt", f"We met at the {CAFE} today")
    add(store, "/d/plain.txt", f"We met at the {CAFE_PLAIN} today")
    assert sorted(paths(store.search_words(CAFE_PLAIN))) == ["/d/plain.txt", "/d/with-accent.txt"]
    assert sorted(paths(store.search_words(CAFE))) == ["/d/plain.txt", "/d/with-accent.txt"]


@pytest.mark.parametrize("word", [GREEK, CYRILLIC, MUELLER, CJK])
def test_non_latin_and_accented_words_are_found(store, word):
    add(store, "/d/x.txt", f"before {word} after")
    assert paths(store.search_words(word)) == ["/d/x.txt"]


def test_an_emoji_next_to_a_word_does_not_hide_the_word(store):
    add(store, "/d/x.txt", f"great{EMOJI} news {EMOJI}")
    assert paths(store.search_words("news")) == ["/d/x.txt"]


def test_numbers_and_dashed_numbers_are_found_as_exact_phrases(store):
    add(store, "/d/real.txt", "Member id 123-45-6789 on file")
    add(store, "/d/decoy.txt", "Member id 123 99 6789 on file")
    add(store, "/d/plain.txt", "Account 1234567 opened")
    assert paths(store.search_words("123-45-6789")) == ["/d/real.txt"]
    assert paths(store.search_words("1234567")) == ["/d/plain.txt"]


def test_plurals_match_because_the_index_stems_words(store):
    # Documents a deliberate choice (DigUp's tokenizer uses stemming). If stemming
    # is ever dropped, change this test on purpose.
    add(store, "/d/x.txt", "three reports were filed")
    assert paths(store.search_words("report")) == ["/d/x.txt"]


def test_every_word_must_match(store):
    add(store, "/d/both.txt", "invoice for the hospital")
    add(store, "/d/one.txt", "invoice for the garage")
    assert paths(store.search_words("invoice hospital")) == ["/d/both.txt"]


def test_file_names_are_searchable_even_without_any_text(store):
    add(store, "/photos/Passport scan.png", kind=FileKind.IMAGE)
    assert paths(store.search_words("passport")) == ["/photos/Passport scan.png"]


def test_a_file_with_several_matching_pages_is_one_row_with_a_match_count(store):
    add(store, "/d/long.txt", pages=["alpha beta", "gamma alpha", "alpha again", "nothing"])
    page = store.search_words("alpha")
    assert paths(page) == ["/d/long.txt"]
    assert page.rows[0].matches == 3
    assert page.total == 1


@pytest.mark.parametrize("query", ['"', "AND", "OR", "NOT", "NEAR(", "NEAR/2", "*", "-", "a:b", "(", ")", "'", "x OR", '"unclosed', "col:val", "^start"])
def test_odd_queries_never_raise(store, query):
    add(store, "/d/x.txt", "some ordinary text")
    result = store.search_words(query)
    assert result.total >= 0


def test_empty_or_blank_query_returns_an_empty_page(store):
    add(store, "/d/x.txt", "text")
    for query in ("", "   ", "\t\n"):
        page = store.search_words(query)
        assert page.rows == [] and page.total == 0 and not page.truncated


def test_build_match_quotes_each_word_and_doubles_inner_quotes():
    assert build_match('a "b"') == '"a" """b"""'
    assert build_match("  ") is None


# ---- honest counts and truncation ------------------------------------------------------

def test_total_and_truncation_are_honest(store):
    for n in range(25):
        add(store, f"/d/f{n:02d}.txt", "needle in a haystack")
    first = store.search_words("needle", limit=10)
    assert (len(first.rows), first.total, first.truncated) == (10, 25, True)
    last = store.search_words("needle", limit=10, offset=20)
    assert (len(last.rows), last.total, last.truncated) == (5, 25, False)
    beyond = store.search_words("needle", limit=10, offset=100)
    assert (len(beyond.rows), beyond.total, beyond.truncated) == (0, 25, False)


def test_zero_limit_still_reports_the_total(store):
    add(store, "/d/a.txt", "needle")
    page = store.search_words("needle", limit=0)
    assert page.rows == [] and page.total == 1 and page.truncated


def test_negative_paging_is_refused(store):
    with pytest.raises(ValueError):
        store.search_words("x", limit=-1)
    with pytest.raises(ValueError):
        store.search_words("x", offset=-1)


# ---- sorting happens in the database ---------------------------------------------------------

def _many(store, count=40, seed=3):
    rng = random.Random(seed)
    sizes = rng.sample(range(1, 10_000), count)
    for n, size in enumerate(sizes):
        add(store, f"/d/file{n:02d}.txt", "needle", size=size, modified=float(rng.randint(1, 10_000)))
    return sizes


def test_sorting_a_truncated_list_returns_the_true_top_not_the_top_of_what_was_first_found(store):
    sizes = _many(store)
    page = store.search_words("needle", limit=5, sort="size", descending=True)
    assert [r.size for r in page.rows] == sorted(sizes, reverse=True)[:5]
    page = store.search_words("needle", limit=5, sort="size", descending=False)
    assert [r.size for r in page.rows] == sorted(sizes)[:5]


def test_paging_through_a_sorted_list_has_no_gaps_or_repeats(store):
    sizes = _many(store, count=23)
    seen = []
    for offset in range(0, 23, 7):
        seen += [r.size for r in store.search_words("needle", limit=7, offset=offset, sort="size").rows]
    assert seen == sorted(sizes)


def test_sorting_by_name_ignores_case(store):
    for name in ["b.txt", "A.txt", "c.txt"]:
        add(store, f"/d/{name}", "needle")
    assert [r.name for r in store.search_words("needle", sort="name").rows] == ["A.txt", "b.txt", "c.txt"]


def test_equal_sort_keys_keep_a_stable_order(store):
    for n in range(10):
        add(store, f"/d/same{n}.txt", "needle", size=5)
    first = paths(store.search_words("needle", sort="size"))
    assert first == paths(store.search_words("needle", sort="size"))


def test_unknown_sort_columns_and_injection_attempts_are_refused(store):
    for bad in ["nonsense", "size; DROP TABLE files", "1", "f.size DESC, (SELECT 1)"]:
        with pytest.raises(ValueError):
            store.search_words("x", sort=bad)
    assert store.file_count() == 0  # nothing was run


# ---- adding, replacing, repeating ---------------------------------------------------------------

def test_adding_the_same_path_updates_instead_of_duplicating(store):
    first = store.upsert_file(FileRecord(path="/d/a.txt", size=1, modified=1.0, kind=FileKind.TEXT))
    second = store.upsert_file(FileRecord(path="/d/a.txt", size=99, modified=2.0, kind=FileKind.TEXT))
    assert first == second and store.file_count() == 1
    assert store.get_file("/d/a.txt").size == 99


def test_replacing_segments_twice_gives_the_same_index_as_once(store):
    row = add(store, "/d/a.txt", "needle one")
    segments = [SegmentIn(1, 0, SegmentKind.TEXT, "needle one"), SegmentIn(2, 1, SegmentKind.TEXT, "needle two")]
    store.replace_segments(row, segments)
    store.replace_segments(row, segments)
    assert store.segment_count() == 2
    page = store.search_words("needle")
    assert page.total == 1 and page.rows[0].matches == 2


def test_replacing_text_forgets_the_old_words(store):
    row = add(store, "/d/a.txt", "alpha")
    store.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, "omega")])
    assert store.search_words("alpha").total == 0
    assert store.search_words("omega").total == 1


def test_replacing_never_drops_the_file_name_from_search(store):
    row = add(store, "/d/Passport.txt", "alpha")
    store.replace_segments(row, [])
    assert paths(store.search_words("passport")) == ["/d/Passport.txt"]


def test_name_segments_cannot_be_written_by_callers(store):
    row = store.upsert_file(FileRecord(path="/d/a.txt", size=1, modified=1.0, kind=FileKind.TEXT))
    with pytest.raises(ValueError):
        store.replace_segments(row, [SegmentIn(0, 0, SegmentKind.NAME, "x")])


def test_unknown_file_id_is_an_error_with_a_reason(store):
    from dugup.store import StoreError
    with pytest.raises(StoreError, match="no file"):
        store.replace_segments(12345, [])
    with pytest.raises(StoreError, match="no file"):
        store.set_state(12345, FileState.FAILED)


def test_states_and_error_notes_round_trip(store):
    row = add(store, "/d/a.txt", "x")
    store.set_state(row, FileState.PARTIAL, "page 3 unreadable")
    got = store.get_file("/d/a.txt")
    assert got.state is FileState.PARTIAL and got.error == "page 3 unreadable"
    with pytest.raises(ValueError):
        store.set_state(row, "done")  # must be a FileState


# ---- names with every kind of character ---------------------------------------------------------------

def test_awkward_folder_and_file_names_round_trip(store):
    path = f"/data/{AWKWARD_FOLDER}/{CAFE} {CJK}.txt"
    add(store, path, "needle")
    assert paths(store.search_words("needle")) == [path]
    assert store.get_file(path).path == path


def test_windows_style_paths_are_split_into_folder_and_name(store):
    add(store, "C:\\Users\\ben\\My Docs\\scan 1.pdf", "needle")
    row = store.search_words("needle").rows[0]
    assert row.name == "scan 1.pdf" and row.folder == "C:\\Users\\ben\\My Docs"


@pytest.mark.linux_only
def test_a_file_name_that_is_not_valid_text_survives_exactly(store):
    odd = os.fsdecode(b"/data/bad-\xff-name.txt")
    add(store, odd, "needle")
    found = store.search_words("needle").rows[0].path
    assert found == odd
    assert store.get_file(odd).path == odd


@pytest.mark.linux_only
def test_two_different_bad_names_do_not_collide(store):
    one, two = os.fsdecode(b"/d/x\xfe.txt"), os.fsdecode(b"/d/x\xff.txt")
    add(store, one, "needle")
    add(store, two, "needle")
    assert store.file_count() == 2


# ---- purge ----------------------------------------------------------------------------------------------------

def test_purge_removes_a_folder_and_everything_below_it_and_nothing_else(store):
    add(store, "/data/a/one.txt", "secretword")
    add(store, "/data/a/deep/two.txt", "secretword")
    add(store, "/data/ab/three.txt", "secretword")    # sibling whose name starts the same
    add(store, "/data/other/four.txt", "secretword")
    assert store.purge_folder("/data/a") == 2
    assert sorted(paths(store.search_words("secretword"))) == ["/data/ab/three.txt", "/data/other/four.txt"]
    assert store.file_count() == 2


def test_purge_accepts_a_trailing_separator(store):
    add(store, "/data/a/one.txt", "x1")
    assert store.purge_folder("/data/a/") == 1


def test_purge_of_a_single_file_path_and_of_nothing(store):
    add(store, "/data/a/one.txt", "x1")
    assert store.purge_folder("/data/nothing-here") == 0
    assert store.purge_folder("/data/a/one.txt") == 1


def test_purge_removes_regions_annotations_and_unread_bands_too(store):
    row = add(store, "/data/a/one.txt", "needle")
    seg = store.conn.execute("SELECT id FROM segments WHERE file_id=? AND kind='text'", (row,)).fetchone()[0]
    store.conn.execute("INSERT INTO regions(segment_id, engine, x0, y0, x1, y1, text) VALUES(?,?,?,?,?,?,?)",
                       (seg, "rapidocr", 0, 0, 10, 10, "needle"))
    store.conn.execute("INSERT INTO annotations(segment_id, kind, label, value) VALUES(?,?,?,?)", (seg, "k", "dob", "x"))
    store.conn.execute("INSERT INTO unread_bands(file_id, page, y0, y1, reason) VALUES(?,?,?,?,?)", (row, 1, 0, 10, "faint"))
    add(store, "/data/b/keep.txt", "needle")
    store.purge_folder("/data/a")
    for table in ("regions", "annotations", "unread_bands"):
        assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert store.conn.execute("SELECT COUNT(*) FROM fts").fetchone()[0] == store.conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]


def test_purge_can_ignore_case_when_asked(store):
    add(store, "C:\\Data\\Scans\\a.pdf", "needle")
    assert store.purge_folder("c:\\data\\scans", case_insensitive=False) == 0
    assert store.purge_folder("c:\\data\\scans", case_insensitive=True) == 1


def test_purge_with_non_ascii_folder_names(store):
    add(store, f"/data/{AWKWARD_FOLDER}/a.txt", "needle")
    assert store.purge_folder(f"/data/{AWKWARD_FOLDER}") == 1


@pytest.mark.parametrize("thorough", [False, True])
def test_purged_text_is_not_left_in_the_files(store_path, thorough):
    """Regression guard, not a proof that `thorough` does more.

    Measured: with secure_delete=ON and a truncating checkpoint, even a plain
    purge leaves no trace of the text at the sizes tried (0 to 8000 files).
    Removing the optimize or VACUUM steps does not change this result, so this
    test cannot tell them apart. It only guards the outcome.
    """
    marker = "zxqvunique" + "marker"
    with SqliteStore(store_path) as s:
        add(s, "/data/gone/a.txt", f"alpha {marker} omega")
        add(s, "/data/stay/b.txt", "alpha omega")
        s.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert marker.encode() in store_path.read_bytes()  # the check can fail: it is really there
    with SqliteStore(store_path) as s:
        assert s.purge_folder("/data/gone", thorough=thorough) == 1
    leftovers = [p for p in (store_path, store_path.with_name(store_path.name + "-wal")) if p.exists()]
    assert all(marker.encode() not in p.read_bytes() for p in leftovers)


# ---- where the matched words are -------------------------------------------------------------------------

def _mark(text):
    return text.replace("<", "\x02").replace(">", "\x03")


@pytest.mark.parametrize("marked,expected", [
    ("no marks here", None),
    ("a <needle> here", "needle"),
    ("<some> text and <some> more", "some"),                                   # one word twice: the first
    ("<some> filler <words> end", "some filler words"),                        # two words close together
    ("<some> alone. " + "x " * 100 + "then <some> <words> together", "some words"),   # the later place holds both
    ("<alpha> " + "x " * 100 + "<beta>", "alpha"),                              # too far apart: the first word
    ("<Some> <SOME> <words>", "Some SOME words"),
    ("broken <mark", None),
])
def test_the_span_covers_the_most_query_words_it_can(marked, expected):
    from dugup.store import _first_span
    span = _first_span(_mark(marked))
    plain = marked.replace("<", "").replace(">", "")
    assert (None if span is None else plain[span[0]:span[1]]) == expected


def test_evidence_for_two_words_points_at_where_both_stand(store):
    row = store.upsert_file(FileRecord(path="/d/a.txt", size=1, modified=1.0, kind=FileKind.TEXT))
    text = "gamma early. " + "filler " * 50 + "later gamma meets delta here"
    store.replace_segments(row, [SegmentIn(1, 0, SegmentKind.TEXT, text)])
    evidence = store.search_words("gamma delta").rows[0].evidence[0]
    assert text[evidence.start:evidence.end] == "gamma meets delta"


def test_word_forms_are_found_together_unless_the_exact_form_is_asked_for(store):
    """The index stores words by their stem (a choice, see TOKENIZER). The exact-form filter narrows that."""
    from dugup.shapes import FileKind, FileRecord, SearchFilters, SegmentIn, SegmentKind
    for name, text in (("a.txt", "they educate the public"), ("b.txt", "educated people value education highly"),
                       ("c.txt", "nursing staff"), ("d.txt", "the nurse on duty")):
        row = store.upsert_file(FileRecord(path=f"/d/{name}", size=1, modified=1.0, kind=FileKind.TEXT))
        store.replace_segments(row, [SegmentIn(0, 0, SegmentKind.TEXT, text)])
    names = lambda page: sorted(r.name for r in page.rows)
    assert names(store.search_words("education")) == ["a.txt", "b.txt"], "by stem: educate, educated, education"
    assert names(store.search_words("nursing")) == ["c.txt", "d.txt"]
    exact = SearchFilters(exact=True)
    assert names(store.search_words("education", filters=exact)) == ["b.txt"]
    assert names(store.search_words("nursing", filters=exact)) == ["c.txt"]
    assert names(store.search_words("nurses", filters=exact)) == [], "no file holds that very form"
    row = store.search_words("educated education", filters=exact).rows[0]
    shown = store.get_passage(row.evidence[0], context=0)
    assert shown.text == "educated people value education", "several exact words: the stretch that holds them"


def test_exact_words_are_cut_and_folded_like_the_index():
    from dugup.store import _exact_ok, _exact_span, _words, exact_terms
    import json
    assert [w for _, _, w in _words(f"E-mail the {helpers.CAFE}, re: R2-D2!")] == ["e", "mail", "the", "cafe", "re", "r2", "d2"]
    assert exact_terms(f"  {helpers.CAFE.upper()}  e-mail e-mail ") == ("cafe", "e", "mail")
    terms = json.dumps(["cafe"])
    assert _exact_ok(f"{helpers.CAFE}.txt", "", "name", terms) == 1 and _exact_ok(f"{helpers.CAFE}.txt", "", "body", terms) == 0
    assert _exact_ok("x", f"the {helpers.CAFE}s", "either", terms) == 0, "a plural is another form"
    assert _exact_ok(None, None, "either", terms) == 0
    assert _exact_span(f"caf\u00e9s and a {helpers.CAFE} here", ["cafe"]) == (12, 16)
    assert _exact_span("nothing here", ["cafe"]) is None
