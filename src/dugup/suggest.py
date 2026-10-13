"""Suggestions for what to leave out of a folder (a proposal for the user, never applied by itself).

A person adding a folder cannot know in advance what is in it that is not worth indexing. This module
looks at a listing of the folder and names candidates, each with its reason and how much it holds:

  safe to skip    downloaded requirements (package folders, Python and conda environments), version-control
                  data, tool caches, the system's own folders. Nothing a person wrote lives there.
  your call       trash and temporary folders, backups, build output of code projects, folders where almost
                  nothing can be read, long runs of numbered pictures. Often clutter, sometimes wanted.

It is pure: it takes paths, sizes and the table of file kinds, and touches no disk. The name lists are
starting points, not rules; they are data at the top of this file so they are easy to change.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .kinds import classify, extension
from .shapes import FileKind

SAFE, REVIEW = "safe", "review"

# Folders that hold downloaded requirements wherever they appear (matched on the folder name, any case).
DEPENDENCY_NAMES = {
    "node_modules": "JavaScript packages", "bower_components": "JavaScript packages",
    "site-packages": "Python packages", "dist-packages": "Python packages", "__pypackages__": "Python packages",
    ".venv": "a Python environment", "venv": "a Python environment", ".tox": "Python test environments",
    ".nox": "Python test environments", "conda-meta": "a conda environment's records",
    "pods": "iOS packages", ".gradle": "Gradle downloads", ".m2": "Maven downloads", ".nuget": "NuGet packages",
    ".cargo": "Rust packages", ".bundle": "Ruby packages", ".pub-cache": "Dart packages",
}
# A file that marks its folder as an environment of downloaded packages, whatever the folder is called.
ENVIRONMENT_MARKERS = {"pyvenv.cfg": "a Python environment"}
ENVIRONMENT_CHILD_FOLDERS = {"conda-meta": "a conda environment"}
TOOL_NAMES = {
    ".git": "version-control data", ".hg": "version-control data", ".svn": "version-control data",
    "__pycache__": "compiled Python files", ".pytest_cache": "a tool cache", ".mypy_cache": "a tool cache",
    ".ruff_cache": "a tool cache", ".ipynb_checkpoints": "notebook checkpoints", ".idea": "editor settings",
    ".vs": "editor settings", ".vscode": "editor settings", ".cache": "a cache",
    "$recycle.bin": "the Windows recycle bin", "system volume information": "Windows system data",
    ".trash": "the system trash", ".trashes": "the system trash", "lost+found": "file-system recovery data",
}
TRASH_NAMES = {"trash", "temp", "tmp", "cache", "caches", "old", "obsolete", "deleted", "junk", "scratch", "_scratch"}
BACKUP_NAMES = {"backup", "backups", "bak", "archive", "archives"}
BUILD_NAMES = {"build", "dist", "out", "target", "bin", "obj", "_build", "coverage", "htmlcov"}
PROJECT_MARKERS = {"package.json", "pyproject.toml", "setup.py", "cargo.toml", "pom.xml", "build.gradle",
                   "build.gradle.kts", "makefile", "cmakelists.txt", "go.mod", "gemfile", "composer.json"}
PROJECT_MARKER_SUFFIXES = (".sln", ".csproj", ".vcxproj", ".xcodeproj")

UNREADABLE_MIN_FILES = 200      # a folder this size or larger ...
UNREADABLE_FRACTION = 0.98      # ... where at least this share cannot be read, is offered for skipping
SEQUENCE_MIN_FILES = 20         # this many pictures named name0001, name0002, ... in one folder
_NUMBERED = re.compile(r"^(.*?)(\d{2,})$")
# Numbered pictures with these name stems come from cameras, phones and scanners. They are what a person
# most wants found (a scanned document, a holiday photo), so a run of them is never offered for skipping.
CAMERA_STEMS = {"", "scan", "scanned", "img", "image", "dsc", "dscn", "dscf", "pxl", "mvimg", "photo", "pic", "picture",
                "page", "doc", "document", "screenshot", "p", "gopr", "wp", "sam", "cimg", "hpim", "kimg", "fax"}


@dataclass(frozen=True)
class Suggestion:
    pattern: str          # what to pass to --exclude: a folder name (skipped wherever it appears) or a path below the root
    group: str            # SAFE or REVIEW
    reason: str
    places: int           # how many folders this pattern covers
    files: int
    bytes: int
    readable: int         # files below it that DugUp could read; they would be skipped too
    example: str          # one folder it covers, as a path below the root


class _Folder:
    __slots__ = ("files", "bytes", "readable", "names", "children", "extensions", "pictures")

    def __init__(self) -> None:
        self.files = 0          # everything below this folder
        self.bytes = 0
        self.readable = 0
        self.names: Set[str] = set()        # marker files directly in this folder (lower case)
        self.children: Set[str] = set()     # names of folders directly below (lower case)
        self.extensions: Counter = Counter()   # of unreadable files below
        self.pictures: List[str] = []       # picture file names directly in this folder


def _is_marker(name: str) -> bool:
    return name in PROJECT_MARKERS or name in ENVIRONMENT_MARKERS or name.endswith(PROJECT_MARKER_SUFFIXES)


def _tree(root: str, files: Iterable[Tuple[str, int]], extensions: Optional[Mapping[str, FileKind]]) -> Dict[str, _Folder]:
    base = root.rstrip("/\\")
    folders: Dict[str, _Folder] = {"": _Folder()}
    for path, size in files:
        if not (path.startswith(base) and len(path) > len(base) and path[len(base)] in "/\\"):
            continue
        parts = [p for p in path[len(base) + 1:].replace("\\", "/").split("/") if p]
        if not parts:
            continue
        name = parts[-1]
        kind = classify(name, extensions)
        readable = kind is not FileKind.OTHER
        for depth in range(len(parts)):
            key = "/".join(parts[:depth])
            folder = folders.get(key)
            if folder is None:
                folder = folders[key] = _Folder()
            folder.files += 1
            folder.bytes += max(size, 0)
            folder.readable += readable
            if not readable:
                folder.extensions["." + extension(name) if extension(name) else "(no extension)"] += 1
            if depth < len(parts) - 1:
                folder.children.add(parts[depth].lower())
        own = folders["/".join(parts[:-1])]
        lowered = name.lower()
        if _is_marker(lowered):
            own.names.add(lowered)
        if kind is FileKind.IMAGE:
            own.pictures.append(name)
    return folders


def _has_project_marker(folder: _Folder) -> bool:
    return any(n in PROJECT_MARKERS or n.endswith(PROJECT_MARKER_SUFFIXES) for n in folder.names)


def _sequence(names: Sequence[str]) -> Optional[Tuple[int, str]]:
    """(how many, an example name) for the longest run of pictures named alike with a number, if long enough."""
    groups: Dict[Tuple[str, str], List[str]] = {}
    for name in names:
        stem, dot, ext = name.rpartition(".")
        found = _NUMBERED.match(stem if dot else name)
        if found and found.group(1).lower().strip(" _-.()") not in CAMERA_STEMS:
            groups.setdefault((found.group(1).lower(), ext.lower()), []).append(name)
    if not groups:
        return None
    best = max(groups.values(), key=len)
    return (len(best), sorted(best)[0]) if len(best) >= SEQUENCE_MIN_FILES else None


def _one_place(key: str) -> str:
    """The pattern for one particular folder: its path, with a slash in front when it sits at the top of the root."""
    return key if "/" in key else "/" + key


def _judge(key: str, folder: _Folder, parent: Optional[_Folder]) -> Optional[Tuple[str, str, str]]:
    """(pattern, group, reason) when this folder is worth offering, else None. A bare name is a pattern
    that applies wherever that name appears; a path applies to this one folder."""
    name = key.rsplit("/", 1)[-1]
    lowered = name.lower()
    for marker, what in ENVIRONMENT_MARKERS.items():
        if marker in folder.names:
            return _one_place(key), SAFE, f"downloaded requirements: {what}"
    for child, what in ENVIRONMENT_CHILD_FOLDERS.items():
        if child in folder.children:
            return _one_place(key), SAFE, f"downloaded requirements: {what}"
    if lowered in DEPENDENCY_NAMES:
        return name, SAFE, f"downloaded requirements: {DEPENDENCY_NAMES[lowered]}"
    if lowered in TOOL_NAMES:
        return name, SAFE, TOOL_NAMES[lowered]
    if lowered in TRASH_NAMES:
        return _one_place(key), REVIEW, "looks like trash or temporary files"
    if lowered in BACKUP_NAMES:
        return _one_place(key), REVIEW, "looks like backups (copies of files that may be indexed elsewhere)"
    if lowered in BUILD_NAMES and parent is not None and _has_project_marker(parent):
        return _one_place(key), REVIEW, "build output of a code project"
    if folder.files >= UNREADABLE_MIN_FILES and folder.files - folder.readable >= UNREADABLE_FRACTION * folder.files:
        common = ", ".join(ext for ext, _ in folder.extensions.most_common(3))
        return _one_place(key), REVIEW, f"almost nothing here can be read (mostly {common})"
    run = _sequence(folder.pictures)
    if run is not None and run[0] >= 0.8 * max(folder.files, 1):
        return _one_place(key), REVIEW, f"{run[0]:,} numbered pictures in a row (such as {run[1]})"
    return None


def _judge_safe(key: str, folder: _Folder) -> Optional[Tuple[str, str, str]]:
    verdict = _judge(key, folder, None)
    return verdict if verdict is not None and verdict[1] == SAFE else None


def _walk(folders: Dict[str, _Folder], judge, skip: Sequence[str] = ()) -> List[Tuple[str, Tuple[str, str, str]]]:
    """Folders with a verdict, top-down. Nothing inside a folder that already has one, or inside `skip`, is judged."""
    covered = list(skip)
    out = []
    for key in sorted((k for k in folders if k), key=lambda k: (k.count("/"), k.lower())):
        if any(key == top or key.startswith(top + "/") for top in covered):
            continue
        verdict = judge(key)
        if verdict is not None:
            covered.append(key)
            out.append((key, verdict))
    return out


def safe_folders(root: str, files: Iterable[Tuple[str, int]],
                 extensions: Optional[Mapping[str, FileKind]] = None) -> List[str]:
    """The folders below root that are safe to skip, as paths below the root with "/" (the first pass of
    suggest_excludes on its own)."""
    everything = _tree(root, list(files), extensions)
    return [key for key, _ in _walk(everything, lambda key: _judge_safe(key, everything[key]))]


def safe_skips(root: str, files: Iterable[Tuple[str, int]],
               extensions: Optional[Mapping[str, FileKind]] = None) -> Tuple[List[str], List[str]]:
    """(the folders that are safe to skip, the skip rules that leave them all out), from one pass over the
    listing. The rules are what `suggest_excludes` would offer in its safe group; this is the quick way to
    them for a listing as large as a whole drive."""
    everything = _tree(root, list(files), extensions)
    found = _walk(everything, lambda key: _judge_safe(key, everything[key]))
    spelling: Dict[str, str] = {}
    for _, (pattern, _group, _reason) in found:
        spelling.setdefault(pattern.casefold(), pattern)
    return [key for key, _ in found], list(spelling.values())


def suggest_excludes(root: str, files: Iterable[Tuple[str, int]],
                     extensions: Optional[Mapping[str, FileKind]] = None) -> List[Suggestion]:
    """Candidates for --exclude, the safest and largest first. `files` is (path, size) for every file below root.

    Two passes. First the safe folders are found wherever they are. Then everything else is judged as if
    those folders were already gone: a project is not "almost all unreadable" because of the environment
    inside it, and a safe folder is never hidden under a broader "your call" suggestion.

    A folder inside a folder that is already offered is not offered again, and the root itself never is.
    A suggestion for one particular folder is written as a path; at the top of the root it starts with a
    slash ("/Trash"), so that accepting it does not also skip every other folder called Trash.
    """
    files = list(files)
    everything = _tree(root, files, extensions)
    safe = _walk(everything, lambda key: _judge_safe(key, everything[key]))
    safe_keys = [key for key, _ in safe]

    base = root.rstrip("/\\")
    def outside_safe(path: str) -> bool:
        relative = path[len(base) + 1:].replace("\\", "/")
        return not any(relative.startswith(key + "/") for key in safe_keys)

    rest = _tree(root, (f for f in files if outside_safe(f[0])), extensions) if safe_keys else everything

    def judge_rest(key: str) -> Optional[Tuple[str, str, str]]:
        parent_key = key.rsplit("/", 1)[0] if "/" in key else ""
        verdict = _judge(key, rest[key], rest.get(parent_key))
        return verdict if verdict is not None and verdict[1] != SAFE else None

    review = _walk(rest, judge_rest, skip=safe_keys)

    found: Dict[Tuple[str, str, str], List[Tuple[str, _Folder]]] = {}
    spelling: Dict[str, str] = {}
    for key, (pattern, group, reason) in [(k, v) for k, v in safe] + [(k, v) for k, v in review]:
        folder = everything[key] if group == SAFE else rest[key]
        spelling.setdefault(pattern.casefold(), pattern)     # patterns ignore case: .git and .GIT are one
        found.setdefault((pattern.casefold(), group, reason), []).append((key, folder))
    out = []
    for (folded, group, reason), places in found.items():
        out.append(Suggestion(spelling[folded], group, reason, len(places), sum(f.files for _, f in places),
                              sum(f.bytes for _, f in places), sum(f.readable for _, f in places), places[0][0]))
    out.sort(key=lambda s: (s.group != SAFE, -s.files, s.pattern.lower()))
    return out
