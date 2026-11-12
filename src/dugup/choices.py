"""Folder choices in a file a person can read and edit: which folders are indexed, what is read in each, and
what is left out (Ben, 2026-10-28). The choices otherwise live only inside the index, so they go when it goes.

The file is plain text in the TOML form config.toml uses, with notes at the top saying what it is and how to
edit it. Paths are written in single quotes, so a Windows backslash needs no doubling.

People will edit it, and some edits will be wrong. So reading it never stops at the first problem: it finds
every one it can, says where and what would fix it (a path in double quotes, a misspelt key, a word "read"
does not know), and nothing is applied until there are none. A folder that is not on this computer is not a
problem: it is listed and left out, so a file from another computer still loads what fits.

Pure: text in, text or plans out. The Service applies a plan (Service.preview_choices, apply_choices).
"""

from __future__ import annotations

import datetime
import difflib
import json
import ntpath
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .shapes import (LEVEL_MEANING, LEVEL_NAMES, OPTION_ALIKE, OPTION_MEANING, OPTION_OCR, OPTION_WORDS, OPTIONS,
                     POLICY_NAMES_ONLY, POLICY_OPEN, Root, level_of, options_of)

FILE_NAME = "DugUp folder choices.txt"
FOLDER_KEYS = ("path", "read", "exclude", "only", "inside")
OLD_KEYS = {"skip": "exclude"}      # the first files said "skip" (Ben, 2026-10-28: "exclude" says it plainly)

# What "read" is written with, and every other way a person may well write it (case and spaces do not matter).
WORD_OF = {OPTION_WORDS: "words", OPTION_OCR: "scans", OPTION_MEANING: "meaning", OPTION_ALIKE: "pictures alike"}
READ_WORDS: Dict[str, str] = {
    "names": LEVEL_NAMES, "name": LEVEL_NAMES, "names only": LEVEL_NAMES, "file names": LEVEL_NAMES,
    "file names only": LEVEL_NAMES, "nothing": LEVEL_NAMES,
    "everything": "all", "all": "all",
    "words": OPTION_WORDS, "word": OPTION_WORDS, "text": OPTION_WORDS, "documents": OPTION_WORDS,
    "words in documents": OPTION_WORDS,
    "scans": OPTION_OCR, "scan": OPTION_OCR, "ocr": OPTION_OCR, "words in pictures": OPTION_OCR,
    "words in pictures and scans": OPTION_OCR,
    "meaning": OPTION_MEANING,
    "pictures alike": OPTION_ALIKE, "alike": OPTION_ALIKE, "pictures": OPTION_ALIKE, "looks": OPTION_ALIKE,
    "pictures by looks": OPTION_ALIKE,
}
READ_HELP = ('file names only, words, words + scans, everything; add "+ meaning" or "+ pictures alike" '
             "as needed")

HEADER = """\
# DugUp folder choices: which folders are indexed, what is read in each, and what is left out.
# Saved {when} from {source}
#
# Why keep this file: your folder choices live inside the index. If the index is deleted or rebuilt, or you
# move to a new computer, load this file (File > Load folder choices...) and every folder is set up again as
# it was. Folders that are not on that computer are listed and left out; the rest still load.
#
# Edit it in any text editor. Lines starting with # are notes. Each folder starts with [[folder]].
#   path    the folder, in single quotes: 'C:\\Users\\you\\Documents' (single quotes keep the backslashes as typed)
#   read    what is read: {read_help}
#   exclude what to leave out: "/Codex" is that folder only, ".git" is any folder with that name
#   only    (rarely used) read only files that match these, such as "*.pdf"
#   inside  a folder below with a "read" of its own; everything else follows the folder above it
#
# Loading shows what will change and asks first. If something in the file is wrong, nothing is changed and
# DugUp says what to fix.
"""


# ---- writing ----------------------------------------------------------------------------------------------------

def read_words(level: str) -> str:
    """A level as it is written in the file: "file names only", "everything", or its parts joined by " + ".
    Ben, 2026-10-28: "names" alone left a reader guessing; "file names only" says it."""
    chosen = options_of(level)
    if not chosen:
        return "file names only"
    if chosen == set(OPTIONS):
        return "everything"
    return " + ".join(WORD_OF[o] for o in OPTIONS if o in chosen)


def _quote(text: str) -> str:
    """A TOML string: a literal one ('...', backslashes as they are) unless the text holds a ' or a line break."""
    if "'" not in text and "\n" not in text and "\r" not in text:
        return f"'{text}'"
    return json.dumps(text, ensure_ascii=False)


def _list(items: Sequence[str]) -> str:
    return "[" + ", ".join(_quote(i) for i in items) + "]"


def dump(roots: Sequence[Root], source: str, now: Optional[datetime.datetime] = None) -> str:
    """The choices of these folders as the file's text."""
    when = (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = [HEADER.format(when=when, source=source, read_help=READ_HELP)]
    for root in roots:
        own = dict(root.levels)
        base = LEVEL_NAMES if root.policy == POLICY_NAMES_ONLY else own.get("", LEVEL_MEANING)
        lines.append("[[folder]]")
        lines.append(f"path = {_quote(root.path)}")
        lines.append(f"read = {_quote(read_words(base))}")
        if root.exclude:
            lines.append(f"exclude = {_list(root.exclude)}")
        if root.include:
            lines.append(f"only = {_list(root.include)}")
        inside = [(folder, level) for folder, level in root.levels if folder]
        if inside:
            lines.append("")
            lines.append("[folder.inside]")
            lines += [f"{_quote(folder)} = {_quote(read_words(level))}" for folder, level in inside]
        lines.append("")
    return "\n".join(lines)


# ---- reading ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    """One folder as the file asks for it."""

    root: Root
    shown: str          # how to name it in a message: its path as written


def decode(data: bytes) -> str:
    """The file's text, whatever Notepad saved it as: UTF-8 (with or without a mark) or UTF-16."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")  # an old editor's default: read it, do not refuse it
    return text.lstrip("\ufeff")


def parse_read(value: object) -> Optional[str]:
    """A "read" value as a level, or None when it holds a word that is not a choice."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = [p.strip() for p in re.split(r"[+,&]|\band\b", value.casefold()) if p.strip()]
    chosen = set()
    for part in parts:
        part = " ".join(part.split())
        meant = READ_WORDS.get(part)
        if meant is None:
            return None
        if meant == LEVEL_NAMES:
            if len(parts) > 1:
                return None          # "names + words" says two things at once
            return LEVEL_NAMES
        chosen |= set(OPTIONS) if meant == "all" else {meant}
    return level_of(chosen)


def _suggest(word: str, known: Sequence[str]) -> str:
    near = difflib.get_close_matches(word.casefold(), [k.casefold() for k in known], n=1, cutoff=0.6)
    return f' (did you mean "{near[0]}"?)' if near else ""


def _is_absolute(path: str) -> bool:
    return os.path.isabs(path) or ntpath.isabs(path) and bool(ntpath.splitdrive(path)[0])


def _toml_problem(error: Exception, text: str) -> str:
    """The parser's complaint, with the line it was on and the likely fix for the usual slips."""
    message = str(error)
    found = re.search(r"line (\d+)", message)
    line_no = int(found.group(1)) if found else None
    lines = text.splitlines()
    line = lines[line_no - 1] if line_no and 0 < line_no <= len(lines) else ""
    hint = ""
    if '"' in line and "\\" in line or "escape" in message.casefold():
        hint = (" A Windows path in double quotes needs every backslash doubled; put it in single quotes "
                "instead: 'C:\\Users\\you\\Documents'.")
    elif "folder" in line and line.strip().startswith("[") and not line.strip().startswith("[["):
        hint = " Each folder starts with [[folder]], with two brackets on each side."
    elif line.count("'") % 2 or line.count('"') % 2 or re.search(r"unterminated|unclosed|expected", message, re.IGNORECASE):
        hint = " A quote or a bracket may be missing at the end of the line."
    where = f" Line {line_no}: {line.strip()}" if line.strip() else ""
    return f"The file could not be read as written ({message}).{where}{hint}"


def parse(text: str, platform_paths: Callable[[str], str] = os.path.abspath,
          fold: Callable[[str], str] = os.path.normcase) -> Tuple[List[Entry], List[str]]:
    """The folders the file asks for, and every problem found in it. Any problem means nothing is applied."""
    try:
        import tomllib as toml          # type: ignore[import-not-found]
    except ImportError:                 # Python 3.10
        import tomli as toml            # type: ignore[no-redef]
    try:
        data = toml.loads(text)
    except Exception as error:          # TOMLDecodeError, and anything a damaged file may bring
        return [], [_toml_problem(error, text)]
    problems: List[str] = []
    for key in data:
        if key != "folder":
            problems.append(f'"{key}" is not something this file can hold{_suggest(key, ["folder"])}; '
                            "each folder starts with [[folder]].")
    folders = data.get("folder", [])
    if isinstance(folders, dict):        # [folder] with one bracket, for a single folder: what it plainly means
        folders = [folders]
    if not isinstance(folders, list) or not folders:
        if not problems:
            problems.append("The file names no folder. Each folder starts with [[folder]] and a path = '...' line.")
        return [], problems
    entries: List[Entry] = []
    seen: Dict[str, str] = {}
    for number, item in enumerate(folders, 1):
        entry = _folder(number, item, problems, platform_paths)
        if entry is None:
            continue
        key = fold(entry.root.path)
        if key in seen:
            problems.append(f"{entry.shown} is in the file twice; keep one of them.")
            continue
        seen[key] = entry.root.path
        entries.append(entry)
    for a in entries:                    # one file belongs to one folder: no folder inside another
        for b in entries:
            if a is not b and _under(fold(b.root.path), fold(a.root.path)):
                problems.append(f"{b.shown} lies inside {a.shown}. A folder below another belongs under its "
                                "[folder.inside] or exclude, not in a [[folder]] of its own.")
    return entries, problems


def _under(path: str, root: str) -> bool:
    root = root.rstrip("\\/")
    return path != root and (path.startswith(root + "\\") or path.startswith(root + "/"))


def _strings(value: object, what: str, shown: str, problems: List[str]) -> Tuple[str, ...]:
    if isinstance(value, str):           # exclude = ".git" for exclude = [".git"]: plainly meant
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f'{shown}: {what} should be a list in brackets, such as {what} = [".git", "/Old"].')
        return ()
    return tuple(v.strip() for v in value if v.strip())


def _folder(number: int, item: object, problems: List[str], platform_paths) -> Optional[Entry]:
    label = f"Folder {number}"
    if not isinstance(item, dict):
        problems.append(f"{label} is not written as a folder; it should start with [[folder]].")
        return None
    raw = item.get("path")
    shown = f"{label} ({raw})" if isinstance(raw, str) and raw.strip() else label
    for key in item:
        if key not in FOLDER_KEYS and key not in OLD_KEYS:
            problems.append(f'{shown}: "{key}" is not a choice{_suggest(key, FOLDER_KEYS)}. '
                            f"A folder can have: {', '.join(FOLDER_KEYS)}.")
    if not isinstance(raw, str) or not raw.strip():
        problems.append(f"{label} has no path. Add a line such as path = 'C:\\Users\\you\\Documents'.")
        return None
    raw = raw.strip()
    if not _is_absolute(raw):
        problems.append(f"{shown}: the path must be a whole path, starting with a drive such as C:\\ (or / on Linux).")
        return None
    path = platform_paths(raw)
    read = item.get("read", "everything")
    base = parse_read(read)
    if base is None:
        problems.append(f'{shown}: read = "{read}" is not a choice. Use {READ_HELP}.')
        base = LEVEL_MEANING
    excluded = _strings(item.get("exclude", []), "exclude", shown, problems)
    if "skip" in item:                   # a file saved before "exclude": read as it was meant
        excluded += _strings(item.get("skip", []), "skip", shown, problems)
    only = _strings(item.get("only", []), "only", shown, problems)
    levels: List[Tuple[str, str]] = []
    if base not in (LEVEL_MEANING, LEVEL_NAMES):
        levels.append(("", base))
    inside = item.get("inside", {})
    if not isinstance(inside, dict):
        problems.append(f"{shown}: inside should be a table under [folder.inside], one folder = \"what to read\" a line.")
        inside = {}
    for folder, value in inside.items():
        rel = _relative(folder, raw)
        if rel is None:
            problems.append(f'{shown}: "{folder}" under inside is not a folder within {raw}. '
                            "Write it as it is below the folder, such as \"Screenshots\" or \"Photos/2024\".")
            continue
        level = parse_read(value)
        if level is None:
            problems.append(f'{shown}: "{folder}" = "{value}" is not a choice. Use {READ_HELP}.')
            continue
        levels.append((rel, level))
    policy = POLICY_NAMES_ONLY if base == LEVEL_NAMES else POLICY_OPEN
    return Entry(Root(path, policy, only, excluded, levels=tuple(levels)), shown)


def _relative(folder: str, root: str) -> Optional[str]:
    """A folder under "inside" as it is below the root: slashes either way, leading ones dropped, and a whole
    path inside the root made relative to it."""
    text = folder.strip().replace("\\", "/")
    base = root.replace("\\", "/").rstrip("/")
    if _is_absolute(folder.strip()):
        if text.casefold().startswith(base.casefold() + "/"):
            text = text[len(base) + 1:]
        else:
            return None
    text = text.strip("/")
    if not text or text.startswith("..") or "/../" in f"/{text}/":
        return None
    return text


# ---- what loading would change ----------------------------------------------------------------------------------

@dataclass
class Preview:
    """What loading the file would do, before anything is done."""

    new: List[Root] = field(default_factory=list)
    changed: List[Tuple[Root, Root, List[str]]] = field(default_factory=list)   # (now, then, what changes)
    same: List[Root] = field(default_factory=list)
    not_here: List[str] = field(default_factory=list)         # in the file, not on this computer: left out
    untouched: List[str] = field(default_factory=list)        # indexed now, not in the file: left as they are
    problems: List[str] = field(default_factory=list)

    @property
    def nothing_to_do(self) -> bool:
        return not self.new and not self.changed

    def lines(self) -> List[str]:
        out: List[str] = []
        for root in self.new:
            out.append(f"Add {root.path} (read: {read_words(_base(root))})")
        for now, then, what in self.changed:
            out.append(f"Change {now.path}: " + "; ".join(what))
        if self.same:
            out.append(f"{len(self.same)} folder(s) already as the file says: no change")
        for path in self.not_here:
            out.append(f"Left out, not on this computer: {path}")
        for path in self.untouched:
            out.append(f"Left as it is (not in the file): {path}")
        return out


def _base(root: Root) -> str:
    return LEVEL_NAMES if root.policy == POLICY_NAMES_ONLY else dict(root.levels).get("", LEVEL_MEANING)


def differences(now: Root, then: Root) -> List[str]:
    """What loading changes in a folder, in plain words."""
    what: List[str] = []
    if _base(now) != _base(then):
        what.append(f"read {read_words(_base(now))} -> {read_words(_base(then))}")
    added = [r for r in then.exclude if r.casefold() not in {x.casefold() for x in now.exclude}]
    dropped = [r for r in now.exclude if r.casefold() not in {x.casefold() for x in then.exclude}]
    if added:
        what.append("also exclude " + ", ".join(added))
    if dropped:
        what.append("no longer exclude " + ", ".join(dropped))
    if tuple(now.include) != tuple(then.include):
        what.append("only: " + (", ".join(then.include) or "every file"))
    inside_now = {f: l for f, l in now.levels if f}
    inside_then = {f: l for f, l in then.levels if f}
    for folder in sorted(set(inside_now) | set(inside_then), key=str.casefold):
        a, b = inside_now.get(folder), inside_then.get(folder)
        if a != b:
            what.append(f"{folder}: " + (f"read {read_words(b)}" if b else "follows the folder above"))
    return what


def preview(entries: Sequence[Entry], current: Sequence[Root], exists: Callable[[str], bool],
            fold: Callable[[str], str] = os.path.normcase) -> Preview:
    """Set what the file asks for against what is indexed now."""
    plan = Preview()
    known = {fold(r.path): r for r in current}
    asked = set()
    for entry in entries:
        key = fold(entry.root.path)
        asked.add(key)
        old = known.get(key)
        if old is not None:
            then = Root(old.path, entry.root.policy, entry.root.include, entry.root.exclude, levels=entry.root.levels)
            what = differences(old, then)
            (plan.changed.append((old, then, what)) if what else plan.same.append(old))
        elif not exists(entry.root.path):
            plan.not_here.append(entry.root.path)
        else:
            plan.new.append(entry.root)
    plan.untouched = [r.path for r in current if fold(r.path) not in asked]
    return plan
