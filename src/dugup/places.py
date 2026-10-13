"""Where a person's files are likely to be: the starting list for "which folders should DugUp index?".

Pure apart from asking which folders exist (and that can be replaced in a test). Each place comes with a
starting tick and a reason in plain words, because a person cannot weigh a choice they are not told about.

DugUp only ever READS the files it indexes; it changes none of them. So the reason for leaving out
Windows' own folders is not danger. It is that nothing a person wrote lives there, there are hundreds of
thousands of files, and indexing them would take long and bury the person's own results.

These are starting points. Every tick can be changed, and any other folder can be added.
"""

from __future__ import annotations

import os
import string
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

YOURS, MAYBE, SYSTEM = "yours", "maybe", "system"


@dataclass(frozen=True)
class Place:
    path: str
    label: str
    ticked: bool
    reason: str
    kind: str = YOURS


# (folder name below the home folder, label, ticked, reason, kind)
HOME_FOLDERS = (
    ("Documents", "Documents", True, "your own documents: the first place most people want searched", YOURS),
    ("Desktop", "Desktop", True, "files kept to hand, often the ones looked for most", YOURS),
    ("Pictures", "Pictures", True, "photos, screenshots and scans; pictures are listed by name and read for words only if you ask", YOURS),
    ("Downloads", "Downloads", False, "mostly installers and things you did not write; tick it if you keep documents there", MAYBE),
    ("Music", "Music", False, "sound has no words to read; ticking it makes the file names searchable", MAYBE),
    ("Videos", "Videos", False, "video has no words to read; ticking it makes the file names searchable", MAYBE),
    ("OneDrive", "OneDrive", False, "files kept in the cloud are listed by name; only those also on this computer are read", MAYBE),
)
SYSTEM_REASON = ("{what}: nothing you wrote lives here, and there are a great many files. DugUp only reads files and "
                 "never changes them, so indexing this is not dangerous, but it would take long and bury your own results")
WINDOWS_SYSTEM = (("Windows", "Windows itself"), ("Program Files", "installed programs"),
                  ("Program Files (x86)", "installed programs"), ("ProgramData", "data that programs keep for themselves"))
POSIX_SYSTEM = (("/usr", "the system's programs"), ("/etc", "the system's settings"), ("/var", "data that programs keep for themselves"))


def candidates(home: Optional[str] = None, is_dir: Callable[[str], bool] = os.path.isdir,
               windows: Optional[bool] = None, drives: Optional[Sequence[str]] = None) -> List[Place]:
    """Places to offer, the person's own first, then whole drives, then the system's folders (unticked, with why)."""
    windows = (os.name == "nt") if windows is None else windows
    home = home or os.path.expanduser("~")
    join = (lambda a, b: a.rstrip("\\/") + "\\" + b) if windows else (lambda a, b: a.rstrip("/") + "/" + b)
    found: List[Place] = []
    for name, label, ticked, reason, kind in HOME_FOLDERS:
        path = join(home, name)
        if is_dir(path):
            found.append(Place(path, label, ticked, reason, kind))
    if windows:
        if drives is None:
            drives = [f"{letter}:\\" for letter in string.ascii_uppercase if is_dir(f"{letter}:\\")]
        system_drive = (home[:2].upper() + "\\") if len(home) > 1 and home[1] == ":" else "C:\\"
        for drive in drives:
            if drive.upper() == system_drive:
                continue
            found.append(Place(drive, f"The whole of drive {drive[:2]}", False,
                               "a whole drive: tick it if your files live there; you then choose folder by folder what is read", MAYBE))
        for name, what in WINDOWS_SYSTEM:
            path = join(system_drive, name)
            if is_dir(path):
                found.append(Place(path, name, False, SYSTEM_REASON.format(what=what), SYSTEM))
    else:
        for path, what in POSIX_SYSTEM:
            if is_dir(path):
                found.append(Place(path, path, False, SYSTEM_REASON.format(what=what), SYSTEM))
    return found


def without_nested(paths: Sequence[str], windows: Optional[bool] = None) -> List[str]:
    """The chosen folders with any that lie inside another chosen one taken out (the outer one covers it,
    and one file must belong to one indexed folder). Order is kept."""
    windows = (os.name == "nt") if windows is None else windows
    fold = (lambda p: p.replace("/", "\\").rstrip("\\").casefold()) if windows else (lambda p: p.rstrip("/"))
    sep = "\\" if windows else "/"
    kept: List[str] = []
    folded = [fold(p) for p in paths]
    for index, path in enumerate(paths):
        mine = folded[index]
        inside = any(other != mine and mine.startswith(other + sep) for other in folded)
        duplicate = mine in folded[:index]
        if not inside and not duplicate:
            kept.append(path)
    return kept


LEVELS_EXPLAINED = (
    "How long the first build takes depends on what you choose here and on how far each folder is read. "
    "Listing file names takes seconds, even for a whole drive. Reading the words in documents takes minutes for "
    "thousands of files. Reading pictures and scanned pages is the slow part{pace}. After you finish, DugUp looks "
    "through each folder you ticked and shows what is inside, with a suggestion for every folder in it. "
    "Nothing is read until you have seen that and agreed.")


def pace_words(page_seconds: Optional[float], card_speedup: Optional[float], use_card: bool) -> str:
    """The reading speed on this computer, in words, when the test gave one. Division is guarded."""
    if not page_seconds or page_seconds <= 0:
        return ""
    per_minute = 60.0 / page_seconds
    if use_card and card_speedup and card_speedup > 0:
        per_minute *= card_speedup
    return f": about {per_minute:,.0f} pages a minute with one reader on this computer, going by the test"
