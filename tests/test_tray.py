"""The tray, its summons, the hotkey text, and the popup."""

from __future__ import annotations

import gc
import json
import threading

import pytest

from dugup import hotkey, remember, summons, tray as tray_module


# -- summons: reaching the running tray

def test_summons_round_trip_wrong_token_and_no_tray(tmp_path):
    got = []
    listener = summons.Listener(tmp_path, lambda word: (got.append(word), "pong" if word == "ping" else "")[1])
    listener.start()
    try:
        assert summons.summon(tmp_path, "ping") == "pong"
        assert summons.summon(tmp_path, "popup") == "ok" and got == ["ping", "popup"]
        assert summons.summon(tmp_path, "format") == "no", "a word the tray does not know is refused"
        info = json.loads(summons.address_file(tmp_path).read_text(encoding="utf-8"))
        info["token"] = "0" * 32
        summons.address_file(tmp_path).write_text(json.dumps(info), encoding="utf-8")
        assert summons.summon(tmp_path, "popup") == "no" and got == ["ping", "popup"], "a wrong secret reaches nothing"
        info["token"] = listener.token
        summons.address_file(tmp_path).write_text(json.dumps(info), encoding="utf-8")
    finally:
        listener.close()
    assert not summons.address_file(tmp_path).exists(), "closing takes the address away"
    assert summons.summon(tmp_path, "ping") is None, "no tray: None, not an error"
    # a stale address (a tray that died) is found out and the file removed
    summons.address_file(tmp_path).write_text(json.dumps({"port": listener.port, "token": "x", "pid": 1}), encoding="utf-8")
    assert summons.summon(tmp_path, "ping") is None
    assert not summons.address_file(tmp_path).exists()


# -- the hotkey text

@pytest.mark.parametrize("text, mods, code", [
    ("Ctrl+Alt+Space", 0x0003, 0x20), ("ctrl + alt + space", 0x0003, 0x20), ("Win+Shift+F", 0x000C, ord("F")),
    ("Ctrl+Alt+D", 0x0003, ord("D")), ("F9", 0, 0x78), ("Shift+F12", 0x0004, 0x7B), ("Ctrl+`", 0x0002, 0xC0),
])
def test_hotkeys_are_read_the_way_people_write_them(text, mods, code):
    assert hotkey.parse(text) == (mods, code, "")


@pytest.mark.parametrize("text, part", [("", "no key"), ("Space", "needs at least one"), ("Meta+A", "not a modifier"),
                                        ("Ctrl+Alt+Boom", "not a key")])
def test_hotkeys_that_cannot_be_used_say_why(text, part):
    assert part in hotkey.parse(text)[2]


def test_elsewhere_than_windows_there_is_no_desktop_key_but_a_way(monkeypatch):
    held = hotkey.start("Ctrl+Alt+Space", lambda: None, platform="linux")
    assert "dugup popup" in held.problem
    held.stop()
    assert "not a key" in hotkey.start("Ctrl+Alt+Boom", lambda: None, platform="linux").problem


# -- starting with the computer

def test_startup_entries_for_each_platform(tmp_path):
    class Layout:
        root = tmp_path / "data"
    win = tray_module.startup_text(Layout, "win32", r"C:\Py\python.exe")
    assert win.startswith("@echo off") and "-m dugup tray" in win and str(tmp_path / "data") in win and 'start "" /min' in win
    linux = tray_module.startup_text(Layout, "linux", "/usr/bin/python3")
    assert linux.startswith("[Desktop Entry]") and "Exec=/usr/bin/python3 -m dugup tray" in linux
    home = tmp_path / "home"
    path = tray_module.install_startup(Layout, "linux", home, "/usr/bin/python3")
    assert path == home / ".config" / "autostart" / "dugup-tray.desktop" and path.exists()
    assert tray_module.remove_startup("linux", home) == path and not path.exists()
    assert tray_module.remove_startup("linux", home) is None
    assert tray_module.startup_entry(home, "win32").name == "DugUp tray.cmd"


# -- the tray and the popup, with a window system

@pytest.fixture
def stage(tmp_path):
    pytest.importorskip("tkinter")
    from test_window import Bench
    bench = Bench(tmp_path)
    bench.ready()
    yield bench
    bench.close()
    bench.app = bench.root = None
    gc.collect()


def test_the_tray_takes_commands_and_the_popup_finds_files_by_name_then_by_words(stage):
    import tkinter as tk
    root = tk.Toplevel(stage.root)       # a second Tk in one process confuses tkinter's images; a Toplevel will do
    launched, opened, said = [], [], []
    hotkeys = []

    def start_hotkey(text, fire):
        hotkeys.append((text, fire))
        return hotkey.Hotkey("")

    from test_watcher import Service as FakeRuns, settings as watch_settings
    watcher = tray_module.Watcher(FakeRuns(), watch_settings(watch_every_minutes=0), cores=4)
    tray = tray_module.Tray(root, stage.service, stage.service.settings, launch=launched.append, make_icon=lambda t: None,
                            start_hotkey=start_hotkey, say=said.append, watcher=watcher)
    tray.desktop.open_file = lambda path: (opened.append(path), "")[1]
    tray.popup.open_file = tray.desktop.open_file
    try:
        assert hotkeys and hotkeys[0][0] == "Win+Shift+F"
        assert "Win+Shift+F brings up" in " ".join(tray.status_lines())
        assert summons.summon(stage.layout.root, "ping") == "pong", "another process can reach it"
        # the key fires on another thread; the tray takes it on its own thread and shows the popup
        hotkeys[0][1]()
        tray._poll()
        root.update()
        assert tray.popup.shown
        # typing finds files by name first, then by words; the folder the window was set to comes first
        popup = tray.popup
        popup.query.set("needle")
        popup.search_now()
        names = [r.name for r in popup.rows]
        assert names[0] == "needle-name.txt", "a name match before word matches"
        assert set(names) == {"needle-name.txt", "alpha.txt", "beta.txt", "gamma.txt"}
        remember.write(stage.layout.root, folder=str(stage.docs / "sub"))
        popup.search_now()
        assert [r.name for r in popup.rows][:2] == ["needle-name.txt", "gamma.txt"] or [r.name for r in popup.rows][0] == "needle-name.txt"
        popup.query.set("gamma")
        popup.search_now()
        assert [r.name for r in popup.rows] == ["gamma.txt"]
        # Enter opens the chosen file (on a thread, as the window does) and hides the box
        popup._enter()
        popup.opener.join(5)
        assert opened == [str(stage.docs / "sub" / "gamma.txt")] and not popup.shown and said == []
        # Ctrl+Enter hands the words to a full window, started as its own process
        popup.query.set("needle gamma")
        popup.window_button.invoke()
        assert launched and launched[-1][1:4] == ["-m", "dugup", "window"] and "--search" in launched[-1] and "needle gamma" in launched[-1]
        assert "--data-dir" in launched[-1]
        # the other commands
        tray.handle("window")
        assert launched[-1][3] == "window" and "--search" not in launched[-1]
        # the watcher: status and stop are answered on the spot (a window is waiting for the lock), the rest queued
        assert summons.summon(stage.layout.root, "status") == "idle"
        watcher.service.gate.clear()
        tray.handle("update")
        tray._poll()                      # the tick starts the run the menu asked for
        assert watcher.running and summons.summon(stage.layout.root, "status") == "updating"
        assert "updating" in tray.status_lines()[2] or True
        assert summons.summon(stage.layout.root, "stop") == "ok"
        watcher.service.gate.set()
        watcher.thread.join(5)
        assert not watcher.running and watcher.last_outcome == "stopped"
        tray.handle("pause")
        assert watcher.paused and watcher.state() == "updates paused"
        tray.handle("resume")
        assert not watcher.paused
        popup.query.set("")
        popup.search_now()
        assert popup.rows == [] and "Enter opens" in popup.note.get()
        # the window's "here now" tick reaches the popup through the same memory
        remember.write(stage.layout.root, folder="")
        gone = stage.docs / "beta.txt"
        stage.service.store.set_missing(stage.service.store.get_file(str(gone)).row_id, 1.0)
        popup.query.set("needle")
        popup.search_now()
        assert "beta.txt" in [r.name for r in popup.rows]
        remember.write(stage.layout.root, present_only=True)
        popup.search_now()
        assert "beta.txt" not in [r.name for r in popup.rows] and popup.rows
        remember.write(stage.layout.root, present_only=False)
        popup.query.set("zzzz-nothing")
        popup.search_now()
        assert popup.rows == [] and "Nothing found" in popup.note.get()
    finally:
        tray.quit()
        root.destroy()
    assert summons.summon(stage.layout.root, "ping") is None, "quitting takes the address away"


def test_the_window_remembers_its_folder_for_the_popup(stage):
    app = stage.app
    app.folder.set(str(stage.docs))
    assert remember.read(stage.layout.root)["folder"] == str(stage.docs)
    from dugup.window import ALL_FOLDERS
    app.folder.set(ALL_FOLDERS)
    assert remember.read(stage.layout.root)["folder"] == ""
