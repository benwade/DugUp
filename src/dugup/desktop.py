"""Handing a file to the desktop: open it, open it for editing, or show it in its folder.

These start the user's own programs (a viewer, the file manager). They are meant to outlive this program,
so they do not go through launcher.py, whose whole point is to end what it started.

Every function returns "" when the request was handed over, or a sentence saying why it could not be.
Nothing here raises for a missing file or a missing tool. The pieces that touch the system are passed in,
so the choices made for each platform can be tested on any platform.
"""

from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
import sys
from typing import Callable, Optional, Sequence

GONE = "The file is not at this path right now."


class Desktop:
    def __init__(self, platform: Optional[str] = None, startfile: Optional[Callable[..., None]] = None,
                 run: Optional[Callable[[object], object]] = None,
                 which: Callable[[str], Optional[str]] = shutil.which,
                 exists: Callable[[str], bool] = os.path.exists) -> None:
        self.platform = platform or sys.platform
        self.startfile = startfile if startfile is not None else getattr(os, "startfile", None)
        self.run = run or self._start
        self.which = which
        self.exists = exists

    @staticmethod
    def _start(command: object) -> object:
        quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.name != "nt":
            quiet["start_new_session"] = True     # the viewer is not ended when this program is
        return subprocess.Popen(command, **quiet)    # type: ignore[arg-type]

    @property
    def windows(self) -> bool:
        return self.platform.startswith("win")

    def _hand_over(self, command: Sequence[str], tool: str) -> str:
        if self.which(tool) is None:
            return f"This needs the program {tool}, which was not found on this machine."
        try:
            self.run(list(command))
        except OSError as exc:
            return f"Could not start {tool}: {exc.strerror or exc}"
        return ""

    def open_file(self, path: str) -> str:
        """Open the file with the program the desktop uses for its type."""
        if not self.exists(path):
            return GONE
        if self.windows and self.startfile is not None:
            try:
                self.startfile(path)
            except OSError as exc:
                return f"Windows could not open the file: {exc.strerror or exc}"
            return ""
        tool = "open" if self.platform == "darwin" else "xdg-open"
        return self._hand_over([tool, path], tool)

    def edit_file(self, path: str) -> str:
        """Open the file for editing. Only Windows has a separate "edit" action, and only for some types;
        everywhere else, and for the other types, this is the same as opening the file."""
        if not self.exists(path):
            return GONE
        if self.windows and self.startfile is not None:
            try:
                self.startfile(path, "edit")
                return ""
            except OSError:
                pass      # no edit action for this type: open it instead
        return self.open_file(path)

    def show_in_folder(self, path: str) -> str:
        """Open the file's folder in the file manager, with the file selected where the system can do that.
        When the file itself is gone, its folder is shown if that still exists."""
        folder = (ntpath.dirname(path) if self.windows else os.path.dirname(path)) or "."
        there = self.exists(path)
        if not there and not self.exists(folder):
            return "Neither the file nor its folder is at this path right now."
        if self.windows:
            # explorer takes the path as part of one argument; quoted here so a comma or space survives
            command = f'explorer /select,"{path}"' if there else f'explorer "{folder}"'
            try:
                self.run(command)
            except OSError as exc:
                return f"Could not start the file manager: {exc.strerror or exc}"
            return ""
        if self.platform == "darwin":
            return self._hand_over(["open", "-R", path] if there else ["open", folder], "open")
        return self._hand_over(["xdg-open", folder], "xdg-open")
