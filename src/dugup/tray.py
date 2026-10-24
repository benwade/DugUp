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
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence

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


def quiet_python() -> str:
    """The interpreter to start a window with. On Windows, pythonw.exe beside the running one: a venv's
    python.exe is a launcher that starts the real Python as a child, which, started without a console, is
    given a new console window of its own; pythonw.exe never shows one."""
    if os.name == "nt":
        folder = os.path.dirname(sys.executable)
        for name in ("DugUp.exe", "pythonw.exe"):     # DugUp.exe where installed: Windows names it DugUp
            quiet = os.path.join(folder, name)
            if os.path.exists(quiet):
                return quiet
    return sys.executable


def dugup_command(layout, *words: str) -> List[str]:
    """`dugup <words>` the way this very installation runs it, with the data folder passed along."""
    return [quiet_python(), "-m", "dugup", *words, "--data-dir", str(layout.root)]


class Tray:
    def __init__(self, root: "tk.Misc", service, settings, *, platform: Optional[str] = None,
                 launch: Callable[[Sequence[str]], None] = _detached, desktop: Optional[Desktop] = None,
                 make_icon: Optional[Callable[["Tray"], object]] = None,
                 start_hotkey: Callable[[str, Callable[[], None]], hotkeys.Hotkey] = hotkeys.start,
                 say: Optional[Callable[[str], None]] = None, watcher: Optional[Watcher] = None,
                 cloud_options: Optional[dict] = None) -> None:
        self.root, self.service, self.settings = root, service, settings
        self.layout = service.layout
        self.launch = launch
        self.desktop = desktop or Desktop(platform)
        self.say = say or (lambda text: print(text, flush=True))
        self.events: "queue.Queue[str]" = queue.Queue()
        self.closed = False
        from .quitsignal import Listener
        self.quit_listener = Listener(self.layout.root)     # "Quit DugUp" from any DugUp program
        self.loading = False
        from .popup import Popup
        root.withdraw()
        service.list_roots()      # the service's reading connection belongs to the thread that first uses it:
                                  # this one, before the background update (on its own thread) can claim it
        kept = remember.read(self.layout.root).get("popup_size")
        self.popup = Popup(root, service, self.folder_first, self.desktop.open_file, self.open_window, self.say,
                           present_only=lambda: bool(remember.read(self.layout.root).get("present_only", False)),
                           size=tuple(kept) if isinstance(kept, list) and len(kept) == 2 else None,
                           on_resize=lambda size: remember.write(self.layout.root, popup_size=list(size)),
                           cloud_options=cloud_options, report=self.report)
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
        from .watchdog import Watchdog
        self.watchdog = Watchdog(root, self.layout.logs, "tray")

    # -- what the tray can be asked to do

    def folder_first(self) -> str:
        return str(remember.read(self.layout.root).get("folder") or "")

    def open_window(self, words: str = "") -> None:
        self.launch(dugup_command(self.layout, "window", *(["--search", words] if words else [])))

    def free_models(self) -> None:
        """End the kept model workers and pause background updates, which would load them again."""
        self.watcher.stop()
        self.watcher.pause()
        ended = self.service.free_models()
        self.say(("Freed: " + ", ".join(ended) + ". " if ended else "No model was loaded. ")
                 + "Background updates are paused so they do not load the models again; resume them from the menu.")

    def load_models(self) -> None:
        """The opposite of free_models: load the models on a thread (seconds), then resume background updates."""
        if self.loading:
            return
        self.loading = True

        def work():
            try:
                loaded = self.service.load_models()
            except Exception as exc:              # a failed load is reported, never a dead tray
                loaded, problem = [], f"{type(exc).__name__}: {exc}"
            else:
                problem = ""
            self.events.put(("loaded", loaded, problem))

        threading.Thread(target=work, daemon=True, name="dugup-tray-load").start()

    def _loaded(self, loaded, problem: str) -> None:
        self.loading = False
        self.watcher.resume()
        if problem:
            self.say(f"The models could not be loaded: {problem}. Background updates resume all the same.")
        else:
            self.say(("Loaded: " + ", ".join(loaded) + ". " if loaded else "No model is installed to load. ")
                     + "Background updates resume.")

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
        self.watchdog.stop()
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
        if command == "state":          # a sentence for a person (the remote); "status" stays a word for programs
            return self.watcher.state()
        if command == "models":         # what is loaded, for the remote's toggle; "loading" while a load runs
            return "loading" if self.loading else (", ".join(self.service.loaded_models()) or "none")
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
            if isinstance(command, tuple):           # an answer from a thread of our own
                if command[0] == "loaded":
                    self._loaded(command[1], command[2])
                continue
            self.handle(command)
            if self.closed:
                return
        if self.quit_listener.due():
            self.quit()
            return
        self.watcher.tick()
        self._poll_id = self.root.after(POLL_MS, self._poll)

    def report(self) -> None:
        """Report a problem, as a program of its own (it works even if this one is stuck later)."""
        from . import report
        report.launch(self.layout, self.launch)

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
        elif command == "free":
            self.free_models()
        elif command == "load":
            self.load_models()
        elif command == "about":
            self.popup.about()
        elif command == "report":
            self.report()
        elif command == "cloud":
            self.popup.show()                  # the switch asks first when turning on: the question needs a window
            self.popup.cloud_switch.click()
        elif command == "quit":
            self.quit()
        elif command == "quit-all":            # the icon's Quit DugUp: every DugUp program, not only the tray
            from . import quitsignal
            quitsignal.send(self.layout.root)
            self.quit()


def make_pystray_icon(tray: Tray):
    """The icon and its menu, through pystray, run on a thread of its own. Raises when pystray (or a tray)
    is not there; the caller carries on without."""
    import pystray
    from PIL import Image, ImageDraw
    from .identity import icon_image
    image = icon_image(64)
    if image is None:                    # no icon file: a drawn magnifying glass
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((6, 6, 44, 44), outline=(43, 108, 176, 255), width=7)
        draw.line((38, 38, 58, 58), fill=(43, 108, 176, 255), width=9)
    key = tray.settings.hotkey if not tray.hotkey.problem else ""
    def toggle_pause():
        tray.events.put("resume" if tray.watcher.paused else "pause")

    def cloud_on() -> bool:
        from .assistantsetup import load
        try:
            return bool(load(tray.layout).mcp_enabled)
        except Exception:
            return False

    menu = pystray.Menu(
        pystray.MenuItem(f"Search ({key})" if key else "Search", lambda: tray.events.put("popup"), default=True),
        pystray.MenuItem("Open the window", lambda: tray.events.put("window")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: tray.watcher.state(), None, enabled=False),
        pystray.MenuItem("Update now", lambda: tray.events.put("update")),
        pystray.MenuItem(lambda item: "Resume updates" if tray.watcher.paused else "Pause updates", toggle_pause),
        pystray.MenuItem("Stop this update", lambda: tray.events.put("stop"), visible=lambda item: tray.watcher.running),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Free the graphics card and memory", lambda: tray.events.put("free")),
        pystray.MenuItem("Load the models", lambda: tray.events.put("load")),
        pystray.MenuItem("Allow cloud search access", lambda: tray.events.put("cloud"), checked=lambda item: cloud_on()),
        pystray.MenuItem("Report a problem...", lambda: tray.events.put("report")),
        pystray.MenuItem("About DugUp", lambda: tray.events.put("about")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit DugUp", lambda: tray.events.put("quit-all")),
    )
    icon = pystray.Icon("dugup", image, TITLE + (f" ({key})" if key else ""), menu)
    threading.Thread(target=icon.run, name="dugup-tray-icon", daemon=True).start()
    return icon


# -- starting with the computer
#
# On Windows the entry is a shortcut in the Startup folder, "DugUp tray", that starts DugUp.exe (the
# installer's own program file), so Startup apps lists DugUp, not Python. Where no shortcut can be made
# (no DugUp.exe, as in a source checkout, or PowerShell refuses), it is a small script, "DugUp tray.cmd",
# as before; whichever kind is made replaces the other.

def _startup_folder(home: Optional[Path], platform: str) -> Path:
    home = home or Path.home()
    appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_entry(home: Optional[Path] = None, platform: Optional[str] = None) -> Path:
    """The entry that starts the tray at login: the one there is, or where a new one goes."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        folder = _startup_folder(home, platform)
        shortcut, script = folder / "DugUp tray.lnk", folder / "DugUp tray.cmd"
        return script if script.exists() and not shortcut.exists() else shortcut
    return (home or Path.home()) / ".config" / "autostart" / "dugup-tray.desktop"


def launcher(python: Optional[str] = None) -> Path:
    """The program that starts DugUp without a console: DugUp.exe where the installer put one beside
    Python, else pythonw.exe, else Python itself."""
    python_path = Path(python or sys.executable)
    for name in ("DugUp.exe", "pythonw.exe"):
        candidate = python_path.with_name(name)
        if candidate.exists():
            return candidate
    return python_path


def tray_arguments(layout) -> str:
    return f'-m dugup tray --data-dir "{layout.root}"'


def startup_text(layout, platform: Optional[str] = None, python: Optional[str] = None) -> str:
    platform = platform or sys.platform
    if platform.startswith("win"):
        return f'@echo off\r\nstart "" /min "{launcher(python)}" {tray_arguments(layout)}\r\n'
    python = python or sys.executable
    return ("[Desktop Entry]\nType=Application\nName=DugUp tray\n"
            f"Exec={python} -m dugup tray --data-dir \"{layout.root}\"\nX-GNOME-Autostart-enabled=true\n")


class Shortcuts:
    """Windows shortcuts (.lnk), made and read through PowerShell's WScript.Shell. Every answer is False or
    None when that cannot be done, never an exception. A seam for tests."""

    MAKE = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:DUGUP_LNK); $s.TargetPath = $env:DUGUP_TARGET; "
            "$s.Arguments = $env:DUGUP_ARGS; $s.WorkingDirectory = $env:DUGUP_DIR; $s.IconLocation = $env:DUGUP_ICON; "
            "$s.Description = 'DugUp in the tray: one key searches from anywhere'; $s.WindowStyle = 7; $s.Save()")
    READ = "(New-Object -ComObject WScript.Shell).CreateShortcut($env:DUGUP_LNK).Arguments"

    def _powershell(self, script: str, values: Dict[str, str]) -> Optional[str]:
        import subprocess
        env = dict(os.environ, **values)
        try:
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    def make(self, path: Path, target: Path, arguments: str) -> bool:
        if not sys.platform.startswith("win"):
            return False
        done = self._powershell(self.MAKE, {"DUGUP_LNK": str(path), "DUGUP_TARGET": str(target), "DUGUP_ARGS": arguments,
                                            "DUGUP_DIR": str(target.parent.parent), "DUGUP_ICON": f"{target},0"})
        return done is not None and path.exists()

    def arguments(self, path: Path) -> Optional[str]:
        if not sys.platform.startswith("win"):
            return None
        found = self._powershell(self.READ, {"DUGUP_LNK": str(path)})
        return None if found is None else found.strip()


shortcuts = Shortcuts()


def install_startup(layout, platform: Optional[str] = None, home: Optional[Path] = None, python: Optional[str] = None) -> Path:
    platform = platform or sys.platform
    if platform.startswith("win"):
        folder = _startup_folder(home, platform)
        folder.mkdir(parents=True, exist_ok=True)
        shortcut, script = folder / "DugUp tray.lnk", folder / "DugUp tray.cmd"
        target = launcher(python)
        if target.name.lower() == "dugup.exe" and shortcuts.make(shortcut, target, tray_arguments(layout)):
            script.unlink(missing_ok=True)
            return shortcut
        script.write_text(startup_text(layout, platform, python), encoding="utf-8")
        shortcut.unlink(missing_ok=True)
        return script
    path = startup_entry(home, platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(startup_text(layout, platform, python), encoding="utf-8")
    return path


def refresh_startup(platform: Optional[str] = None, home: Optional[Path] = None, python: Optional[str] = None) -> Optional[Path]:
    """After an upgrade: an older start-at-login script that starts this copy of DugUp is made again in
    today's form (a shortcut to DugUp.exe), for the same data folder. An entry for another copy (a source
    checkout, say) is left alone. The new entry, or None when nothing was remade."""
    import re
    platform = platform or sys.platform
    if not platform.startswith("win"):
        return None
    script = _startup_folder(home, platform) / "DugUp tray.cmd"
    try:
        text = script.read_text(encoding="utf-8")
    except OSError:
        return None
    here = os.path.normcase(str(Path(python or sys.executable).parent))
    found = re.search(r'--data-dir "([^"]+)"', text)
    if here not in os.path.normcase(text) or found is None:
        return None

    class Kept:
        root = Path(found.group(1))

    path = install_startup(Kept, platform, home, python)
    return path if path.suffix.lower() == ".lnk" else None


def remove_startup(platform: Optional[str] = None, home: Optional[Path] = None) -> Optional[Path]:
    platform = platform or sys.platform
    if platform.startswith("win"):
        folder = _startup_folder(home, platform)
        removed = None
        for path in (folder / "DugUp tray.cmd", folder / "DugUp tray.lnk"):
            if path.exists():
                path.unlink()
                removed = path
        return removed
    path = startup_entry(home, platform)
    if path.exists():
        path.unlink()
        return path
    return None