import os
import stat
from pathlib import Path

import platformdirs
import pytest

from dugup import datadir
from dugup.datadir import DataDirError, DataLayout, ensure_data_dir, is_within, resolve_data_dir
from helpers import AWKWARD_FOLDER


def _default(tmp_path):
    return lambda: tmp_path / "default-folder"


# ---- choosing the folder ---------------------------------------------------

def test_command_line_beats_environment_beats_default(tmp_path):
    env = {datadir.ENV_DATA_DIR: str(tmp_path / "from-env")}
    assert resolve_data_dir(str(tmp_path / "from-cli"), env, _default(tmp_path)) == tmp_path / "from-cli"
    assert resolve_data_dir(None, env, _default(tmp_path)) == tmp_path / "from-env"
    assert resolve_data_dir(None, {}, _default(tmp_path)) == tmp_path / "default-folder"


def test_empty_values_fall_through_to_the_next_source(tmp_path):
    env = {datadir.ENV_DATA_DIR: ""}
    assert resolve_data_dir("", env, _default(tmp_path)) == tmp_path / "default-folder"


def test_the_default_is_the_per_user_data_folder_named_for_the_app():
    expected = Path(platformdirs.user_data_dir("DugUp", appauthor=False))
    assert datadir.default_data_dir() == expected
    assert "DugUp" in str(expected)


def test_home_shortcut_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert resolve_data_dir("~/digup-data", {}, _default(tmp_path)) == tmp_path / "digup-data"


def test_relative_path_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_data_dir("rel-folder", {}, _default(tmp_path)).is_absolute()


# ---- making it ---------------------------------------------------------------

def test_creates_nested_folder_with_spaces_accents_and_japanese(tmp_path):
    target = tmp_path / AWKWARD_FOLDER / "inner folder" / "data"
    made = ensure_data_dir(target)
    assert made.is_dir()
    assert made == target


def test_creating_twice_is_harmless_and_keeps_contents(tmp_path):
    target = ensure_data_dir(tmp_path / "data")
    (target / "keep.txt").write_text("x", encoding="utf-8")
    ensure_data_dir(target)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "x"


def test_no_probe_files_are_left_behind(tmp_path):
    target = ensure_data_dir(tmp_path / "data")
    assert list(target.iterdir()) == []


@pytest.mark.linux_only
def test_folder_is_owner_only_on_posix(tmp_path):
    target = ensure_data_dir(tmp_path / "private")
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700


def test_a_file_where_the_folder_should_be_gives_a_reason(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("i am a file", encoding="utf-8")
    with pytest.raises(DataDirError) as caught:
        ensure_data_dir(blocker)
    assert "file" in caught.value.reason


@pytest.mark.linux_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores folder permissions")
def test_unwritable_parent_gives_a_reason_not_a_raw_oserror(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(DataDirError) as caught:
            ensure_data_dir(locked / "data")
        assert caught.value.reason
    finally:
        locked.chmod(0o700)


@pytest.mark.windows_only
def test_very_long_path_works_or_fails_with_a_stated_reason(tmp_path):
    deep = tmp_path
    for _ in range(12):
        deep = deep / ("x" * 30)
    try:
        ensure_data_dir(deep)
    except DataDirError as exc:
        assert exc.reason  # Windows without long-path support: a reason, never a raw OSError


# ---- places the program must not write -----------------------------------------

def test_folder_inside_a_never_write_root_is_refused(tmp_path):
    root = tmp_path / "backup-drive"
    root.mkdir()
    with pytest.raises(DataDirError, match="never write"):
        ensure_data_dir(root / "digup", never_write=[root])
    with pytest.raises(DataDirError, match="never write"):
        resolve_data_dir(str(root), {}, _default(tmp_path), never_write=[root])


def test_never_write_matches_whole_path_parts_only(tmp_path):
    (tmp_path / "dat").mkdir()
    ensure_data_dir(tmp_path / "data", never_write=[tmp_path / "dat"])  # "data" is not below "dat"


def test_never_write_cannot_be_overridden_by_allow_synced(tmp_path):
    with pytest.raises(DataDirError, match="never write"):
        ensure_data_dir(tmp_path / "x" / "y", never_write=[tmp_path / "x"], allow_synced=True)


@pytest.mark.parametrize("name", ["OneDrive", "OneDrive - Contoso", "Dropbox", "Google Drive", "My Drive", "iCloud Drive"])
def test_folders_that_look_cloud_synced_are_refused_unless_allowed(tmp_path, name):
    target = tmp_path / name / "digup"
    with pytest.raises(DataDirError, match="cloud-synced"):
        ensure_data_dir(target)
    assert not target.exists()  # refused before anything was created
    ensure_data_dir(target, allow_synced=True)
    assert target.is_dir()


def test_is_within_compares_whole_parts():
    assert is_within("/a/b", "/a/b")
    assert is_within("/a/b/c", "/a/b")
    assert not is_within("/a/bc", "/a/b")
    assert not is_within("/a", "/a/b")


@pytest.mark.windows_only
def test_windows_comparison_ignores_case_and_handles_other_drives():
    assert is_within("g:\\my drive\\digup", "G:\\My Drive")
    assert not is_within("D:\\data", "G:\\My Drive")


def test_never_write_list_can_come_from_the_environment():
    value = os.pathsep.join(["/one", "/two", ""])
    assert datadir.never_write_from_env({datadir.ENV_NEVER_WRITE: value}) == ["/one", "/two"]
    assert datadir.never_write_from_env({}) == []


# ---- layout: everything under one folder ----------------------------------------

def test_every_path_in_the_layout_is_inside_the_one_folder(tmp_path):
    layout = DataLayout(tmp_path / "root")
    paths = [layout.index, layout.config, layout.logs, layout.cache, layout.models, layout.tmp]
    assert all(is_within(p, layout.root) for p in paths)
    assert layout.index.name == "index.sqlite"


def test_layout_ensure_creates_the_working_folders(tmp_path):
    layout = DataLayout(ensure_data_dir(tmp_path / "root")).ensure()
    assert all(folder.is_dir() for folder in layout.folders())
    layout.ensure()  # repeat is harmless


def test_error_always_carries_a_reason():
    assert DataDirError("because").reason == "because"
