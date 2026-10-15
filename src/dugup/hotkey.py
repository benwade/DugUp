"""A key that works anywhere on the desktop and brings up the search box.

Windows has a system call for exactly this (RegisterHotKey), so nothing is installed for it. It must be
registered and listened for on one and the same thread, which is why the listening runs on a thread of its
own with its own message loop. Elsewhere there is no one way: the desktop's own keyboard settings can bind
a key to `dugup popup`, which reaches the running tray.

A hotkey is written the way people write them: "Ctrl+Alt+Space", "Win+Shift+F", "Ctrl+Alt+D".
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Optional, Tuple

DEFAULT = "Ctrl+Alt+Space"

MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008, "super": 0x0008}
NAMED_KEYS = {"space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B,
              "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21,
              "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "`": 0xC0, "-": 0xBD,
              "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


def parse(text: str) -> Tuple[int, int, str]:
    """(modifiers, virtual key, problem). The problem is "" when the text is a usable hotkey."""
    parts = [p.strip() for p in text.replace(" ", "").split("+") if p.strip()]
    if not parts:
        return 0, 0, "no key given"
    mods, key = 0, parts[-1]
    for part in parts[:-1]:
        bit = MODIFIERS.get(part.lower())
        if bit is None:
            return 0, 0, f'"{part}" is not a modifier (Ctrl, Alt, Shift, Win)'
        mods |= bit
    low = key.lower()
    if low in NAMED_KEYS:
        code = NAMED_KEYS[low]
    elif len(key) == 1 and (key.isalnum()):
        code = ord(key.upper())
    elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        code = 0x70 + int(low[1:]) - 1
    else:
        return 0, 0, f'"{key}" is not a key this program knows'
    if not mods and not (0x70 <= code <= 0x87):
        return 0, 0, "a hotkey needs at least one of Ctrl, Alt, Shift or Win, unless it is a function key"
    return mods, code, ""


class Hotkey:
    """What the tray holds: `problem` says why there is no hotkey ("" when there is one); `stop()` lets go."""

    def __init__(self, problem: str = "") -> None:
        self.problem = problem

    def stop(self) -> None:
        pass


class WindowsHotkey(Hotkey):
    ID = 0xD06

    def __init__(self, text: str, fire: Callable[[], None]) -> None:
        super().__init__()
        self.fire = fire
        self.mods, self.code, problem = parse(text)
        self.thread_id: Optional[int] = None
        self.ready = threading.Event()
        if problem:
            self.problem = problem
            return
        self.thread = threading.Thread(target=self._loop, name="dugup-hotkey", daemon=True)
        self.thread.start()
        self.ready.wait(5.0)

    def _loop(self) -> None:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, self.ID, self.mods | MOD_NOREPEAT, self.code):
            self.problem = "that key is already taken by another program (Windows refused it)"
            self.ready.set()
            return
        self.ready.set()
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    try:
                        self.fire()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, self.ID)

    def stop(self) -> None:
        if self.thread_id is not None and not self.problem:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


def start(text: str, fire: Callable[[], None], platform: Optional[str] = None) -> Hotkey:
    """The platform's hotkey, or one that says why there is none."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return WindowsHotkey(text, fire)
    _, _, problem = parse(text)
    if problem:
        return Hotkey(problem)
    return Hotkey("a desktop-wide key is not set up by this program here; in your desktop's keyboard settings, "
                  "bind a key to the command: dugup popup")
