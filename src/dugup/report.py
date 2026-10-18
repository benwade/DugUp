"""Report a problem: gather what helps find it, show it, and let the person send it themselves.

The report is put together by a program of its own (`dugup report`), so it works while another DugUp window
is frozen. It holds the version and system, the health check (`dugup doctor`), the end of the log, and any
recent freeze notes (watchdog.py). The person's Windows user name and home folder are replaced with
%USERNAME% and %USERPROFILE%. Nothing is sent: the person reads it, can edit it, and then chooses to open
GitHub with it filled in, copy it, or save it.
"""

from __future__ import annotations

import getpass
import os
import platform
import re
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from . import __version__
from .about import HOME

ISSUES = HOME + "/issues/new"
LOG_LINES = 60
NOTE_CHARS = 6000             # of one freeze note
DOCTOR_SECONDS = 20.0
URL_BODY_CHARS = 6000         # GitHub takes long addresses, but browsers and servers have limits
TITLE = "Problem report"


def redact(text: str, home: Optional[str] = None, user: Optional[str] = None) -> str:
    """The home folder and the user name, replaced by their Windows names, whatever their case."""
    home = home if home is not None else os.path.expanduser("~")
    if user is None:
        try:
            user = getpass.getuser()
        except Exception:
            user = ""
    if home and len(home) > 3:
        for variant in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
            text = re.sub(re.escape(variant), "%USERPROFILE%", text, flags=re.IGNORECASE)
    if user and len(user) >= 3:
        # only inside a path (C:\Users\name\..., /home/name/...): the same letters elsewhere are ordinary words
        text = re.sub(r"(?<=[\\/])" + re.escape(user) + r"(?=[\\/]|$|\s|[\"'`,;:)\]])", "%USERNAME%", text,
                      flags=re.IGNORECASE | re.MULTILINE)
    return text


# ---- hiding folder and file names -------------------------------------------------------------------------------

# Folder names Windows itself makes: kept, since they say where a path is without saying anything about you.
KNOWN_FOLDERS = {name.lower() for name in (
    "Users", "AppData", "Local", "LocalLow", "Roaming", "Programs", "Program Files", "Program Files (x86)", "ProgramData",
    "Windows", "System32", "Temp", "Documents", "Desktop", "Pictures", "Downloads", "Music", "Videos", "OneDrive",
    "My Drive", "Shared drives", "Dropbox", "Microsoft", "Packages", "DugUp", "data", "logs", "models", "cache", "tmp",
    "python", "Lib", "site-packages", "dugup", "home", "usr", "lib", "share", "%USERPROFILE%", "%USERNAME%",
    "<data folder>", "<program folder>")}
# A path: a drive letter (C:\...), a network share (\\server\share\...), or an absolute POSIX path. It runs to the
# end of the line or to a delimiter that is rarely part of a name.
_END = r'(?=$|[\n"<>|*?\t]|, |; | \(|\)|: (?![\\/])| \[)'
_PATH = re.compile(
    r'(?<![A-Za-z0-9])[A-Za-z]:[\\/](?![\\/])[^\n"<>|*?\t]*?' + _END      # C:\... (names may hold spaces)
    + r'|\\\\[^\\/\s]+[\\/][^\n"<>|*?\t]*?' + _END                        # \\server\share\...
    + r'|(?<![\w.:/])/(?=[^\s/])[^\s"<>|*?,;()]+',                         # /home/... (no spaces: prose stays prose)
    re.MULTILINE)


class Hider:
    """Replaces folder and file names with numbered stand-ins, the same name always with the same stand-in, and
    keeps what helps find a problem: the drive and its type, folders Windows itself makes, file endings, which
    indexed folder a path is in, and the path's length, depth and any characters that often cause trouble."""

    def __init__(self, known: Optional[dict] = None, roots: Sequence[str] = (),
                 drive_type: Optional[Callable[[str], str]] = None) -> None:
        self.known = {label: _norm(path) for label, path in (known or {}).items() if path}
        self.roots = [_norm(r) for r in roots]
        self.drive_type = drive_type or windows_drive_type
        self.folders: dict = {}
        self.files: dict = {}

    def _folder(self, name: str) -> str:
        if name.lower() in KNOWN_FOLDERS:
            return name
        key = name.lower()
        if key not in self.folders:
            self.folders[key] = f"folder-{len(self.folders) + 1:02d}"
        return self.folders[key]

    def _file(self, name: str) -> str:
        stem, dot, ending = name.rpartition(".")
        if not dot or not stem or len(ending) > 8 or not ending.isalnum():
            stem, ending = name, ""
        key = name.lower()
        if key not in self.files:
            self.files[key] = f"file-{len(self.files) + 1:02d}" + (f".{ending.lower()}" if ending else "")
        return self.files[key]

    @staticmethod
    def notes(path: str) -> str:
        found = [f"{len(path)} chars"]
        parts = [p for p in re.split(r"[\\/]+", path) if p]
        found.append(f"depth {len(parts)}")
        if any(ord(c) > 127 for c in path):
            found.append("non-ASCII")
        if any(c in path for c in "#%&{}[]~$'`!@^+=") and "%USER" not in path:
            found.append("unusual characters")
        if any(p != p.rstrip(" .") for p in parts):
            found.append("a name ending in a space or dot")
        if len(path) >= 260:
            found.append("longer than Windows' classic limit of 260")
        return ", ".join(found)

    def path(self, path: str) -> str:
        trimmed = path.rstrip(" .")
        tail = path[len(trimmed):]
        normal = _norm(trimmed)
        for label, place in self.known.items():
            if normal == place or normal.startswith(place + "/"):
                rest = trimmed[len(place):].lstrip("\\/")
                if label == "<program folder>":       # DugUp's own files: not private, and what a freeze note needs
                    return label + (("\\" if "\\" in trimmed else "/") + rest if rest else "") + tail
                return label + (("\\" if "\\" in trimmed else "/") + self._parts(rest) if rest else "") + tail
        sep = "\\" if "\\" in trimmed else "/"
        head, rest = "", trimmed
        if trimmed[1:2] == ":":
            head = trimmed[:2] + f"[{self.drive_type(trimmed[0])}]"
            rest = trimmed[2:]
        elif trimmed.startswith("\\\\"):
            head, rest = "\\\\server", "\\" + trimmed[2:].split("\\", 1)[1] if "\\" in trimmed[2:] else ""
        root_note = ""
        for at, root in enumerate(self.roots, start=1):
            if normal == root or normal.startswith(root + "/"):
                root_note = f", in indexed folder {at:02d}"
                break
        hidden = head + sep + self._parts(rest) if rest.strip("\\/") else head + rest
        return f"{hidden} ({self.notes(trimmed)}{root_note})" + tail

    def _parts(self, rest: str) -> str:
        parts = [p for p in re.split(r"[\\/]+", rest) if p]
        out = [self._folder(p) for p in parts[:-1]]
        if parts:
            last = parts[-1]
            out.append(self._file(last) if "." in last.strip(".") else self._folder(last))
        sep = "\\" if "\\" in rest else "/"
        return sep.join(out)

    def hide(self, text: str) -> str:
        return _PATH.sub(lambda m: self.path(m.group(0)), text)


def _norm(path: str) -> str:
    """A path compared as Windows compares it: either separator, any case, no trailing separator."""
    return str(path).replace("\\", "/").rstrip("/").lower()


def windows_drive_type(letter: str) -> str:
    """"fixed", "removable", "network", "CD", "RAM disk" or "unknown", as Windows says."""
    if os.name != "nt":
        return "unknown"
    try:
        import ctypes
        kind = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
    except Exception:
        return "unknown"
    return {2: "removable", 3: "fixed", 4: "network", 5: "CD", 6: "RAM disk"}.get(kind, "unknown")


def indexed_folders(data_dir: Optional[str]) -> List[str]:
    """The indexed folders, read without waiting: a busy or frozen index is simply not asked."""
    if not data_dir:
        return []
    import sqlite3
    index = Path(data_dir) / "index.sqlite"
    if not index.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{index.as_posix()}?mode=ro", uri=True, timeout=1.0)
        try:
            return [row[0] for row in conn.execute("SELECT path FROM roots ORDER BY path")]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def program_folder() -> Optional[str]:
    from .datadir import find_program_home
    home = find_program_home()
    return str(home.folder) if home is not None else str(Path(__file__).resolve().parent)


def _how_installed() -> str:
    from .datadir import find_program_home
    home = find_program_home()
    if home is None:
        return "a Python environment (no marker beside the program)"
    if (home.folder / "python").is_dir() and (home.folder / "unins000.exe").exists():
        return "the Windows installer"
    return "a copy of the source"


def _doctor(data_dir: Optional[str]) -> str:
    """The health check, or a note that it did not finish in time (an index held by a frozen window can do that)."""
    answer = {}

    def work():
        try:
            from .doctor import render, run_checks
            answer["text"] = render(run_checks(None, data_dir) if data_dir else run_checks(None))
        except Exception as exc:
            answer["text"] = f"The health check could not run: {type(exc).__name__}: {exc}"

    thread = threading.Thread(target=work, name="dugup-report-doctor", daemon=True)
    thread.start()
    thread.join(DOCTOR_SECONDS)
    return answer.get("text") or f"The health check did not finish within {DOCTOR_SECONDS:.0f} s (the index may be busy)."


def _tail(path: Path, lines: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError:
        return ""


def gather(data_dir: Optional[str], what_happened: str = "", doctor: Callable[[Optional[str]], str] = _doctor,
           hide_names: bool = True) -> str:  # (the window asks for it unhidden, and hides it as the person chooses)
    """The whole report, as Markdown, with the user name replaced and (by default) every folder and file name hidden."""
    from .watchdog import latest_notes
    parts = ["> Sent with DugUp's \"Report a problem\". It holds the version, system, health check, the end of the "
             "log (counts and reasons only) and any freeze notes (program locations only); no document text or search "
             "words. Folder and file names are replaced with numbered stand-ins (folder-01, file-01.pdf), keeping the "
             "drive type, file endings, and each path's length and unusual characters.", "",
             "## What happened", what_happened.strip() or "(please describe what you were doing and what went wrong)", "",
             "## DugUp",
             f"- Version: {__version__}, installed from {_how_installed()}",
             f"- System: {platform.platform()}",
             f"- Python: {sys.version.split()[0]}",
             f"- Data folder: {data_dir or '(not found)'}", "",
             "## Health check (dugup doctor)", "```", doctor(data_dir).rstrip(), "```"]
    if data_dir:
        logs = Path(data_dir) / "logs"
        tail = _tail(logs / "dugup.log", LOG_LINES)
        if tail:
            parts += ["", f"## The end of the log (last {LOG_LINES} lines; counts and reasons only)", "```", tail.rstrip(), "```"]
        for note in latest_notes(logs):
            text = _tail(note, 10_000)[:NOTE_CHARS]
            parts += ["", f"## Freeze note: {note.name}", "```", text.rstrip(), "```"]
    text = redact("\n".join(parts) + "\n")
    return hide(text, data_dir) if hide_names else text


def hide(text: str, data_dir: Optional[str], roots: Optional[Sequence[str]] = None) -> str:
    """Folder and file names replaced (Hider), with DugUp's own folders named for what they are."""
    known = {"<data folder>": data_dir, "<program folder>": program_folder(),
             "%USERPROFILE%": os.path.expanduser("~")}
    return Hider(known, indexed_folders(data_dir) if roots is None else roots).hide(text)


def issue_url(body: str, title: str = TITLE) -> "tuple[str, bool]":
    """The address of a new GitHub issue with the report filled in, and whether the report had to be cut short."""
    cut = len(body) > URL_BODY_CHARS
    if cut:
        body = body[:URL_BODY_CHARS] + "\n\n(The report was cut short here. The whole report is on your clipboard: paste it below.)"
    return ISSUES + "?" + urllib.parse.urlencode({"title": title, "body": body}), cut


def launch(layout, start: Optional[Callable] = None) -> None:
    """Open "Report a problem" as a program of its own, so it works whatever state the caller is in."""
    from . import tray as tray_module
    (start or tray_module._detached)(tray_module.dugup_command(layout, "report"))
