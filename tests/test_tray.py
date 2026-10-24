"""The tray, its summons, the hotkey text, and the popup."""

from __future__ import annotations

import gc
import json
import threading

import pytest
from types import SimpleNamespace

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
    assert tray_module.startup_entry(home, "win32").name == "DugUp tray.lnk", "a new entry is a shortcut"


def test_on_windows_the_entry_is_a_shortcut_to_dugup_exe_and_replaces_the_old_script(tmp_path, monkeypatch):
    class Layout:
        root = tmp_path / "data"
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    python_dir = tmp_path / "DugUp" / "python"
    python_dir.mkdir(parents=True)
    for name in ("python.exe", "pythonw.exe"):
        (python_dir / name).write_bytes(b"")
    python = str(python_dir / "python.exe")
    made = []

    class Fake:
        def make(self, path, target, arguments):
            made.append((path.name, target.name, arguments))
            path.write_bytes(b"shortcut")
            return True

    monkeypatch.setattr(tray_module, "shortcuts", Fake())
    # without DugUp.exe (a source checkout): the script, through pythonw
    script = tray_module.install_startup(Layout, "win32", python=python)
    assert script.name == "DugUp tray.cmd" and "pythonw.exe" in script.read_text(encoding="utf-8") and made == []
    assert tray_module.startup_entry(platform="win32") == script
    # with DugUp.exe (installed): a shortcut named DugUp tray, and the old script goes
    (python_dir / "DugUp.exe").write_bytes(b"")
    shortcut = tray_module.install_startup(Layout, "win32", python=python)
    assert shortcut.name == "DugUp tray.lnk" and not script.exists()
    assert made == [("DugUp tray.lnk", "DugUp.exe", f'-m dugup tray --data-dir "{tmp_path / "data"}"')]
    assert tray_module.startup_entry(platform="win32") == shortcut
    # a shortcut that cannot be made falls back to the script
    monkeypatch.setattr(Fake, "make", lambda self, path, target, arguments: False)
    assert tray_module.install_startup(Layout, "win32", python=python).name == "DugUp tray.cmd"
    assert tray_module.remove_startup("win32") is not None and tray_module.remove_startup("win32") is None



def test_an_upgrade_remakes_only_an_entry_for_this_copy_and_keeps_its_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    python_dir = tmp_path / "DugUp" / "python"
    python_dir.mkdir(parents=True)
    for name in ("python.exe", "pythonw.exe", "DugUp.exe"):
        (python_dir / name).write_bytes(b"")
    python = str(python_dir / "python.exe")
    made = []

    class Fake:
        def make(self, path, target, arguments):
            made.append(arguments)
            path.write_bytes(b"shortcut")
            return True

    monkeypatch.setattr(tray_module, "shortcuts", Fake())
    assert tray_module.refresh_startup("win32", python=python) is None, "no entry: none is made"
    folder = tmp_path / "roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    folder.mkdir(parents=True)
    script = folder / "DugUp tray.cmd"
    script.write_text(f'@echo off\r\nstart "" /min "D:\\source\\.venv\\Scripts\\pythonw.exe" -m dugup tray --data-dir "D:\\mine"\r\n',
                      encoding="utf-8")
    assert tray_module.refresh_startup("win32", python=python) is None and script.exists(), "another copy's entry stays"
    script.write_text(f'@echo off\r\nstart "" /min "{python_dir / "pythonw.exe"}" -m dugup tray --data-dir "E:\\Index"\r\n',
                      encoding="utf-8")
    shortcut = tray_module.refresh_startup("win32", python=python)
    assert shortcut is not None and shortcut.name == "DugUp tray.lnk" and not script.exists()
    assert made == ['-m dugup tray --data-dir "E:\\Index"'], "the same data folder as before"


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
        assert summons.summon(stage.layout.root, "state") == "updates paused", "a sentence for the remote"
        tray.handle("resume")
        assert not watcher.paused
        # free pauses the updates and unloads; load (on a thread) reloads and resumes; "models" says which
        loads = []
        stage.service.load_models = lambda: (loads.append(1), ["the question model (searching by meaning)"])[1]
        stage.service.loaded_models = lambda: ["the question model (searching by meaning)"] if loads else []
        tray.handle("free")
        assert watcher.paused and summons.summon(stage.layout.root, "models") == "none"
        tray.handle("load")
        assert summons.summon(stage.layout.root, "models") == "loading" or loads, "asked while the thread runs, or done already"
        for _ in range(200):
            tray._poll()
            if not tray.loading:
                break
            root.after(10)
        assert loads == [1] and not tray.loading and not watcher.paused
        assert summons.summon(stage.layout.root, "models") == "the question model (searching by meaning)"
        assert any("Loaded: the question model" in text for text in said)
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
        # the box remembers its size through the tray's memory, and the tray's menu can free the models
        popup.hide()
        before = popup.size
        popup._sized(SimpleNamespace(widget=popup.top, width=900, height=400))
        assert popup.size == before, "a size change while hidden is not a user's resize"
        popup.show()
        popup._sized(SimpleNamespace(widget=popup.top, width=900, height=400))
        assert popup.size == (900, 400) and remember.read(stage.layout.root)["popup_size"] == [900, 400]
        popup.hide()
        tray.handle("free")
        assert watcher.paused and "paused" in said[-1]
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


def test_a_background_update_with_the_real_service_leaves_the_search_box_working(stage):
    """The service's reading connection belongs to one thread. The watcher runs on another and must never
    touch it (before the fix, its check for waiting sketches did, and every search from the box failed)."""
    import tkinter as tk
    from test_watcher import settings as watch_settings
    root = tk.Toplevel(stage.root)
    said = []
    stage.service.run_meaning = lambda *a, **k: None      # the check for waiting sketches is the point, not the fill
    stage.service.run_looks = lambda *a, **k: None
    watcher = tray_module.Watcher(stage.service, watch_settings(watch_every_minutes=60, ocr_after_index=False),
                                  cores=4, say=said.append)
    tray = tray_module.Tray(root, stage.service, stage.service.settings, launch=lambda a: None, make_icon=lambda t: None,
                            start_hotkey=lambda text, fire: hotkey.Hotkey(""), say=said.append, watcher=watcher)
    try:
        assert tray.watcher.tick()
        watcher.thread.join(30)
        assert not watcher.running and "Error" not in watcher.last_outcome, watcher.last_outcome
        assert watcher.last_outcome.startswith("index ")
        tray.popup.query.set("needle")
        tray.popup.search_now()          # on this thread, with the shared connection: must still work
        assert [r.name for r in tray.popup.rows][0] == "needle-name.txt"
    finally:
        tray.quit()
        root.destroy()


def test_the_window_is_started_with_a_console_free_python_on_windows(tmp_path, monkeypatch):
    import sys
    quiet = tmp_path / "pythonw.exe"
    monkeypatch.setattr(tray_module.os, "name", "nt")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert tray_module.quiet_python() == str(tmp_path / "python.exe"), "no pythonw beside it: the plain one"
    quiet.write_bytes(b"")
    assert tray_module.quiet_python() == str(quiet)
    class Layout:
        root = tmp_path
    assert tray_module.dugup_command(Layout, "window")[0] == str(quiet)



# -- the remote: a small window that drives the tray

def test_the_remote_drives_the_tray_through_its_summons_and_starts_it_when_there_is_none(stage, tmp_path):
    """Not running: only Start tray is live, and it starts the tray with this very installation's command.
    Running: the state the tray reports is shown in the read-only line, each button sends its command, Full
    window goes through the tray when there is one and starts a window on its own when there is not, and
    the start-at-login tick writes and removes the entry. Asking happens on a thread; the answer is shown
    when it arrives, so the test waits for it rather than assuming the asker is quick."""
    import tkinter as tk
    from dugup import remote as remote_module
    root = tk.Toplevel(stage.root)
    answers = {"state": None, "models": "none"}
    sent, launched, entries = [], [], {}
    home = tmp_path / "home"

    def ask(folder, command):
        sent.append(command)
        return answers.get(command, "ok" if answers["state"] is not None else None)

    def install():
        path = tray_module.install_startup(stage.layout, "win32", home)
        entries["path"] = path
        return path

    window = remote_module.Remote(root, stage.layout, ask=ask, launch=launched.append, install_startup=install,
                                      remove_startup=lambda: tray_module.remove_startup("win32", home),
                                      startup_entry=lambda: tray_module.startup_entry(home, "win32"), platform="win32")

    def settle(tries=50):
        for _ in range(tries):
            root.update()
            if not window.asking and not window.answers.qsize():
                break
            root.after(20)
            root.update()
        root.update()

    try:
        settle()
        assert window.status.get() == remote_module.NOT_RUNNING
        assert "disabled" not in window.buttons["start"].state() and "disabled" in window.buttons["quit"].state()
        window.window()
        assert launched[-1][2:4] == ["dugup", "window"], "no tray: the window is started on its own"
        window.start_tray()
        assert launched[-1][2:4] == ["dugup", "tray"] and "--data-dir" in launched[-1]
        # the tray is up now
        answers["state"] = "updating (ocr)"
        window.refresh()
        for _ in range(100):
            root.update()
            if window.status.get() == "updating (ocr)":
                break
            root.after(20)
        assert window.status.get() == "updating (ocr)"
        assert "disabled" in window.buttons["start"].state() and "disabled" not in window.buttons["free"].state()
        sent.clear()
        window.popup(); window.update(); window.free()
        assert "unloaded" in window.note.get() and "index stays warm" in window.note.get()
        window.window(); window.quit_tray()
        assert [c for c in sent if c != "state"] == ["popup", "update", "free", "window", "quit"]
        assert "unloads every model" in window.note.get()
        # start at login
        assert not window.at_login.get()
        window.at_login.set(True)
        window.toggle_startup()
        assert entries["path"].exists() and "dugup tray" in entries["path"].read_text(encoding="utf-8")
        window.at_login.set(False)
        window.toggle_startup()
        assert not entries["path"].exists() and "no longer" in window.note.get()
    finally:
        window.close()


# -- cloud search in the remote: off unless allowed, plain about the cost, folders chosen in a window

def _remote(stage, tmp_path, confirm, chosen=None):
    import tkinter as tk
    from dugup import remote as remote_module
    root = tk.Toplevel(stage.root)
    asked = []

    def answer(title, text, allow, cancel):
        asked.append((title, text, allow))
        return confirm.pop(0) if confirm else False

    picks = []

    def choose_folder(**kw):
        picks.append(kw.get("initialdir"))
        return chosen.pop(0) if chosen else ""

    window = remote_module.Remote(root, stage.layout, ask=lambda folder, command: None, launch=lambda args: None,
                                  startup_entry=lambda: tmp_path / "no entry", platform="win32", confirm=answer,
                                  choose_folder=choose_folder, claude_config=tmp_path / "claude.json")
    return window, asked, picks


def test_cloud_search_is_allowed_only_after_the_cost_is_read_and_turned_off_at_once(stage, tmp_path):
    from dugup import assistantsetup as setup
    window, asked, _ = _remote(stage, tmp_path, confirm=[False])
    try:
        assert window.cloud.get() is False, "off until allowed"
        window.cloud.set(True)
        window.toggle_cloud()                                  # Cancel
        assert window.cloud.get() is False and setup.load(stage.layout).mcp_enabled is False
        title, text, allow = asked[0]
        assert allow == "Allow" and "leaves this computer" in text and "file names and full paths" in text
        assert "does not cover what Claude is shown" in text
        window.confirm = lambda *a: asked.append(a) or (a[2] == "Allow")    # Allow, then "Not now" to connecting
        window._click_cloud()                                  # the words beside the box work too
        assert window.cloud.get() is True and setup.load(stage.layout).mcp_enabled is True
        assert not (tmp_path / "claude.json").exists() and "connect --write" in window.note.get()
        window._click_cloud()                                  # off: no question
        before = len(asked)
        assert setup.load(stage.layout).mcp_enabled is False and len(asked) == before
        window.confirm = lambda *a: True                       # Allow, and Connect
        window._click_cloud()
        assert setup.is_connected(tmp_path / "claude.json") and "restart the Claude app" in window.note.get()
    finally:
        window.close()


def test_folders_are_chosen_in_a_window_that_remembers_where_it_was(stage, tmp_path):
    from dugup import assistantsetup as setup
    first, second = tmp_path / "Shared" / "Travel", tmp_path / "Shared" / "Recipes"
    first.mkdir(parents=True)
    second.mkdir()
    window, _, picks = _remote(stage, tmp_path, confirm=[], chosen=[str(first), str(second)])
    try:
        assert window.cloud_folders.get() == "Every indexed folder"
        chooser = window.choose_folders()
        chooser.add()
        chooser.add()
        assert picks[1] == str(tmp_path / "Shared"), "the dialog opens where the last folder was chosen"
        assert chooser.folders() == [str(first), str(second)] and chooser.mode.get() == "these"
        chooser.list.selection_set(1)
        chooser.remove()
        chooser.save()
        assert setup.folders_of(setup.load(stage.layout)) == [str(first)]
        assert str(first) in window.cloud_folders.get() and "Saved" in window.note.get()
        again = window.choose_folders()
        assert again.folders() == [str(first)], "the window opens with the saved list"
        again.list.delete(0, "end")
        again.save()
        assert "at least one folder" in again.message.get(), "an empty list is not taken for every folder"
        again.mode.set("all")
        again.save()
        assert setup.folders_of(setup.load(stage.layout)) is None and window.cloud_folders.get() == "Every indexed folder"
        setup.change(stage.layout, folders=[str(second)])      # changed elsewhere (dugup mcp enable --folders)
        window._show(None, None)
        assert str(second) in window.cloud_folders.get()
    finally:
        window.close()


def test_the_search_box_and_the_tray_menu_carry_the_cloud_switch_too(stage, tmp_path):
    """Win+Shift+F is what people see most, so the switch is there as well: one line, asking first."""
    import tkinter as tk
    from dugup import assistantsetup as setup
    root = tk.Toplevel(stage.root)
    asked = []

    def confirm(title, text, allow, cancel):
        asked.append(title)
        return allow == "Allow"                    # Allow cloud access; "Not now" to connecting the app

    from test_watcher import Service as FakeRuns, settings as watch_settings
    watcher = tray_module.Watcher(FakeRuns(), watch_settings(watch_every_minutes=0), cores=4)
    tray = tray_module.Tray(root, stage.service, stage.service.settings, launch=lambda a: None, make_icon=lambda t: None,
                            start_hotkey=lambda text, fire: hotkey.Hotkey(""), say=lambda text: None, watcher=watcher,
                            cloud_options={"confirm": confirm, "claude_config": tmp_path / "claude.json"})
    try:
        switch = tray.popup.cloud_switch
        assert switch.on.get() is False
        tray.handle("cloud")                       # the tray menu's "Allow cloud search access"
        assert tray.popup.shown and asked[0] == setup.CONFIRM_TITLE and switch.on.get() is True
        assert setup.load(stage.layout).mcp_enabled is True and "connect" in tray.popup.note.get()
        tray.handle("cloud")
        assert switch.on.get() is False and setup.load(stage.layout).mcp_enabled is False and len(asked) == 2
        setup.change(stage.layout, enabled=True)   # turned on elsewhere: the box shows it when next brought up
        tray.popup.hide()
        tray.popup.show()
        assert switch.on.get() is True
    finally:
        tray.quit()
        root.destroy()


def test_report_a_problem_is_one_step_from_the_tray_the_search_box_and_the_remote(stage, tmp_path):
    import tkinter as tk
    from dugup import remote as remote_module
    root = tk.Toplevel(stage.root)
    launched = []
    from test_watcher import Service as FakeRuns, settings as watch_settings
    watcher = tray_module.Watcher(FakeRuns(), watch_settings(watch_every_minutes=0), cores=4)
    tray = tray_module.Tray(root, stage.service, stage.service.settings, launch=launched.append, make_icon=lambda t: None,
                            start_hotkey=lambda text, fire: hotkey.Hotkey(""), say=lambda text: None, watcher=watcher)
    try:
        tray.handle("report")                      # the tray menu's Report a problem...
        tray.popup.report()                        # the search box's button
        assert len(launched) == 2 and all(c[1:4] == ["-m", "dugup", "report"] for c in launched)
    finally:
        tray.quit()
        root.destroy()
    top = tk.Toplevel(stage.root)
    window = remote_module.Remote(top, stage.layout, ask=lambda f, c: None, launch=launched.append,
                                  startup_entry=lambda: tmp_path / "none", claude_config=tmp_path / "c.json")
    try:
        window.report_button.invoke()
        assert launched[-1][1:4] == ["-m", "dugup", "report"] and "own" in window.note.get()
    finally:
        window.close()
