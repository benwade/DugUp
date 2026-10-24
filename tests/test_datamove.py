"""Moving a data folder: every way it can fail leaves the old folder whole and in use."""

import shutil
import sqlite3
from pathlib import Path

import pytest

from dugup import datamove
from dugup.datadir import KEPT_ELSEWHERE
from dugup.datamove import MoveError, keep_where_it_is, move
from dugup.lockfile import RunLock


def _old(tmp_path: Path, rows: int = 50) -> Path:
    """A data folder with an index (rows still in its write-ahead log), settings, a record, and the files a
    running DugUp leaves that must not travel."""
    old = tmp_path / "per-user" / "DugUp"
    (old / "removed").mkdir(parents=True)
    (old / "tmp").mkdir()
    conn = sqlite3.connect(str(old / "index.sqlite"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()
    (old / "config.toml").write_text("max_index_mb = 2000\n", encoding="utf-8")
    (old / "removed" / "trashed 2026.txt").write_text("moving\ta\tkept\tb\n", encoding="utf-8")
    (old / "tray.json").write_text("{}", encoding="utf-8")
    (old / "tmp" / "scratch.bin").write_bytes(b"x" * 10)
    return old


def _rows(folder: Path) -> int:
    conn = sqlite3.connect(str(folder / "index.sqlite"))
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def _untouched(old: Path, new: Path, rows: int = 50) -> None:
    assert _rows(old) == rows
    assert (old / "config.toml").exists() and (old / "removed" / "trashed 2026.txt").exists()
    assert not new.with_name(new.name + ".moving").exists()
    assert not (new / "index.sqlite").exists()


def no_tray(_):
    return False


def test_a_move_carries_the_data_and_leaves_the_running_files(tmp_path):
    old, new = _old(tmp_path), tmp_path / "DugUp" / "data"
    seen = []
    done = move(old, new, tell=lambda d, t: seen.append((d, t)), is_tray_running=no_tray,
                startup_entry=tmp_path / "none")
    assert _rows(new) == 50
    assert (new / "config.toml").read_text(encoding="utf-8") == "max_index_mb = 2000\n"
    assert (new / "removed" / "trashed 2026.txt").exists()
    assert not (new / "tray.json").exists() and not (new / "tmp").exists() and not (new / "index.lock").exists()
    assert not old.exists() and done.left_behind == ""
    assert seen and seen[-1][0] == seen[-1][1]


def test_rows_still_in_the_write_ahead_log_travel_too(tmp_path):
    old, new = _old(tmp_path), tmp_path / "data"
    reader = sqlite3.connect(str(old / "index.sqlite"))         # another connection keeps the log from folding in
    reader.execute("INSERT INTO t VALUES (999)")
    reader.commit()
    try:
        move(old, new, is_tray_running=no_tray, startup_entry=tmp_path / "none")
    finally:
        reader.close()
    assert _rows(new) == 51


def test_a_running_tray_stops_the_move(tmp_path):
    old, new = _old(tmp_path), tmp_path / "data"
    with pytest.raises(MoveError) as caught:
        move(old, new, is_tray_running=lambda _: True)
    assert "tray" in caught.value.reason
    _untouched(old, new)


def test_an_indexing_run_stops_the_move(tmp_path):
    old, new = _old(tmp_path), tmp_path / "data"
    with RunLock(old / "index.lock") as running:
        assert running.acquire()
        with pytest.raises(MoveError) as caught:
            move(old, new, is_tray_running=no_tray)
    assert "indexing" in caught.value.reason
    _untouched(old, new)


def test_a_destination_with_other_files_is_never_mixed_in(tmp_path):
    old, new = _old(tmp_path), tmp_path / "data"
    new.mkdir()
    (new / "someone else's.txt").write_text("x", encoding="utf-8")
    with pytest.raises(MoveError) as caught:
        move(old, new, is_tray_running=no_tray)
    assert "will not mix" in caught.value.reason
    _untouched(old, new)


def test_stopping_part_way_changes_nothing(tmp_path):
    old, new = _old(tmp_path), tmp_path / "data"
    asked = []

    def stop():
        asked.append(1)
        return len(asked) > 1

    with pytest.raises(MoveError):
        move(old, new, stop=stop, is_tray_running=no_tray)
    _untouched(old, new)
    assert RunLock(old / "index.lock").acquire()            # and the lock was let go


def test_ctrl_c_part_way_changes_nothing(tmp_path):
    old, new = _old(tmp_path), tmp_path / "data"

    def interrupted(done, total):
        if done:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        move(old, new, tell=interrupted, is_tray_running=no_tray)
    _untouched(old, new)
    assert RunLock(old / "index.lock").acquire()


def test_a_copy_that_comes_out_short_is_caught_before_anything_changes(tmp_path, monkeypatch):
    old, new = _old(tmp_path), tmp_path / "data"

    def short_copy(source, target):
        Path(target).write_bytes(Path(source).read_bytes()[:-1])

    monkeypatch.setattr(datamove.shutil, "copy2", short_copy)
    with pytest.raises(MoveError) as caught:
        move(old, new, is_tray_running=no_tray)
    assert "whole" in caught.value.reason
    _untouched(old, new)


def test_an_old_folder_that_cannot_be_removed_is_reported_and_the_data_is_safe(tmp_path, monkeypatch):
    old, new = _old(tmp_path), tmp_path / "data"
    real = shutil.rmtree

    def stubborn(path, *args, **kwargs):
        if Path(path) == old:
            raise PermissionError(13, "in use")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(datamove.shutil, "rmtree", stubborn)
    done = move(old, new, is_tray_running=no_tray, startup_entry=tmp_path / "none")
    assert _rows(new) == 50 and old.exists()
    assert "safe" in done.left_behind and str(new) in done.left_behind


def test_the_start_at_login_entry_follows_the_data(tmp_path):
    old, new = _old(tmp_path), tmp_path / "data"
    entry = tmp_path / "DugUp tray.cmd"
    entry.write_text(f'@echo off\r\nstart "" /min "pythonw.exe" -m dugup tray --data-dir "{old}"\r\n', encoding="utf-8")
    other = tmp_path / "other.cmd"
    other.write_text('start "" pythonw.exe -m dugup tray --data-dir "D:\\elsewhere"\r\n', encoding="utf-8")
    done = move(old, new, is_tray_running=no_tray, startup_entry=entry)
    assert done.startup_updated and f'--data-dir "{new}"' in entry.read_text(encoding="utf-8")
    assert not datamove.update_startup(Path("D:/x"), new, other)


def test_a_start_at_login_shortcut_is_made_again_with_the_new_folder(tmp_path, monkeypatch):
    from dugup import tray
    old, new = Path("D:/old data"), Path("E:/new data")
    entry = tmp_path / "DugUp tray.lnk"
    entry.write_bytes(b"shortcut")
    made = []

    class Fake:
        def arguments(self, path):
            return f'-m dugup tray --data-dir "{old}"'

        def make(self, path, target, arguments):
            made.append(arguments)
            return True

    monkeypatch.setattr(tray, "shortcuts", Fake())
    assert datamove.update_startup(old, new, entry) and made == [f'-m dugup tray --data-dir "{new}"']
    assert not datamove.update_startup(Path("D:/elsewhere"), new, entry), "a shortcut for another folder is left alone"


def test_a_kept_choice_beside_the_program_does_not_block_a_later_move(tmp_path):
    old, new = _old(tmp_path), tmp_path / "data"
    keep_where_it_is(new, old)
    move(old, new, is_tray_running=no_tray, startup_entry=tmp_path / "none")
    assert _rows(new) == 50 and not (new / KEPT_ELSEWHERE).exists()


def test_a_cut_off_earlier_move_is_cleared_away_first(tmp_path):
    old, new = _old(tmp_path), tmp_path / "data"
    leftover = tmp_path / "data.moving"
    leftover.mkdir()
    (leftover / "half.sqlite").write_bytes(b"partial")
    move(old, new, is_tray_running=no_tray, startup_entry=tmp_path / "none")
    assert not leftover.exists() and not (new / "half.sqlite").exists() and _rows(new) == 50
