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
MAX_CAPTION_WORDS = 10
_TRAILING = {"a", "an", "the", "of", "in", "on", "at", "to", "with", "and", "or", "for", "by", "from", "near", "next", "over",
             "under", "into", "onto", "front", "top", "side", "is", "are", "its", "it", "their", "his", "her", "that", "which",
             "while", "as", "up", "down", "out", "off", "through", "behind", "beside", "between", "above", "below", "some", "two",
             "three", "four", "several", "many", "one", "holding", "wearing", "sitting", "standing",
             "body", "piece", "pair", "group", "couple", "bunch", "lot", "kind", "type", "sort", "number", "variety",
             "series", "stack", "pile", "row", "set"}      # nouns that need their "of ..." to mean anything
# a name a camera or a phone gave, which says nothing about the picture
MEANINGLESS = re.compile(
    r"^(?:img|dsc|dscn|dscf|dcim|pxl|mvimg|p|pic|picture|image|photo|snapshot|scan|screenshot|screen shot|capture|"
    r"fb_img|received|signal|whatsapp image|snapchat|vid|mov|clip|untitled|new image|unnamed|download|"
    r"\d{4}-?\d{2}-?\d{2}|\d{8}|\d{6}|[0-9a-f]{16,}|[A-Z]{2,4}\d{3,}|\d{3,}|wp_\d+|gopr\d+|sam_\d+)"
    r"(?:[-_ ]?\d[\d_\-: .()]*)?(?:\s*\(\d+\))?(?:[-_ ]?(?:copy|edit|edited|hdr|burst|\d+))*$",
    re.IGNORECASE)


def is_meaningless(name: str) -> bool:
    """Whether a file's name (without its ending) is the kind a camera or a phone gives: a prefix such as
    IMG, DSC or Screenshot with numbers, or numbers alone, with nothing a person wrote in it."""
    base = os.path.splitext(name)[0].strip()
    if not base:
        return True
    if MEANINGLESS.match(base):
        return True
    letters = re.findall(r"[^\W\d_]{3,}", base)
    words = [w for w in letters if w.lower() not in ("img", "dsc", "pic", "photo", "image", "screenshot", "shot", "copy", "edit",
                                                      "hdr", "burst", "scan", "capture", "snapshot", "picture", "untitled")]
    return not words
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
    identity: Optional[Tuple] = None   # seen_identity(old) at the preview; None: not looked at then


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
    kept = (kept or words)
    if len(kept) > most:
        cut = most
        while cut < len(kept) and cut < most + 2 and kept[cut - 1].lower() in _TRAILING:
            cut += 1                       # a cut on "in front of": a word or two more finishes the phrase ...
        kept = kept[:cut]
        while len(kept) > 2 and kept[-1].lower() in _TRAILING:
            kept.pop()                     # ... or, when it does not, back off to the last whole phrase
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
         protected: Sequence[str] = (), fold: Callable[[str], str] = lambda p: p, every: bool = False) -> List[Change]:
    """What each picture would be called. `captions` maps path -> sentence ("" when none). `clock(modified)`
    gives the day as YYYY-MM-DD HH-MM for files with no record of their own. Protected folders, files whose
    new name would be the old one, and (unless `every`) files whose name already says something are left
    alone, with a reason."""
    changes: List[Change] = []
    for record in records:
        old = record.path
        folder = os.path.dirname(old)
        if any(fold(old).startswith(fold(p.rstrip("/\\")) + sep) for p in protected for sep in ("/", "\\")):
            changes.append(Change(record, old, "", captions.get(old, ""), "in a protected folder"))
            continue
        if not every and not is_meaningless(os.path.basename(old)):
            changes.append(Change(record, old, "", captions.get(old, ""), "its name already says something"))
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
        changes.append(Change(record, old, new, captions.get(old, ""), identity=seen_identity(old)))
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
    index_problems: List[Tuple[str, str]] = field(default_factory=list)   # (path, note): renamed on disk, index not
    record_problem: str = ""        # the record failed: before a file (nothing more touched) or after (the change is in renamed)
    record: str = ""                # the record this operation wrote, when it opened one of its own (undo)


RENAME_NOREPLACE = 1


def rename_no_replace(old: str, new: str) -> None:
    """Rename `old` to `new`, never over a file that is there, as one atomic step where the system can.
    Windows: os.rename refuses an existing destination. Linux: renameat2 with RENAME_NOREPLACE (one call,
    atomic, on every common filesystem). Elsewhere, or where that call is not available: a hard link to the
    new name (which fails if the name is taken, atomically) followed by removing the old name. On a
    filesystem that allows neither, the last resort claims the name with an exclusive empty file first and
    then replaces it; that one has a window a competing writer could slip into, and is used only when the
    others cannot be. Raises OSError as os.rename does, with the destination left as it was."""
    if os.name == "nt":
        os.rename(old, new)
        return
    if _renameat2(old, new):
        return
    try:
        os.link(old, new)
    except FileExistsError:
        raise
    except OSError:
        _claim_then_replace(old, new)
        return
    os.unlink(old)


def _renameat2(old: str, new: str) -> bool:
    """True when the rename was done by renameat2(RENAME_NOREPLACE). False when that call is not there or
    not supported by the filesystem (nothing was changed). Raises OSError for a real refusal (the name taken)."""
    import ctypes
    import errno
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameat2
    except (OSError, AttributeError):
        return False
    fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    fn.restype = ctypes.c_int
    at_fdcwd = -100
    if fn(at_fdcwd, os.fsencode(old), at_fdcwd, os.fsencode(new), RENAME_NOREPLACE) == 0:
        return True
    code = ctypes.get_errno()
    if code in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
        return False
    raise OSError(code, os.strerror(code), old, None, new)


def _claim_then_replace(old: str, new: str) -> None:
    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        os.replace(old, new)
    except OSError:
        try:
            os.unlink(new)
        except OSError:
            pass
        raise


def identity_of(path: str) -> Tuple[int, int, int, int]:
    """(size, time in nanoseconds, device, file id) of the file at `path`, from one stat. The file id is the
    inode on POSIX and the volume file index on Windows (0 where the filesystem gives none): a different file
    put at the same path has a different id, however alike its size and time. Where there is no id, a
    fingerprint of the content (content_fingerprint) stands in for destructive decisions."""
    st = os.stat(path)
    return st.st_size, int(st.st_mtime_ns), int(getattr(st, "st_dev", 0) or 0), int(getattr(st, "st_ino", 0) or 0)


def content_fingerprint(path: str, limit: int = 4 * 1024 * 1024) -> str:
    """A hash of the file's first `limit` bytes and its size: the fallback identity on a filesystem that gives
    no file id. Raises OSError when the file cannot be read."""
    import hashlib
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while size < limit:
            piece = handle.read(min(65536, limit - size))
            if not piece:
                break
            h.update(piece)
            size += len(piece)
    return h.hexdigest()[:32]


def seen_identity(path: str) -> Optional[Tuple]:
    """What a preview keeps of a file so the act can check it is the same file: (size, time ns, device, file
    id, fingerprint-or-""). The fingerprint is taken only where the filesystem gives no id. None when the file
    cannot be looked at."""
    try:
        size, when, dev, ident = identity_of(path)
        mark = "" if ident else content_fingerprint(path)
        return (size, when, dev, ident, mark)
    except OSError:
        return None


MOMENT_NS = 2_000             # two microseconds: the index keeps the time as a float of seconds, this as nanoseconds


def same_moment(when_ns: int, seconds: float) -> bool:
    """Whether a time in nanoseconds and the index's float of seconds are the same moment (to a microsecond,
    which is where the two ways of writing a time stop agreeing; a file changed later differs by far more)."""
    return abs(when_ns - int(round(seconds * 1e9))) <= MOMENT_NS


def same_as_indexed(identity: Optional[Tuple], record) -> str:
    """"" when a file's identity on disk (seen_identity) is the file the index describes in `record` (size,
    time to the microsecond, and the file id when the index kept one), else why not. What the index holds
    about a file (its sketch, its text) is evidence about that file only."""
    if identity is None:
        return "it is not there now"
    if identity[0] != record.size:
        return "it changed since it was indexed (the size differs)"
    if not same_moment(identity[1], record.modified):
        return "it changed since it was indexed (the time differs)"
    kept = getattr(record, "file_id", None) or ""
    ident = identity[3] if len(identity) > 3 else 0
    if kept and ident:
        dev_text, _, ino_text = kept.rpartition(":")
        try:
            dev0, ino0 = (int(dev_text) if dev_text else 0), int(ino_text)
        except ValueError:
            return ""
        dev = identity[2] if len(identity) > 3 else 0
        if ino0 != ident or (dev0 and dev and dev0 != dev):
            return "it is not the file that was indexed (the file id differs)"
    return ""


def same_file(path: str, seen: Tuple, tolerance_ns: int = 0) -> str:
    """"" when the file at `path` is the file `seen` at the preview (seen_identity), else why not. Size and
    time must match (exactly, or within `tolerance_ns` for a record written at a coarser precision); the
    device and file id too when both sides have them; where neither has an id, the content fingerprint."""
    try:
        size, when, dev, ident = identity_of(path)
    except FileNotFoundError:
        return "it is not there now"
    except OSError as exc:
        return f"it could not be looked at ({type(exc).__name__})"
    size0, when0 = seen[0], seen[1]
    dev0, ident0 = (seen[2], seen[3]) if len(seen) > 3 else (0, seen[2] if len(seen) > 2 else 0)
    mark0 = seen[4] if len(seen) > 4 else ""
    if size != size0:
        return "it is not the file that was previewed (the size differs)"
    if when0 is not None and abs(when - when0) > tolerance_ns:
        return "it is not the file that was previewed (the time differs)"
    if ident0 and ident:
        if ident != ident0 or (dev0 and dev and dev != dev0):
            return "it is not the file that was previewed (the file id differs)"
    elif mark0:
        try:
            if content_fingerprint(path) != mark0:
                return "it is not the file that was previewed (the content differs)"
        except OSError as exc:
            return f"it could not be read ({type(exc).__name__})"
    return ""


def same_identity(now: Tuple, then: Tuple, tolerance_ns: int = 0) -> bool:
    """Whether a stat identity now matches one seen before (size, time within tolerance, ids when both known)."""
    size, when = now[0], now[1]
    ident = now[3] if len(now) > 3 else (now[2] if len(now) > 2 else 0)
    size0, when0 = then[0], then[1]
    ident0 = then[3] if len(then) > 3 else (then[2] if len(then) > 2 else 0)
    if size != size0:
        return False
    if when0 is not None and abs(when - when0) > tolerance_ns:
        return False
    if ident0 and ident and ident != ident0:
        return False
    return True


def apply(changes: Sequence[Change], store, journal: Optional[Callable[[str], None]] = None, stop=None,
          renamer: Callable[[str, str], None] = rename_no_replace, protected: Sequence[str] = (),
          fold: Callable[[str], str] = lambda p: p) -> Applied:
    """Rename each file whose plan says so, one at a time, only if it is still there, unchanged in size and
    time since the plan, not in a folder protected now, and the new name is still free. The index follows
    each rename in the same step. `journal(line)` is told, for each file, first what is about to happen
    ("renaming", with the file's size, time and id, so a crash between the lines leaves a record of the
    intent and of which file it was) and then what happened ("renamed" or "left"). A journal that cannot
    write the intent (records.RecordError) stops the run before that file is touched; one that cannot write
    an outcome stops it after, with the change reported in the result."""
    from .records import RecordError
    done = Applied()
    for change in changes:
        if stop is not None and stop.is_set():
            done.stopped = True
            break
        if not change.new:
            continue
        why = ""
        identity: Tuple = (change.record.size, 0, 0, 0)
        if any(fold(change.old).startswith(fold(f.rstrip("/\\")) + sep) for f in protected for sep in ("/", "\\")):
            why = "it is in a protected folder now"
        if not why:
            if change.identity is not None:
                why = same_file(change.old, change.identity)       # seen at the preview: the same file, now
            try:
                identity = identity_of(change.old)
                if not why and change.identity is None and (
                        identity[0] != change.record.size or not same_moment(identity[1], change.record.modified)):
                    why = "it changed since the preview"
            except FileNotFoundError:
                why = why or "it is not there now"
            except OSError as exc:
                why = why or f"it could not be looked at ({type(exc).__name__})"
        if not why and os.path.lexists(change.new):
            why = "a file with the new name is there now"
        if not why:
            if journal is not None:
                try:
                    journal("\t".join(("renaming", change.old, "to", change.new, str(identity[0]), str(identity[1]), str(identity[3]))))
                except RecordError as exc:
                    done.skipped.append((change.old, f"the record could not be written ({exc}); nothing more was renamed"))
                    done.record_problem, done.stopped = str(exc), True
                    return done
            try:
                renamer(change.old, change.new)
            except OSError as exc:
                why = f"the rename was refused ({type(exc).__name__}: {exc})"
        if why:
            done.skipped.append((change.old, why))
            if journal is not None:
                try:
                    journal("\t".join(("left", change.old, why)))
                except RecordError as exc:
                    done.record_problem, done.stopped = str(exc), True
                    return done
            continue
        note = ""
        try:
            store.move_file(change.old, change.new)
        except Exception as exc:
            note = f"index not updated ({type(exc).__name__}); the next index run sees the new name"
            done.index_problems.append((change.new, note))
        done.renamed.append((change.old, change.new))
        if journal is not None:
            try:
                journal("\t".join(("renamed", change.old, "to", change.new, str(identity[0]), str(identity[1]), str(identity[3]))
                                  + ((note,) if note else ())))
            except RecordError as exc:        # the rename has happened and is in `renamed`; nothing more without a record
                done.record_problem, done.stopped = str(exc), True
                return done
    return done


def read_record(text: str) -> List[Tuple[str, str, Optional[int], Optional[int], Optional[int], int]]:
    """(old, new, size, time in ns, file id, time tolerance in ns) from a record's "renamed" lines (None where
    a record written before they were kept has no value; the tolerance is 0 for a record written in
    nanoseconds and the precision of an older record written in seconds). A "renaming" line with no "renamed" or "left" after it is an
    intent whose outcome is not known (a crash in between): it is reported as a pair too, so undo can look
    at the disk and decide."""
    pairs: List[Tuple[str, str, Optional[int], Optional[int], Optional[int], int]] = []
    pending: Dict[Tuple[str, str], Tuple[Optional[int], Optional[int], Optional[int], int]] = {}
    for line in text.splitlines():
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) >= 4 and parts[2] == "to" and parts[0] in ("renaming", "renamed"):
            size = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
            when: Optional[int] = None
            tolerance = 0
            if len(parts) > 5 and _is_number(parts[5]):
                if parts[5].isdigit():
                    when = int(parts[5])
                else:                       # an older record: seconds with a few decimals; compared at that precision
                    when = int(round(float(parts[5]) * 1e9))
                    decimals = len(parts[5].split(".")[1]) if "." in parts[5] else 0
                    tolerance = int(10 ** (9 - decimals)) if decimals < 9 else 0
            ident = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else None
            if parts[0] == "renaming":
                pending[(parts[1], parts[3])] = (size, when, ident, tolerance)
            else:
                pending.pop((parts[1], parts[3]), None)
                pairs.append((parts[1], parts[3], size, when, ident, tolerance))
        elif len(parts) >= 2 and parts[0] == "left":
            for key in [k for k in pending if k[0] == parts[1]]:
                del pending[key]
    for (old, new), (size, when, ident, tolerance) in pending.items():
        pairs.append((old, new, size, when, ident, tolerance))    # intent without outcome: undo looks at the disk
    return pairs


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def undo(pairs: Sequence[Tuple], store, journal: Optional[Callable[[str], None]] = None,
         renamer: Callable[[str, str], None] = rename_no_replace, protected: Sequence[str] = (),
         fold: Callable[[str], str] = lambda p: p) -> Applied:
    """Rename back, last first. A file that is not at its new name, whose old name is taken, that is in a
    folder protected now, or that is not the file that was renamed (size, exact time or file id differ from
    the record) is left, and said so. The same record protocol as apply: intent before, outcome after."""
    from .records import RecordError
    done = Applied()
    for item in reversed(list(pairs)):
        old, new = item[0], item[1]
        size = item[2] if len(item) > 2 else None
        when = item[3] if len(item) > 3 else None
        ident = item[4] if len(item) > 4 else None
        tolerance = item[5] if len(item) > 5 else 0
        why = ""
        if any(fold(new).startswith(fold(f.rstrip("/\\")) + sep) for f in protected for sep in ("/", "\\")):
            why = "it is in a protected folder now"
        elif not os.path.lexists(new):
            why = "it is not at its new name now"
        elif os.path.lexists(old):
            why = "a file with the old name is there now"
        elif size is not None:
            try:
                if not same_identity(identity_of(new), (size, when, 0, ident), tolerance):
                    why = "the file at the new name is not the one that was renamed (size, time or file id differ)"
            except OSError as exc:
                why = f"it could not be looked at ({type(exc).__name__})"
        if not why:
            if journal is not None:
                try:
                    journal("\t".join(("renaming", new, "to", old)))
                except RecordError as exc:
                    done.skipped.append((new, f"the record could not be written ({exc}); nothing more was put back"))
                    done.record_problem, done.stopped = str(exc), True
                    return done
            try:
                renamer(new, old)
            except OSError as exc:
                why = f"the rename was refused ({type(exc).__name__}: {exc})"
        if why:
            done.skipped.append((new, why))
            if journal is not None:
                try:
                    journal("\t".join(("left", new, why)))
                except RecordError as exc:
                    done.record_problem, done.stopped = str(exc), True
                    return done
            continue
        try:
            store.move_file(new, old)
        except Exception as exc:
            done.index_problems.append((old, f"index not updated ({type(exc).__name__})"))
        done.renamed.append((new, old))
        if journal is not None:
            try:
                journal("\t".join(("renamed", new, "to", old)))
            except RecordError as exc:
                done.record_problem, done.stopped = str(exc), True
                return done
    return done
