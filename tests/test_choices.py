"""Folder choices saved to a file a person can read and edit, and loaded again with care (choices.py).

People will edit the file, and some edits will be wrong (Ben, 2026-10-28). So the tests are mostly mistakes:
each must be found and explained, every problem at once, and nothing applied while any is left.
"""

from __future__ import annotations

import datetime
import io
import ntpath
from pathlib import Path

import pytest

import helpers
from dugup import choices, cli
from dugup.choices import decode, dump, parse, parse_read, preview, read_words
from dugup.datadir import DataLayout
from dugup.service import LocalService
from dugup.settings import Settings
from dugup.shapes import (LEVEL_MEANING, LEVEL_NAMES, LEVEL_OCR, POLICY_NAMES_ONLY, POLICY_OPEN, Failure, Root)

DOCS = r"C:\Users\ben\Documents"
WHEN = datetime.datetime(2026, 10, 28, 20, 45)


def win(text):
    """Parse as on Windows, whatever this machine is."""
    return parse(text, ntpath.abspath, ntpath.normcase)


def problems(text):
    return win(text)[1]


# ---- the file as written --------------------------------------------------------------------------------------

def test_saving_and_loading_gives_back_the_same_choices():
    roots = [Root(DOCS, POLICY_OPEN, (), (".git", "/Codex"), levels=(("Screenshots", "words+alike"), ("Ollama", "names"))),
             Root(r"D:\LMStudio_Models", POLICY_NAMES_ONLY),
             Root(r"C:\Users\ben\it's mine", POLICY_OPEN, ("*.pdf",), (), levels=(("", LEVEL_OCR),))]
    text = dump(roots, r"C:\DugUp\data", WHEN)
    entries, found = win(text)
    assert found == [] and [e.root for e in entries] == roots
    assert "path = 'C:\\Users\\ben\\Documents'" in text, "a Windows path in single quotes, backslashes as typed"
    assert "Why keep this file" in text and "Saved 2026-10-28 20:45 from C:\\DugUp\\data" in text


def test_read_is_written_in_words_and_read_in_many_spellings():
    assert read_words(LEVEL_MEANING) == "everything" and read_words(LEVEL_NAMES) == "names"
    assert read_words("words+ocr+alike") == "words + scans + pictures alike"
    for typed, level in (("Everything", LEVEL_MEANING), ("ALL", LEVEL_MEANING), ("names only", LEVEL_NAMES),
                         ("file names", LEVEL_NAMES), ("words, scans", LEVEL_OCR), ("Words and OCR", LEVEL_OCR),
                         ("text + pictures", "words+alike"), ("scans", LEVEL_OCR), ("words+ocr", LEVEL_OCR)):
        assert parse_read(typed) == level, typed
    for wrong in ("evrything", "names + words", "", 5, None):
        assert parse_read(wrong) is None, wrong


def test_what_a_person_plainly_means_is_taken_as_meant():
    entries, found = win("[folder]\npath = 'C:\\Docs\\'\nskip = '.git'\n\n[folder.inside]\n'C:\\Docs\\Pics' = 'pictures'\n"
                         "'\\Letters\\2019\\' = 'names only'\n")
    assert found == []
    root = entries[0].root
    assert root.path == "C:\\Docs" and root.exclude == (".git",), "[folder] for one folder; skip as one word"
    assert dict(root.levels) == {"Pics": "alike", "Letters/2019": LEVEL_NAMES}, "inside: whole paths and backslashes"


def test_notepads_ways_of_saving_are_all_read():
    text = "[[folder]]\npath = 'C:\\Docs'\n"
    for data in (text.encode("utf-8"), b"\xef\xbb\xbf" + text.encode("utf-8"), text.encode("utf-16"),
                 text.encode("utf-16-be").join([b"\xfe\xff", b""]), "path = 'C:\\Café'".encode("cp1252")):
        assert decode(data).startswith("[[folder]]") or "Caf" in decode(data)
    assert win(decode(text.encode("utf-16")))[1] == []


# ---- mistakes: each found, said plainly, all at once ------------------------------------------------------------

def test_a_windows_path_in_double_quotes_is_explained():
    found = problems('[[folder]]\npath = "C:\\Users\\ben\\Documents"\n')
    assert len(found) == 1 and "Line 2" in found[0] and "single quotes" in found[0]


def test_one_bracket_for_two_folders_is_explained():
    found = problems("[folder]\npath = 'C:\\A'\n[folder]\npath = 'C:\\B'\n")
    assert len(found) == 1 and "[[folder]], with two brackets" in found[0]


def test_a_missing_quote_is_explained():
    found = problems("[[folder]]\npath = 'C:\\Docs\nread = 'words'\n")
    assert len(found) == 1 and "Line 2" in found[0] and "quote" in found[0]


def test_every_mistake_in_a_folder_is_named_at_once_with_what_was_meant():
    found = problems("[[folder]]\npath = 'C:\\Docs'\nreed = 'words'\nskp = '.git'\nread = 'evrything'\nskip = 5\n"
                     "[folder.inside]\n'Old' = 'some'\n'D:\\Else' = 'words'\n")
    joined = "\n".join(found)
    assert len(found) == 6, found
    assert 'did you mean "read"' in joined and 'did you mean "skip"' in joined
    assert 'read = "evrything" is not a choice' in joined and "skip should be a list" in joined
    assert '"Old" = "some"' in joined and '"D:\\Else" under inside is not a folder within C:\\Docs' in joined
    assert all(f.startswith("Folder 1 (C:\\Docs)") for f in found), "each says which folder"


def test_paths_that_cannot_be_used_are_named():
    found = problems("[[folder]]\npath = 'Documents'\n[[folder]]\nread = 'words'\n[[folder]]\npath='C:\\Docs'\n"
                     "[[folder]]\npath='c:\\docs\\'\n[[folder]]\npath='C:\\Docs\\Sub'\nstray = 1\n")
    joined = "\n".join(found)
    assert "must be a whole path" in joined and "Folder 2 has no path" in joined
    assert "is in the file twice" in joined and "lies inside Folder 3 (C:\\Docs)" in joined
    assert '"stray" is not a choice' in joined


def test_a_file_with_no_folder_or_something_else_is_explained():
    assert "names no folder" in problems("# only notes\n")[0]
    assert '"folders" is not something this file can hold (did you mean "folder"?)' in problems("[[folders]]\npath='C:\\A'\n")[0]


# ---- loading: shown first, applied whole or not at all ----------------------------------------------------------

def test_the_preview_says_what_is_new_changed_the_same_away_and_left_alone():
    entries, _ = win("[[folder]]\npath='C:\\A'\n[[folder]]\npath='C:\\B'\nread='words'\nskip=['/Old']\n"
                     "[[folder]]\npath='C:\\C'\n[[folder]]\npath='E:\\Gone'\n")
    current = [Root("C:\\B", exclude=("/Tmp",)), Root("C:\\C"), Root("C:\\Kept")]
    plan = preview(entries, current, exists=lambda p: not p.startswith("E:"), fold=ntpath.normcase)
    lines = plan.lines()
    assert [r.path for r in plan.new] == ["C:\\A"] and [r.path for r in plan.same] == ["C:\\C"]
    assert plan.not_here == ["E:\\Gone"] and plan.untouched == ["C:\\Kept"]
    assert "Change C:\\B: read everything -> words; also skip /Old; no longer skip /Tmp" in lines


@pytest.fixture
def made(tmp_path):
    docs, more = tmp_path / helpers.AWKWARD_FOLDER, tmp_path / "more"
    (docs / "Pics").mkdir(parents=True)
    more.mkdir()
    (docs / "a.txt").write_text("alpha", encoding="utf-8")
    layout = DataLayout(tmp_path / "data").ensure()
    service = LocalService(layout, Settings())
    yield service, docs, more, tmp_path
    service.close()


def test_a_saved_file_sets_an_empty_index_up_again(made):
    service, docs, more, tmp_path = made
    service.set_root(Root(str(docs), exclude=("/Old",), levels=(("Pics", "words+alike"),)))
    service.set_root(Root(str(more), POLICY_NAMES_ONLY))
    text = service.save_choices()
    for root in service.list_roots():
        service.remove_root(root.path)
    plan = service.preview_choices(text)
    assert plan.problems == [] and len(plan.new) == 2
    assert service.apply_choices(plan) == [str(docs), str(more)]
    again = {r.path: r for r in service.list_roots()}
    assert again[str(docs)].exclude == ("/Old",) and dict(again[str(docs)].levels) == {"Pics": "words+alike"}
    assert again[str(more)].policy == POLICY_NAMES_ONLY


def test_nothing_is_applied_from_a_file_with_any_problem(made):
    service, docs, more, tmp_path = made
    text = dump([Root(str(docs)), Root(str(more))], "x").replace("read = 'everything'", "read = 'evrything'", 1)
    plan = service.preview_choices(text)
    assert plan.problems and isinstance(service.apply_choices(plan), Failure)
    assert service.list_roots() == [], "the good folder was not set up either"


def test_a_folder_that_clashes_with_one_indexed_now_is_a_problem_before_anything_changes(made):
    service, docs, more, tmp_path = made
    service.set_root(Root(str(docs)))
    text = dump([Root(str(more)), Root(str(docs / "Pics"))], "x")
    plan = service.preview_choices(text)
    assert any("overlaps a folder that is already indexed" in p for p in plan.problems)
    assert [r.path for r in service.list_roots()] == [str(docs)]


def test_the_commands_save_without_overwriting_and_load_only_with_yes(made):
    service, docs, more, tmp_path = made
    service.set_root(Root(str(docs)))
    service.close()
    target = tmp_path / "choices.txt"

    def run(*argv):
        out = io.StringIO()
        return cli.main([*argv, "--data-dir", str(tmp_path / "data")], stdout=out, environ={}), out.getvalue()

    code, said = run("roots", "save", str(target))
    assert code == 0 and "Saved the choices of 1 folder(s)" in said and target.exists()
    code, said = run("roots", "save", str(target))
    assert code == 1 and "is there already" in said and "--replace" in said
    target.write_text(target.read_text(encoding="utf-8") + f"\n[[folder]]\npath = '{more}'\nread = 'names'\n", encoding="utf-8")
    code, said = run("roots", "load", str(target))
    assert code == 0 and f"Add {more} (read: names)" in said and "Nothing was changed yet" in said
    code, said = run("roots", "load", str(target), "--yes")
    assert code == 0 and "Set up 1 folder(s)" in said
    target.write_text("[[folder]]\npath = 'nowhere'\n", encoding="utf-8")
    code, said = run("roots", "load", str(target), "--yes")
    assert code == 1 and "Nothing was changed. Fix these" in said and "whole path" in said
