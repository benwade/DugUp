"""Naming pictures by what they show: a pattern the person edits, filled from a sentence the captioning
model writes about each picture and from what the index and the file already know; a preview first, a
record of every change, and an undo.

Fields in a pattern: {caption} (the sentence, made safe for a file name), {taken} (the day the photo was
taken, from the camera's own record when there is one, else the file's modified day), {time} (hh-mm of the
same), {camera}, {folder} (the folder's own name), {original} (the name it has now, without its ending),
{n} (a running number, added by itself when two files would get the same name), {ext}. The ending is kept
as it is unless the pattern names {ext} itself.

Nothing here opens a file for writing: renaming is os.rename, which refuses to overwrite, and the index
follows each change in the same step (store.move_file). The record file lists old and new paths, tab apart;
`undo(record)` renames them back, last first.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .shapes import Failure, FailureReason, FileKind, FileRecord

DEFAULT_PATTERN = "{caption} {taken}"
FIELDS = ("caption", "taken", "time", "camera", "folder", "original", "n", "ext")
MAX_CAPTION_WORDS = 8
MAX_NAME = 120          # characters, without the ending: well under what any file system allows for a path
_BAD = set('<>:"/\\|?*') | {chr(c) for c in range(32)}
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
_FILLER = {"a", "an", "the"}          # dropped anywhere; the sentence otherwise reads as written
_OPENERS = ("there is", "there are", "this is", "an image of", "a picture of", "a photo of", "a close up of", "a close-up of")


@dataclass
class Change:
    record: FileRecord
    old: str                       # the path now
    new: str                       # the path it would get ("" when it is left alone)
    caption: str = ""              # the sentence the model wrote, if any
    why: str = ""                  # why it is left alone, when it is


@dataclass
class Plan:
    pattern: str
    changes: List[Change] = field(default_factory=list)
    captioned: int = 0
    device: str = ""
    seconds: float = 0.0
    stopped: bool = False

    @property
    def to_rename(self) -> List[Change]:
        return [c for c in self.changes if c.new]

    @property
    def left_alone(self) -> List[Change]:
        return [c for c in self.changes if not c.new]


def check_pattern(pattern: str) -> str:
    """"" when the pattern is usable, else what is wrong with it."""
    names = re.findall(r"\{([a-z_]+)\}", pattern)
    unknown = [n for n in names if n not in FIELDS]
    if unknown:
        return f"unknown field(s) {', '.join('{' + n + '}' for n in unknown)}; the fields are {', '.join('{' + f + '}' for f in FIELDS)}"
    if not names:
        return "the pattern names no field, so every file would get the same name"
    bad = [ch for ch in re.sub(r"\{[a-z_]+\}", "", pattern) if ch in _BAD]
    if bad:
        return "the pattern holds a character a file name cannot: " + " ".join(sorted(set(bad)))
    return ""


def safe_words(text: str, most: int = MAX_CAPTION_WORDS) -> str:
    """A sentence as a file-name-safe phrase: letters, digits and a few marks, "a" and "the" dropped and an
    opener such as "there is" or "a photo of" taken off the front, at most `most` words, the first capitalised."""
    lowered = text.strip().lower()
    for opener in _OPENERS:
        if lowered.startswith(opener + " "):
            text = text.strip()[len(opener):].strip()
            break
    words = re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", text, flags=re.UNICODE)
    kept = [w for w in words if w.lower() not in _FILLER]
    kept = (kept or words)[:most]
    phrase = " ".join(w.replace("’", "'") for w in kept)
    return phrase[:1].upper() + phrase[1:] if phrase else ""


def safe_name(name: str) -> str:
    cleaned = "".join(" " if ch in _BAD else ch for ch in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if cleaned.lower() in _RESERVED:
        cleaned = cleaned + "_"
    return cleaned[:MAX_NAME].rstrip(" .") or "picture"


def picture_facts(path: str) -> Dict[str, str]:
    """{taken, time, camera} from the picture's own record (EXIF), where it has one; "" otherwise."""
    facts = {"taken": "", "time": "", "camera": ""}
    try:
        from PIL import Image
        with Image.open(path) as picture:
            exif = picture.getexif()
            stamp = ""
            try:
                stamp = str(exif.get_ifd(0x8769).get(36867) or "")          # DateTimeOriginal
            except Exception:
                stamp = ""
            stamp = stamp or str(exif.get(306) or "")                        # DateTime
            match = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2})", stamp)
            if match:
                y, mo, d, h, mi = match.groups()
                facts["taken"], facts["time"] = f"{y}-{mo}-{d}", f"{h}-{mi}"
            make, model = str(exif.get(271) or "").strip(), str(exif.get(272) or "").strip()
            facts["camera"] = safe_name((model if make.lower() in model.lower() else f"{make} {model}").strip())
    except Exception:
        pass
    return facts


def fill(pattern: str, fields: Dict[str, str]) -> str:
    def one(match):
        return fields.get(match.group(1), "")
    return re.sub(r"\{([a-z_]+)\}", one, pattern)


def plan(records: Sequence[FileRecord], pattern: str, captions: Dict[str, str], clock: Callable[[float], str],
         protected: Sequence[str] = (), fold: Callable[[str], str] = lambda p: p) -> List[Change]:
    """What each picture would be called. `captions` maps path -> sentence ("" when none). `clock(modified)`
    gives the day as YYYY-MM-DD HH-MM for files with no record of their own. Protected folders and files
    whose new name would be the old one are left alone, with a reason."""
    changes: List[Change] = []
    taken: Dict[str, int] = {}           # new path (folded) -> how many want it
    for record in records:
        old = record.path
        folder = os.path.dirname(old)
        if any(fold(old).startswith(fold(p.rstrip("/\\")) + sep) for p in protected for sep in ("/", "\\")):
            changes.append(Change(record, old, "", captions.get(old, ""), "in a protected folder"))
            continue
        base, ext = os.path.splitext(os.path.basename(old))
        facts = picture_facts(old) if "{taken}" in pattern or "{time}" in pattern or "{camera}" in pattern else {}
        when = clock(record.modified)
        fields = {
            "caption": safe_words(captions.get(old, "")),
            "taken": facts.get("taken") or when[:10],
            "time": facts.get("time") or when[11:16],
            "camera": facts.get("camera", ""),
            "folder": safe_name(os.path.basename(folder)),
            "original": base,
            "n": "",
            "ext": ext.lstrip("."),
        }
        if "{caption}" in pattern and not fields["caption"]:
            changes.append(Change(record, old, "", "", "no caption could be made for it"))
            continue
        name = safe_name(fill(pattern, fields))
        if "{ext}" not in pattern:
            new = os.path.join(folder, name + ext)
        else:
            new = os.path.join(folder, name)
        if fold(new) == fold(old):
            changes.append(Change(record, old, "", captions.get(old, ""), "it has that name already"))
            continue
        changes.append(Change(record, old, new, captions.get(old, "")))
    # two files that want the same name: each after the first gets a number, as {n} or at the end
    wanted: Dict[str, List[Change]] = {}
    for change in changes:
        if change.new:
            wanted.setdefault(fold(change.new), []).append(change)
    existing = {fold(c.old) for c in changes}
    for key, group in wanted.items():
        clash = len(group) > 1 or (os.path.lexists(group[0].new) and fold(group[0].new) not in {fold(g.old) for g in group})
        if not clash:
            continue
        for number, change in enumerate(group, 1):
            change.new = _numbered(change.new, number, pattern)
            while os.path.lexists(change.new) and fold(change.new) != fold(change.old):
                number += 1
                change.new = _numbered(_unnumbered(change.new, pattern), number, pattern)
    return changes


def _numbered(path: str, number: int, pattern: str) -> str:
    folder, name = os.path.split(path)
    base, ext = os.path.splitext(name)
    return os.path.join(folder, f"{base} ({number}){ext}")


def _unnumbered(path: str, pattern: str) -> str:
    folder, name = os.path.split(path)
    base, ext = os.path.splitext(name)
    return os.path.join(folder, re.sub(r" \(\d+\)$", "", base) + ext)


@dataclass
class Applied:
    renamed: List[Tuple[str, str]] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)      # (old path, why)
    stopped: bool = False


def apply(changes: Sequence[Change], store, journal: Optional[Callable[[str], None]] = None, stop=None,
          renamer: Callable[[str, str], None] = os.rename) -> Applied:
    """Rename each file whose plan says so, one at a time, only if it is still there, unchanged in size and
    time since the plan, and the new name is still free. The index follows each rename in the same step.
    `journal(line)` is told of each outcome as it happens."""
    done = Applied()
    for change in changes:
        if stop is not None and stop.is_set():
            done.stopped = True
            break
        if not change.new:
            continue
        why = ""
        try:
            now = os.stat(change.old)
            if now.st_size != change.record.size or abs(now.st_mtime - change.record.modified) > 2.0:
                why = "it changed since the preview"
        except FileNotFoundError:
            why = "it is not there now"
        except OSError as exc:
            why = f"it could not be looked at ({type(exc).__name__})"
        if not why and os.path.lexists(change.new):
            why = "a file with the new name is there now"
        if not why:
            try:
                renamer(change.old, change.new)
            except OSError as exc:
                why = f"the rename was refused ({type(exc).__name__}: {exc})"
        if why:
            done.skipped.append((change.old, why))
            if journal is not None:
                journal("\t".join(("left", change.old, why)))
            continue
        try:
            store.move_file(change.old, change.new)
        except Exception:
            pass          # the next index run sees the new name; the file itself is already renamed
        done.renamed.append((change.old, change.new))
        if journal is not None:
            journal("\t".join(("renamed", change.old, "to", change.new)))
    return done


def read_record(text: str) -> List[Tuple[str, str]]:
    """(old, new) pairs from a record file's lines that say "renamed"."""
    pairs = []
    for line in text.splitlines():
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) >= 4 and parts[0] == "renamed" and parts[2] == "to":
            pairs.append((parts[1], parts[3]))
    return pairs


def undo(pairs: Sequence[Tuple[str, str]], store, journal: Optional[Callable[[str], None]] = None,
         renamer: Callable[[str, str], None] = os.rename) -> Applied:
    """Rename back, last first. A file that is not at its new name, or whose old name is taken, is left."""
    done = Applied()
    for old, new in reversed(list(pairs)):
        why = ""
        if not os.path.lexists(new):
            why = "it is not at its new name now"
        elif os.path.lexists(old):
            why = "a file with the old name is there now"
        else:
            try:
                renamer(new, old)
            except OSError as exc:
                why = f"the rename was refused ({type(exc).__name__}: {exc})"
        if why:
            done.skipped.append((new, why))
            if journal is not None:
                journal("\t".join(("left", new, why)))
            continue
        try:
            store.move_file(new, old)
        except Exception:
            pass
        done.renamed.append((new, old))
        if journal is not None:
            journal("\t".join(("renamed", new, "to", old)))
    return done
