"""The data folder beside the program: the marker, how it is chosen, and the `data` command."""

import io
from pathlib import Path

import pytest

from dugup import cli, datadir
from dugup.datadir import (MARKER_NAME, ProgramHome, beside_program, choose_beside, find_program_home, read_marker,
                           resolve_data_dir)


def _index(folder: Path) -> Path:
    """A real, empty DugUp index in `folder`."""
    from dugup.store import SqliteStore
    folder.mkdir(parents=True, exist_ok=True)
    with SqliteStore(folder / "index.sqlite"):
        pass
    return folder


def _home(tmp_path, *skip) -> ProgramHome:
    program = tmp_path / "DugUp"
    program.mkdir(exist_ok=True)
    lines = ["# a marker", "data = data", *[f"skip = {s}" for s in skip]]
    (program / MARKER_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read_marker(program / MARKER_NAME)


@pytest.fixture
def program(tmp_path, monkeypatch):
    """A program folder with a marker, and a per-user folder of its own, standing in for the real ones."""
    home = _home(tmp_path, "python")
    per_user = tmp_path / "per-user" / "DugUp"
    monkeypatch.setattr(datadir, "find_program_home", lambda *a, **k: home)
    monkeypatch.setattr(datadir, "default_data_dir", lambda: per_user)
    return home, per_user


def run(*argv, ask=None):
    out = io.StringIO()
    if ask is not None:
        from dugup.cli import data_cmds
        args = cli.build_parser().parse_args(list(argv))
        code = data_cmds.cmd_data(args, None, out, {}, ask=ask)
    else:
        code = cli.main(list(argv), stdout=out, environ={})
    return code, out.getvalue()


# ---- the marker ---------------------------------------------------------------------------------------------

def test_a_marker_names_the_data_folder_and_the_folders_never_indexed(tmp_path):
    home = _home(tmp_path, "python", "app")
    assert home.folder == tmp_path / "DugUp"
    assert home.data == tmp_path / "DugUp" / "data"
    assert home.skip == (tmp_path / "DugUp" / "python", tmp_path / "DugUp" / "app")
    assert home.never_indexed()[0] == home.data


def test_a_marker_with_nothing_but_notes_means_a_data_folder_beside_it(tmp_path):
    (tmp_path / MARKER_NAME).write_text("# only a note\n\nunknown = ignored\n", encoding="utf-8")
    home = read_marker(tmp_path / MARKER_NAME)
    assert home.data == tmp_path / "data" and home.skip == ()


def test_the_marker_is_found_from_below_but_not_too_far(tmp_path):
    home = _home(tmp_path)
    deep = home.folder / "python" / "Lib" / "site-packages" / "dugup"
    deep.mkdir(parents=True)
    assert find_program_home(deep).folder == home.folder
    too_deep = deep / "a" / "b"
    too_deep.mkdir(parents=True)
    assert find_program_home(too_deep) is None


def test_no_marker_means_no_folder_beside_the_program(tmp_path):
    assert find_program_home(tmp_path) is None


def test_the_checkout_carries_a_marker_that_skips_none_of_the_code():
    home = read_marker(Path(__file__).resolve().parents[1] / MARKER_NAME)
    assert home.data.name == "data" and home.skip == ()


# ---- choosing -----------------------------------------------------------------------------------------------

def test_a_fresh_install_uses_the_folder_beside_the_program(tmp_path):
    home = _home(tmp_path)
    choice = choose_beside(home, tmp_path / "per-user")
    assert choice.path == home.data and choice.move_from is None


def test_an_older_index_stays_in_use_until_the_user_chooses(tmp_path):
    home = _home(tmp_path)
    per_user = _index(tmp_path / "per-user")
    choice = choose_beside(home, per_user)
    assert choice.path == per_user and choice.move_from == per_user


def test_an_index_beside_the_program_wins_over_an_older_one(tmp_path):
    home = _home(tmp_path)
    _index(home.data)
    choice = choose_beside(home, _index(tmp_path / "per-user"))
    assert choice.path == home.data and choice.move_from is None


def test_keeping_the_older_folder_is_remembered(tmp_path):
    from dugup.datamove import keep_where_it_is
    home = _home(tmp_path)
    per_user = _index(tmp_path / "per-user")
    keep_where_it_is(home.data, per_user)
    choice = choose_beside(home, per_user)
    assert choice.path == per_user and choice.move_from is None


def test_a_kept_folder_that_is_gone_falls_back_to_beside_the_program(tmp_path):
    from dugup.datamove import keep_where_it_is
    home = _home(tmp_path)
    keep_where_it_is(home.data, tmp_path / "vanished")
    assert choose_beside(home, tmp_path / "per-user").path == home.data


def test_the_command_line_and_environment_still_come_first(tmp_path, program):
    home, _ = program
    env = {datadir.ENV_DATA_DIR: str(tmp_path / "from-env")}
    assert resolve_data_dir(str(tmp_path / "from-cli"), env) == tmp_path / "from-cli"
    assert resolve_data_dir(None, env) == tmp_path / "from-env"
    assert resolve_data_dir(None, {}) == home.data
    assert beside_program(str(tmp_path / "x"), {}) is None


def test_the_synced_folder_guard_still_applies_beside_the_program(tmp_path, monkeypatch):
    synced = tmp_path / "OneDrive"
    synced.mkdir()
    home = _home(synced)
    monkeypatch.setattr(datadir, "find_program_home", lambda *a, **k: home)
    with pytest.raises(datadir.DataDirError):
        resolve_data_dir(None, {})


# ---- the data command ---------------------------------------------------------------------------------------

def test_where_says_which_folder_and_why(program):
    home, per_user = program
    code, text = run("data", "where")
    assert code == 0 and str(home.data) in text and "Beside the program" in text
    _index(per_user)
    code, text = run("data", "where")
    assert str(per_user) in text and "dugup data move" in text


def test_every_command_mentions_a_waiting_index_until_it_is_settled(program):
    home, per_user = program
    _index(per_user)
    code, text = run("status")
    assert "DugUp found your index in" in text and str(home.data) in text
    run("data", "keep")
    code, text = run("status")
    assert "DugUp found your index in" not in text


def test_keep_records_the_choice_and_stops_asking(program):
    home, per_user = program
    _index(per_user)
    code, text = run("data", "keep")
    assert code == 0 and "will not ask again" in text
    assert resolve_data_dir(None, {}) == per_user
    code, text = run("data", "keep")
    assert "Already chosen" in text


def test_move_asks_first_and_does_nothing_on_no(program):
    home, per_user = program
    _index(per_user)
    code, text = run("data", "move", ask=lambda q: "n")
    assert code == 0 and "Nothing was moved" in text
    assert (per_user / "index.sqlite").exists() and not home.data.exists()


def test_move_brings_the_index_beside_the_program(program):
    home, per_user = program
    _index(per_user)
    (per_user / "config.toml").write_text("x = 1\n", encoding="utf-8")
    code, text = run("data", "move", "--yes")
    assert code == 0, text
    assert (home.data / "index.sqlite").exists() and (home.data / "config.toml").exists()
    assert not per_user.exists()
    assert resolve_data_dir(None, {}) == home.data


def test_move_after_keeping_still_works(program):
    home, per_user = program
    _index(per_user)
    run("data", "keep")
    code, text = run("data", "move", "--yes")
    assert code == 0, text
    assert (home.data / "index.sqlite").exists() and not (home.data / datadir.KEPT_ELSEWHERE).exists()


def test_move_with_nothing_to_move_says_so(program):
    code, text = run("data", "move", "--yes")
    assert code == 0 and "Nothing to move" in text


# ---- never indexing the program -----------------------------------------------------------------------------

def test_the_program_folders_are_never_indexed_but_the_rest_is(tmp_path, monkeypatch):
    root = tmp_path / "everything"
    program = root / "DugUp"
    (program / "python").mkdir(parents=True)
    (program / MARKER_NAME).write_text("data = data\nskip = python\n", encoding="utf-8")
    (program / "python" / "library.txt").write_text("zebrafish inside the bundled runtime", encoding="utf-8")
    (root / "notes.txt").write_text("zebrafish in the user's own notes", encoding="utf-8")
    home = read_marker(program / MARKER_NAME)
    monkeypatch.setattr(datadir, "find_program_home", lambda *a, **k: home)
    data = str(tmp_path / "data")
    out = io.StringIO()
    assert cli.main(["roots", "add", str(root), "--data-dir", data], stdout=out, environ={}) == 0, out.getvalue()
    assert cli.main(["index", "--no-ocr", "--no-meaning", "--data-dir", data], stdout=out, environ={}) == 0, out.getvalue()
    out = io.StringIO()
    cli.main(["search", "zebrafish", "--data-dir", data], stdout=out, environ={})
    assert "notes.txt" in out.getvalue() and "library.txt" not in out.getvalue()


def test_a_folder_inside_the_program_cannot_be_added(tmp_path, monkeypatch):
    program = tmp_path / "DugUp"
    (program / "python").mkdir(parents=True)
    (program / MARKER_NAME).write_text("skip = python\n", encoding="utf-8")
    home = read_marker(program / MARKER_NAME)
    monkeypatch.setattr(datadir, "find_program_home", lambda *a, **k: home)
    out = io.StringIO()
    code = cli.main(["roots", "add", str(program / "python"), "--data-dir", str(tmp_path / "data")], stdout=out, environ={})
    assert code != 0 and "program folder" in out.getvalue()
