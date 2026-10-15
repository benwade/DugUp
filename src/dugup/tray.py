"""The tray: one process that stays, with the index open, so a key brings up the search box at once.

It owns a hidden tkinter root, the popup, the hotkey, and a listener for commands from other processes
(`dugup popup`, a second `dugup tray`). The icon in the tray comes from the pystray package when that is
installed; without it the hotkey and the popup work all the same, and the tray says so when it starts.

Everything that reaches the tray from another thread (the hotkey, the listener, the icon's menu) is put on
a queue and taken on the window thread, which polls it. Nothing touches tkinter from another thread.

The full window and an index update are started as separate processes, so the tray stays light and a
window closed by the user does not take the tray with it.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Sequence

from . import hotkey as hotkeys
from . import remember, summons
from .desktop import Desktop
from .watcher import Watcher

if TYPE_CHECKING:          # tkinter is imported when a tray is made, so the startup helpers work without it
    import tkinter as tk

POLL_MS = 100
TITLE = "DugUp"


def _detached(args: Sequence[str]) -> None:
    """Start a program that outlives this one, with no console of its own on Windows."""
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        quiet["creationflags"] = 0x00000008 | 0x00000200       # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        quiet["start_new_session"] = True
    subprocess.Popen(list(args), **quiet)    # type: ignore[arg-type]


def dugup_command(layout, *words: str) -> List[str]:
    """`dugup <words>` the way this very installation runs it, with the data folder passed along."""
    return [sys.executable, "-m", "dugup", *words, "--data-dir", str(layout.root)]


class Tray:
    def __init__(self, root: "tk.Misc", service, settings, *, platform: Optional[str] = None,
                 launch: Callable[[Sequence[str]], None] = _detached, desktop: Optional[Desktop] = None,
                 make_icon: Optional[Callable[["Tray"], object]] = None,
                 start_hotkey: Callable[[str, Callable[[], None]], hotkeys.Hotkey] = hotkeys.start,
                 say: Optional[Callable[[str], None]] = None, watcher: Optional[Watcher] = None) -> None:
        self.root, self.service, self.settings = root, service, settings
        self.layout = service.layout
        self.launch = launch
        self.desktop = desktop or Desktop(platform)
        self.say = say or (lambda text: print(text, flush=True))
        self.events: "queue.Queue[str]" = queue.Queue()
        self.closed = False
        from .popup import Popup
        root.withdraw()
        self.popup = Popup(root, service, self.folder_first, self.desktop.open_file, self.open_window, self.say,
                           present_only=lambda: bool(remember.read(self.layout.root).get("present_only", False)))
        self.hotkey = start_hotkey(settings.hotkey, lambda: self.events.put("popup")) if settings.hotkey else hotkeys.Hotkey("no hotkey set")
        self.watcher = watcher if watcher is not None else Watcher(service, settings, say=self.say)
        self.listener = summons.Listener(self.layout.root, self._summoned)
        self.listener.start()
        self.icon = None
        self.icon_problem = ""
        maker = make_icon if make_icon is not None else make_pystray_icon
        try:
            self.icon = maker(self)
        except Exception as exc:         # no pystray, or no tray on this desktop: carry on without the icon
            self.icon_problem = f"{type(exc).__name__}: {exc}"
        self._poll_id = root.after(POLL_MS, self._poll)

    # -- what the tray can be asked to do

    def folder_first(self) -> str:
        return str(remember.read(self.layout.root).get("folder") or "")

    def open_window(self, words: str = "") -> None:
        self.launch(dugup_command(self.layout, "window", *(["--search", words] if words else [])))

    def update_index(self) -> None:
        """Update now, in this process, through the watcher (paused or not)."""
        self.watcher.now()

    def quit(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.root.after_cancel(self._poll_id)
        except Exception:
            pass
        self.hotkey.stop()
        self.watcher.stop()
        self.listener.close()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        try:
            self.root.quit()
        except Exception:
            pass

    def status_lines(self) -> List[str]:
        lines = [f"DugUp is in the tray (data folder {self.layout.root})."]
        if self.hotkey.problem:
            lines.append(f"No desktop-wide key: {self.hotkey.problem}.")
        else:
            lines.append(f"{self.settings.hotkey} brings up the search box anywhere.")
        every = self.settings.watch_every_minutes
        lines.append(f"The index updates itself every {every:g} minutes, and once now." if every > 0
                     else "The index does not update by itself (watch_every_minutes is 0).")
        if self.icon is None:
            lines.append(f"No icon in the tray ({self.icon_problem or 'the pystray package is not installed'}); "
                         "the key and `dugup popup` work all the same, and `dugup tray --quit` ends it.")
        return lines

    # -- from other threads

    def _summoned(self, command: str) -> str:
        if command == "ping":
            return "pong"
        if command == "status":
            return "updating" if self.watcher.running else "idle"
        if command == "stop":          # answered here and now: the asker is waiting to take the lock
            self.watcher.stop()
            return "ok"
        self.events.put(command)
        return "ok"

    def _poll(self) -> None:
        if self.closed:
            return
        while True:
            try:
                command = self.events.get_nowait()
            except queue.Empty:
                break
            self.handle(command)
            if self.closed:
                return
        self.watcher.tick()
        self._poll_id = self.root.after(POLL_MS, self._poll)

    def handle(self, command: str) -> None:
        if command == "popup":
            self.popup.show()
        elif command == "window":
            self.open_window()
        elif command == "update":
            self.update_index()
        elif command == "pause":
            self.watcher.pause()
        elif command == "resume":
            self.watcher.resume()
        elif command == "stop":
            self.watcher.stop()
        elif command == "quit":
            self.quit()


def make_pystray_icon(tray: Tray):
    """The icon and its menu, through pystray, run on a thread of its own. Raises when pystray (or a tray)
    is not there; the caller carries on without."""
    import pystray
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, 44, 44), outline=(43, 108, 176, 255), width=7)
    draw.line((38, 38, 58, 58), fill=(43, 108, 176, 255), width=9)
    key = tray.settings.hotkey if not tray.hotkey.problem else ""
    def toggle_pause():
        tray.events.put("resume" if tray.watcher.paused else "pause")

    menu = pystray.Menu(
        pystray.MenuItem(f"Search ({key})" if key else "Search", lambda: tray.events.put("popup"), default=True),
        pystray.MenuItem("Open the window", lambda: tray.events.put("window")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: tray.watcher.state(), None, enabled=False),
        pystray.MenuItem("Update now", lambda: tray.events.put("update")),
        pystray.MenuItem(lambda item: "Resume updates" if tray.watcher.paused else "Pause updates", toggle_pause),
        pystray.MenuItem("Stop this update", lambda: tray.events.put("stop"), visible=lambda item: tray.watcher.running),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit DugUp", lambda: tray.events.put("quit")),
    )
    icon = pystray.Icon("dugup", image, TITLE + (f" ({key})" if key else ""), menu)
    threading.Thread(target=icon.run, name="dugup-tray-icon", daemon=True).start()
    return icon


# -- starting with the computer

def startup_entry(home: Optional[Path] = None, platform: Optional[str] = None) -> Path:
    """Where an entry that starts the tray at login goes."""
    platform = platform or sys.platform
    home = home or Path.home()
    if platform.startswith("win"):
        appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "DugUp tray.cmd"
    return home / ".config" / "autostart" / "dugup-tray.desktop"


def startup_text(layout, platform: Optional[str] = None, python: Optional[str] = None) -> str:
    platform = platform or sys.platform
    python = python or sys.executable
    if platform.startswith("win"):
        quiet = Path(python).with_name("pythonw.exe")        # no console window at login
        program = str(quiet if quiet.exists() else python)
        return f'@echo off\r\nstart "" /min "{program}" -m dugup tray --data-dir "{layout.root}"\r\n'
    return ("[Desktop Entry]\nType=Application\nName=DugUp tray\n"
            f"Exec={python} -m dugup tray --data-dir \"{layout.root}\"\nX-GNOME-Autostart-enabled=true\n")


def install_startup(layout, platform: Optional[str] = None, home: Optional[Path] = None, python: Optional[str] = None) -> Path:
    path = startup_entry(home, platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(startup_text(layout, platform, python), encoding="utf-8")
    return path


def remove_startup(platform: Optional[str] = None, home: Optional[Path] = None) -> Optional[Path]:
    path = startup_entry(home, platform)
    if path.exists():
        path.unlink()
        return path
    return None
