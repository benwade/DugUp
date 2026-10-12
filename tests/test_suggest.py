"""Suggestions for what to skip: pure logic on made-up listings, then the commands on real folders."""

import io

import pytest

import helpers
from dugup import cli
from dugup.datadir import DataLayout
from dugup.service import LocalService
from dugup.shapes import Failure, FailureReason, FileKind, Root
from dugup.suggest import REVIEW, SAFE, suggest_excludes

ROOT = "C:\\Users\\me\\Stuff"


def listing(*relative, root=ROOT, sep="\\"):
    return [(root + sep + r.replace("/", sep), 10) for r in relative]


def many(folder, count, ext="bin"):
    return [f"{folder}/f{i:04d}.{ext}" for i in range(count)]


def by_pattern(found):
    return {s.pattern: s for s in found}


def test_downloaded_requirements_are_safe_to_skip_wherever_they_are():
    found = by_pattern(suggest_excludes(ROOT, listing(
        "app/node_modules/left-pad/index.js", "app/node_modules/left-pad/readme.md", "other/deep/node_modules/x/y.js",
        "tool/Lib/site-packages/requests/api.py", "notes/a.txt")))
    assert found["node_modules"].group == SAFE and found["node_modules"].places == 2 and found["node_modules"].files == 3
    assert "downloaded requirements" in found["node_modules"].reason
    assert found["site-packages"].group == SAFE and found["site-packages"].example == "tool/Lib/site-packages"
    assert "notes" not in found


def test_an_environment_is_recognised_by_its_marker_whatever_it_is_called():
    found = by_pattern(suggest_excludes(ROOT, listing(
        "proj/my-env/pyvenv.cfg", "proj/my-env/Lib/site-packages/a/b.py", "proj/my-env/Scripts/python.exe",
        "miniconda/conda-meta/history", "miniconda/pkgs/x.py", "proj/main.py")))
    assert found["proj/my-env"].group == SAFE and found["proj/my-env"].files == 3
    assert found["/miniconda"].group == SAFE and "conda" in found["/miniconda"].reason
    assert "site-packages" not in found, "a folder inside one that is already offered is not offered again"


def test_tool_data_and_system_folders_are_safe_and_names_ignore_case():
    found = by_pattern(suggest_excludes(ROOT, listing(".git/HEAD", "a/__pycache__/x.pyc", "$RECYCLE.BIN/S-1/x", "A/.GIT/config")))
    assert {found[".git"].group, found["__pycache__"].group, found["$RECYCLE.BIN"].group} == {SAFE}
    assert found[".git"].places == 2


def test_a_safe_folder_is_never_hidden_under_a_broader_suggestion():
    # a project whose bulk is its environment and packages: without them it is an ordinary, readable project
    files = (many("proj/.venv/Lib/site-packages/big", 400, "py") + ["proj/.venv/pyvenv.cfg"]
             + many("proj/web/node_modules/dep", 300, "js") + ["proj/.ruff_cache/x.bin", "proj/.git/HEAD"]
             + ["proj/notes.md", "proj/report.docx", "proj/src/main.py"])
    found = by_pattern(suggest_excludes(ROOT, listing(*files)))
    assert {"proj/.venv", "node_modules", ".ruff_cache", ".git"} <= set(found)
    assert all(found[p].group == SAFE for p in ("proj/.venv", "node_modules", ".ruff_cache", ".git"))
    assert "/proj" not in found, "the project is not 'almost all unreadable' once its packages are set aside"
    assert found["proj/.venv"].files == 401


def test_what_is_left_after_the_safe_folders_is_judged_on_its_own():
    files = many("proj/.venv/lib", 50, "py") + ["proj/.venv/pyvenv.cfg"] + many("proj/data", 400, "bin") + ["proj/a.txt"]
    found = by_pattern(suggest_excludes(ROOT, listing(*files)))
    assert found["proj/.venv"].group == SAFE
    assert found["/proj"].group == REVIEW and found["/proj"].files == 401, "counted without the environment's files"


def test_trash_backups_and_build_output_are_the_users_call():
    found = by_pattern(suggest_excludes(ROOT, listing(
        "Trash/old.txt", "work/Temp/x.txt", "Backups/2024/a.docx",
        "site/package.json", "site/dist/bundle.js", "site/src/a.js",
        "novel/build/chapter.txt", "novel/draft.txt")))
    assert found["/Trash"].group == REVIEW and found["/Trash"].readable == 1
    assert found["work/Temp"].group == REVIEW and found["/Backups"].group == REVIEW
    assert found["site/dist"].group == REVIEW and "build output" in found["site/dist"].reason
    assert "novel/build" not in found, "a folder named build is only build output inside a code project"


def test_a_folder_where_almost_nothing_can_be_read_is_offered_with_what_it_holds():
    files = many("dataset/shards", 300, "bin") + many("dataset/shards", 3, "txt") + ["dataset/readme.md", "docs/a.pdf"]
    found = by_pattern(suggest_excludes(ROOT, listing(*files)))
    assert set(found) == {"/dataset"}, "the top folder is offered once, not each folder inside it"
    s = found["/dataset"]
    assert s.group == REVIEW and ".bin" in s.reason and (s.files, s.readable) == (304, 4)


def test_mostly_readable_or_small_folders_are_left_alone():
    files = many("mixed", 300, "bin") + many("mixed", 30, "txt") + many("small", 50, "bin")
    assert suggest_excludes(ROOT, listing(*files)) == []


def test_a_long_run_of_numbered_pictures_is_offered_but_a_photo_folder_is_not():
    frames = [f"renders/frames/frame_{i:04d}.png" for i in range(40)]
    photos = [f"holiday/{name}.jpg" for name in ("beach", "antonios sign", "dinner", "sunset")] + \
             [f"camera/IMG_{i:04d}.jpg" for i in range(60)] + [f"papers/Scan{i:04d}.jpg" for i in range(30)] + \
             [f"phone/{20260101 + i}.jpg" for i in range(25)] + [f"book/page-{i:03d}.png" for i in range(40)]
    found = by_pattern(suggest_excludes(ROOT, listing(*(frames + photos))))
    assert set(found) == {"renders/frames"} and "numbered pictures" in found["renders/frames"].reason
    assert found["renders/frames"].readable == 40


def test_the_root_itself_is_never_offered_and_other_roots_files_are_ignored():
    files = listing(*many("x", 0)) + [(ROOT + "\\" + f"f{i}.bin", 1) for i in range(400)] + [("C:\\Users\\me\\Stuff2\\node_modules\\a.js", 1)]
    assert suggest_excludes(ROOT, files) == []
    assert suggest_excludes(ROOT, []) == []


def test_posix_paths_non_ascii_names_and_sizes():
    root = f"/home/me/{helpers.AWKWARD_FOLDER}"
    files = [(f"{root}/{helpers.CJK}/node_modules/a.js", 1000), (f"{root}/{helpers.CJK}/node_modules/b.js", 24),
             (f"{root}/{helpers.CAFE}/trash/x.txt", 5)]
    found = by_pattern(suggest_excludes(root, files))
    assert found["node_modules"].bytes == 1024 and found["node_modules"].example == f"{helpers.CJK}/node_modules"
    assert found[f"{helpers.CAFE}/trash"].group == REVIEW


def test_safe_suggestions_come_first_then_the_largest():
    files = many("node_modules/a", 5, "js") + many("Trash", 50, "txt") + many(".git/objects", 20, "")
    assert [s.pattern for s in suggest_excludes(ROOT, listing(*files))] == [".git", "node_modules", "/Trash"]


def test_the_table_of_kinds_decides_what_counts_as_readable():
    files = listing(*many("logs", 300, "nfo"))
    assert [s.pattern for s in suggest_excludes(ROOT, files)] == ["/logs"]
    assert suggest_excludes(ROOT, files, {"nfo": FileKind.TEXT}) == []


# ---- through the service and the command line ----------------------------------------------------------------

class Shell:
    def __init__(self, tmp_path):
        self.data = tmp_path / "data"
        self.docs = tmp_path / helpers.AWKWARD_FOLDER
        for relative, text in (("notes/a.txt", "alpha note"), ("proj/package.json", "{}"), ("proj/readme.md", "beta readme"),
                               ("proj/node_modules/pkg/readme.md", "gamma dependency words"),
                               ("proj/node_modules/pkg/index.js", "x"), ("proj/.env one/pyvenv.cfg", "home = x"),
                               ("proj/.env one/Lib/readme.txt", "delta environment words"),
                               ("Trash/old.txt", "epsilon thrown away")):
            path = self.docs / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def run(self, *argv):
        out = io.StringIO()
        code = cli.main([*argv, "--data-dir", str(self.data)], stdout=out, environ={})
        return code, out.getvalue()


@pytest.fixture
def sh(tmp_path):
    return Shell(tmp_path)


def test_suggest_changes_nothing_and_says_what_to_type(sh):
    code, text = sh.run("roots", "suggest", str(sh.docs))
    assert code == 0 and "Safe to skip" in text and "Your call" in text
    assert "node_modules" in text and "'proj/.env one'" in text and "Trash" in text and "downloaded requirements" in text
    assert "not indexed yet" in text and "--apply-safe" in text and "roots exclude" in text
    assert "No folders yet" in sh.run("roots", "list")[1], "suggesting must not add the folder"
    assert sh.run("roots", "suggest", str(sh.docs), "--apply-safe")[0] == 1, "nothing to apply to before the folder is added"


def test_adding_a_folder_points_at_the_suggestions(sh):
    assert "roots suggest" in sh.run("roots", "add", str(sh.docs))[1]


def test_applying_the_safe_list_takes_those_files_out_at_once_without_a_false_alarm(sh):
    sh.run("roots", "add", str(sh.docs))
    assert sh.run("index")[0] == 0
    assert "pkg" in sh.run("search", "gamma")[1] and "No files found" not in sh.run("search", "delta")[1]
    code, text = sh.run("roots", "suggest", str(sh.docs), "--apply-safe")
    assert code == 0 and "node_modules" in text and "proj/.env one" in text
    assert "4 file(s) that are now skipped left the index" in text
    assert "No files found" in sh.run("search", "gamma")[1] and "No files found" in sh.run("search", "delta")[1]
    assert "a.txt" in sh.run("search", "alpha")[1] and "old.txt" in sh.run("search", "epsilon")[1]
    code, text = sh.run("index")
    assert code == 0 and "WARNING" not in text and "gone 0" in text, "skipped by choice is not 'gone'"
    assert "missing" not in sh.run("status")[1]
    again = sh.run("roots", "suggest", str(sh.docs))[1]
    assert "Safe to skip" not in again and "/Trash" in again, "what is already skipped is not suggested again"


def test_exclude_adds_to_the_rules_and_undo_brings_files_back(sh):
    sh.run("roots", "add", str(sh.docs), "--exclude", "*.js")
    sh.run("index")
    code, text = sh.run("roots", "exclude", str(sh.docs), "Trash")
    assert code == 0 and "skipping: *.js, Trash" in text and "1 file(s) that are now skipped left the index" in text
    assert "No files found" in sh.run("search", "epsilon")[1]
    code, text = sh.run("roots", "exclude", str(sh.docs), "trash", "--undo")
    assert code == 0 and "skipping: *.js" in text and "Trash" not in text.split("skipping:")[1].splitlines()[0]
    assert "new 1" in sh.run("index")[1] and "old.txt" in sh.run("search", "epsilon")[1]
    assert sh.run("roots", "exclude", str(sh.docs / "notes"), "x")[0] == 1, "only an indexed folder has rules"


def test_a_suggestion_for_one_top_folder_does_not_skip_its_namesakes(sh):
    (sh.docs / "proj" / "Trash").mkdir()
    (sh.docs / "proj" / "Trash" / "kept.txt").write_text("zeta kept on purpose", encoding="utf-8")
    sh.run("roots", "add", str(sh.docs))
    sh.run("index")
    text = sh.run("roots", "suggest", str(sh.docs))[1]
    assert "  /Trash" in text and "  proj/Trash" in text, "each folder is its own suggestion"
    code, text = sh.run("roots", "exclude", str(sh.docs), "/Trash")
    assert code == 0 and "1 file(s) that are now skipped left the index" in text
    assert "No files found" in sh.run("search", "epsilon")[1] and "kept.txt" in sh.run("search", "zeta")[1]
    sh.run("index")
    assert "kept.txt" in sh.run("search", "zeta")[1], "the scan must read the rule the same way"
    sh.run("roots", "exclude", str(sh.docs), "Trash")
    assert "No files found" in sh.run("search", "zeta")[1], "a bare name skips every folder of that name"


def test_narrowing_with_include_also_takes_files_out(tmp_path):
    sh = Shell(tmp_path)
    data = DataLayout(sh.data)
    sh.run("roots", "add", str(sh.docs))
    sh.run("index")
    with LocalService(data) as service:
        before = service.indexing_status()["files"]
        service.set_root(Root(str(sh.docs), include=("*.txt",)))
        names = sorted(r.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for r in service.store.list_files())
        assert names == ["a.txt", "old.txt", "readme.txt"] and before == 8


def test_suggesting_for_a_folder_that_is_not_there_is_a_stated_failure(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with LocalService(DataLayout(data).ensure()) as service:
        found = service.suggest_excludes(str(tmp_path / "nowhere"))
        assert isinstance(found, Failure) and found.reason is FailureReason.UNREACHABLE


def test_patterns_are_quoted_so_they_can_be_typed():
    assert cli.quote("node_modules") == "node_modules" and cli.quote("a/b.c") == "a/b.c"
    assert cli.quote("$RECYCLE.BIN") == "'$RECYCLE.BIN'" and cli.quote("my folder") == "'my folder'"
    assert cli.quote("it's") == "'it''s'"


@pytest.mark.parametrize("count,text", [(0, "0 bytes"), (1023, "1,023 bytes"), (1024, "1.0 KB"), (5 * 1024 ** 2, "5.0 MB"),
                                        (3 * 1024 ** 3, "3.0 GB"), (5000 * 1024 ** 3, "5,000.0 GB"), (-1, "0 bytes")])
def test_sizes_read_naturally(count, text):
    assert cli.format_bytes(count) == text
