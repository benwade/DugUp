"""Synced folders as the sync apps record them, and what DugUp does about them."""

import io
import json
import sqlite3
import types
from pathlib import Path

import pytest

from dugup import cli, datadir, datamove, syncfolders
from dugup.datadir import MARKER_NAME, DataDirError, check_allowed, choose_beside, read_marker
from dugup.syncfolders import SyncedFolder, Survey
from dugup.syncfolders import survey as real_survey      # bound before the test fixture stands in for it


def _drivefs(local: Path, roots=(), drives=(), tables=("roots", "media")) -> Path:
    """Google Drive for desktop's settings database, laid out as on a real machine (2026-10)."""
    folder = local / "Google" / "DriveFS"
    folder.mkdir(parents=True)
    conn = sqlite3.connect(str(folder / "root_preference_sqlite.db"))
    if "roots" in tables:
        conn.execute("CREATE TABLE roots (root_id, metadata, media_id, title, root_path, account_token, sync_type, "
                     "destination, medium, state, one_shot, is_my_drive, doc_id, last_seen_absolute_path)")
        for i, path in enumerate(roots):
            conn.execute("INSERT INTO roots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (i, b"", "m", Path(path).name, "", "acct", 1, 1, 1, 2, 0, 0, "", str(path)))
        conn.execute("INSERT INTO roots VALUES (99,'','phone','Phone','', 'acct',2,1,2,2,0,0,'','')")  # no path
    if "media" in tables:
        conn.execute("CREATE TABLE media (media_id, name, last_mount_point, fs_type, device_type, capacity, ignored)")
        for mount in drives:
            conn.execute("INSERT INTO media VALUES ('x','Google Drive',?,2,2,1,0)", (str(mount),))
        conn.execute("INSERT INTO media VALUES ('y','Kindle','F:\\\\',2,7,1,1)")
    conn.commit()
    conn.close()
    return folder


@pytest.fixture
def synced(monkeypatch, tmp_path):
    """Say that tmp_path/Backed is synced by Google Drive for desktop, as the real survey would."""
    found = Survey([SyncedFolder(tmp_path / "Backed", "Google Drive for desktop")])
    monkeypatch.setattr(syncfolders, "survey", lambda *a, **k: found)
    return found


# ---- reading the sync apps' records ------------------------------------------------------------------------

def test_google_drive_lists_the_folders_it_backs_up_and_its_own_drive(tmp_path):
    _drivefs(tmp_path, roots=[tmp_path / "Projects", tmp_path / "Pictures"], drives=[tmp_path / "G"])
    folders, unread = syncfolders.google_drive(tmp_path)
    assert unread == []
    assert {f.path for f in folders} == {tmp_path / "Projects", tmp_path / "Pictures", tmp_path / "G"}
    assert all(f.by == "Google Drive for desktop" for f in folders)


def test_google_drive_with_a_renamed_table_still_reads_the_rest(tmp_path):
    _drivefs(tmp_path, roots=[tmp_path / "Projects"], tables=("roots",))
    folders, unread = syncfolders.google_drive(tmp_path)
    assert [f.path for f in folders] == [tmp_path / "Projects"] and unread == []


def test_google_drive_present_but_unreadable_is_said_not_hidden(tmp_path):
    folder = tmp_path / "Google" / "DriveFS"
    folder.mkdir(parents=True)
    (folder / "root_preference_sqlite.db").write_bytes(b"not a database at all")
    assert syncfolders.google_drive(tmp_path) == ([], ["Google Drive for desktop"])
    (folder / "root_preference_sqlite.db").unlink()
    assert syncfolders.google_drive(tmp_path) == ([], ["Google Drive for desktop"])


def test_no_google_drive_is_simply_nothing(tmp_path):
    assert syncfolders.google_drive(tmp_path) == ([], [])


def test_dropbox_reads_each_account_and_says_when_it_cannot(tmp_path):
    info = tmp_path / "info.json"
    info.write_text(json.dumps({"personal": {"path": str(tmp_path / "Dropbox")},
                                "business": {"path": str(tmp_path / "Dropbox (Work)")}}), encoding="utf-8")
    folders, unread = syncfolders.dropbox([info])
    assert {f.path for f in folders} == {tmp_path / "Dropbox", tmp_path / "Dropbox (Work)"} and unread == []
    info.write_text("{ broken", encoding="utf-8")
    assert syncfolders.dropbox([info]) == ([], ["Dropbox"])


def test_a_source_that_breaks_is_reported_and_the_others_still_count(tmp_path):
    def broken():
        raise RuntimeError("registry gone")

    good = lambda: ([SyncedFolder(tmp_path / "OneDrive", "OneDrive")], [])
    found = real_survey([broken, good])
    assert [f.by for f in found.folders] == ["OneDrive"] and found.unread == ["broken"]


def test_the_deepest_synced_folder_names_the_app(tmp_path):
    found = Survey([SyncedFolder(tmp_path, "Google Drive for desktop"),
                    SyncedFolder(tmp_path / "OneDrive", "OneDrive")])
    assert found.synced_by(tmp_path / "OneDrive" / "Documents").by == "OneDrive"
    assert found.synced_by(tmp_path / "elsewhere").by == "Google Drive for desktop"
    assert Survey([SyncedFolder(tmp_path / "a", "x")]).synced_by(tmp_path / "ab") is None, "a name prefix is not inside"


# ---- what DugUp does about them ------------------------------------------------------------------------------

def test_a_synced_folder_is_refused_with_the_app_and_folder_named(tmp_path, synced):
    with pytest.raises(DataDirError) as caught:
        check_allowed(tmp_path / "Backed" / "projects" / "data")
    assert "Google Drive for desktop" in caught.value.reason and str(tmp_path / "Backed") in caught.value.reason
    check_allowed(tmp_path / "Backed" / "data", allow_synced=True)        # the person's explicit choice stands
    check_allowed(tmp_path / "Local" / "data")


def test_beside_a_program_in_a_synced_folder_the_index_stays_per_user(tmp_path, synced):
    program = tmp_path / "Backed" / "projects" / "app"
    program.mkdir(parents=True)
    (program / MARKER_NAME).write_text("data = data\n", encoding="utf-8")
    home = read_marker(program / MARKER_NAME)
    per_user = tmp_path / "per-user"
    (per_user).mkdir()
    (per_user / "index.sqlite").write_bytes(b"")
    choice = choose_beside(home, per_user)
    assert choice.path == per_user and choice.move_from is None, "no offer to move the index into a synced folder"
    assert choice.synced.by == "Google Drive for desktop"


def test_data_where_explains_and_warns(tmp_path, monkeypatch):
    program = tmp_path / "Backed" / "app"
    program.mkdir(parents=True)
    (program / MARKER_NAME).write_text("data = data\n", encoding="utf-8")
    home = read_marker(program / MARKER_NAME)
    monkeypatch.setattr(datadir, "find_program_home", lambda *a, **k: home)
    monkeypatch.setattr(datadir, "default_data_dir", lambda: tmp_path / "per-user")
    found = Survey([SyncedFolder(tmp_path / "Backed", "Google Drive for desktop")], unread=["Dropbox"])
    monkeypatch.setattr(syncfolders, "survey", lambda *a, **k: found)
    out = io.StringIO()
    assert cli.main(["data", "where"], stdout=out, environ={}) == 0
    text = out.getvalue()
    assert "syncs to the cloud, so the index stays out of it" in text
    assert "could not read which folders Dropbox syncs" in text, "warned, and nothing refused"


def test_a_move_into_a_synced_folder_is_refused_and_changes_nothing(tmp_path, synced):
    old = tmp_path / "per-user"
    old.mkdir()
    (old / "config.toml").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(datamove.MoveError) as caught:
        datamove.move(old, tmp_path / "Backed" / "data", is_tray_running=lambda _: False)
    assert "Google Drive for desktop" in caught.value.reason and (old / "config.toml").exists()
    assert not (tmp_path / "Backed").exists()


def test_doctor_says_what_the_sync_apps_report():
    from dugup.doctor import INFO, OK, WARN, Environment, run_checks
    yes = lambda name: True
    def line(found):
        return {c.name: c for c in run_checks(Environment(can_import=yes, ocr_card=lambda: "", directml=lambda: False,
                                                          sync=lambda: found))}["cloud sync"]
    assert line(Survey([SyncedFolder(Path("D:/Projects"), "Google Drive for desktop")])).status == OK
    assert line(Survey(unread=["Google Drive for desktop"])).status == WARN
    assert line(Survey()).status == INFO


# ---- a leftover from an earlier try never blocks a move --------------------------------------------------------

def test_a_stuck_leftover_is_stepped_around_and_mentioned(tmp_path, monkeypatch):
    old = tmp_path / "per-user"
    old.mkdir()
    (old / "config.toml").write_text("x = 1\n", encoding="utf-8")
    new = tmp_path / "app" / "data"
    stuck = tmp_path / "app" / "data.moving"
    (stuck / "models" / "captions").mkdir(parents=True)
    real = datamove.shutil.rmtree

    def holds_the_leftover(path, *args, **kwargs):       # as when a sync app holds a folder: it will not go
        if Path(path) == stuck:
            return None
        return real(path, *args, **kwargs)

    monkeypatch.setattr(datamove.shutil, "rmtree", holds_the_leftover)
    done = datamove.move(old, new, is_tray_running=lambda _: False, startup_entry=tmp_path / "none")
    assert (new / "config.toml").exists() and not (tmp_path / "app" / "data.moving-2").exists()
    assert any(str(stuck) in note for note in done.notes)


def test_a_failed_move_says_what_it_left_and_where(tmp_path, monkeypatch):
    old = tmp_path / "per-user"
    old.mkdir()
    (old / "config.toml").write_text("x = 1\n", encoding="utf-8")
    new = tmp_path / "app" / "data"
    monkeypatch.setattr(datamove.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")))
    monkeypatch.setattr(datamove.shutil, "rmtree", lambda *a, **k: None)      # and the leftover will not go either
    with pytest.raises(datamove.MoveError) as caught:
        datamove.move(old, new, is_tray_running=lambda _: False)
    reason = caught.value.reason
    assert "nothing was moved" in reason and str(old) in reason and "data.moving" in reason and "deleted later" in reason


# ---- the tray asks too -----------------------------------------------------------------------------------------

def test_the_tray_asks_when_it_would_open_the_older_index(tmp_path, monkeypatch):
    from dugup.cli.desktop_cmds import move_waits_for
    program = tmp_path / "app"
    program.mkdir()
    (program / MARKER_NAME).write_text("data = data\n", encoding="utf-8")
    home = read_marker(program / MARKER_NAME)
    per_user = tmp_path / "per-user"
    per_user.mkdir()
    (per_user / "index.sqlite").write_bytes(b"")
    monkeypatch.setattr(datadir, "find_program_home", lambda *a, **k: home)
    monkeypatch.setattr(datadir, "default_data_dir", lambda: per_user)
    assert move_waits_for(types.SimpleNamespace(data_dir=None), {})
    assert move_waits_for(types.SimpleNamespace(data_dir=str(per_user)), {}), "as its login entry starts it"
    assert not move_waits_for(types.SimpleNamespace(data_dir=str(tmp_path / "other")), {})


def test_keeping_the_index_out_of_a_synced_install_is_said_once(tmp_path, synced, monkeypatch):
    """Not a silent surprise: the first time, a note says where the index went and how to choose another folder."""
    import types
    from dugup import datadir, startnotes
    program = tmp_path / "Backed" / "app"
    program.mkdir(parents=True)
    (program / MARKER_NAME).write_text("data = data\n", encoding="utf-8")
    per_user = tmp_path / "per-user"
    monkeypatch.setattr(datadir, "find_program_home", lambda *a, **k: read_marker(program / MARKER_NAME))
    monkeypatch.setattr(datadir, "default_data_dir", lambda: per_user)
    told = []
    tell = lambda parent, title, text: told.append(text)
    args = types.SimpleNamespace(data_dir=None)
    assert startnotes.before_window(None, args, {}, tell=tell, is_elevated=lambda: False) is None
    assert len(told) == 1 and str(per_user) in told[0] and "Google Drive for desktop" in told[0]
    assert "Where DugUp keeps its index" in told[0] and syncfolders.CAVEAT in told[0]
    startnotes.before_window(None, args, {}, tell=tell, is_elevated=lambda: False)
    assert len(told) == 1, "said once, not every time DugUp opens"


def test_a_data_folder_in_the_survey_still_comes_with_the_caveat(tmp_path, synced):
    with pytest.raises(DataDirError) as caught:
        check_allowed(tmp_path / "Backed" / "data")
    assert "not a guarantee" in caught.value.reason


def test_a_marker_may_name_a_full_path_for_the_data(tmp_path):
    """The installer writes the folder chosen for the index as a full path when it is not the usual one."""
    elsewhere = tmp_path / "Index here"
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / MARKER_NAME).write_text(f"﻿# note\ndata = {elsewhere}\nskip = python\n", encoding="utf-8")
    home = read_marker(tmp_path / "app" / MARKER_NAME)
    assert home.data == elsewhere and home.skip == (tmp_path / "app" / "python",)
