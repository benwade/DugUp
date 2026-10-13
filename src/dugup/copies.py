"""Folders that look like copies of other folders (backups), found from names and sizes alone.

No file is opened. Each folder gets a small sketch of the (file name, size) pairs below it; two folders whose
sketches largely agree hold largely the same files. It is a guess and is said as one: a name and a size are
not proof, and the person decides.

  find()          pairs of folders that look alike, the topmost of each family only
  backup_name()   does a folder's name say "backup"?
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

SKETCH = 128            # how many of the smallest hashes are kept for a folder
BANDS = 12              # folders are compared when they share one of their first few hashes
MIN_FILES = 20          # a handful of files with the same names means little
MIN_SHARE = 0.8         # the share of files two folders must have in common
MIN_OVERLAP = 0.6      # the two must also be of a like size: a folder is not a "copy" of a much larger one that
                       # merely holds a copy of it somewhere inside (found on a real drive: one package against a whole
                       # environment). The pair that is alike is then the folder and that inner copy.
MOST_PAIRS = 200

_BACKUP = re.compile(r"(^|[^a-z])(backups?|bak|bkp|bkup)([^a-z]|$)|(^|[\s_\-(])copy( of\b|\s*\(?\d*\)?$)|^copy of ", re.IGNORECASE)


def backup_name(name: str) -> bool:
    """"Backup", "docs_bak", "Photos - Copy", "Copy of Taxes" and the like. Deliberately narrow: "old" and
    "archive" often name the only copy there is."""
    return bool(_BACKUP.search(name.strip()))


def page_parts(folder: str) -> bool:
    """Inside the folder a browser writes beside a saved web page ("Some page_files"): scripts and styles that
    are the same for every page saved from one site, so they pair up with each other and mean nothing."""
    return any(part.casefold().endswith(("_files", ".files", "_archivos", "_fichiers", "-dateien")) for part in folder.split("/"))


@dataclass(frozen=True)
class Family:
    folders: Tuple[str, ...]   # every folder that looks like a copy of the others, in a steady order
    files: int                 # files in the smallest of them
    share: float               # the weakest likeness between two of them that were paired
    inside: int = 0            # above 0: this many folders inside them are alike, not the folders as wholes


def families(pairs: Sequence["Pair"]) -> List[Family]:
    """Pairs joined up: when A is like B and B is like C, the three are one family. A folder kept in four
    places is then said once, with its four places, and not as six pairs."""
    leader: Dict[str, str] = {}

    def find_leader(folder: str) -> str:
        while leader.setdefault(folder, folder) != folder:
            leader[folder] = leader[leader[folder]]
            folder = leader[folder]
        return folder

    for pair in pairs:
        a, b = find_leader(pair.first), find_leader(pair.second)
        if a != b:
            leader[max(a, b)] = min(a, b)
    groups: Dict[str, List[Pair]] = {}
    for pair in pairs:
        groups.setdefault(find_leader(pair.first), []).append(pair)
    made = []
    for members in groups.values():
        folders = sorted({f for pair in members for f in (pair.first, pair.second)}, key=lambda f: (f.count("/"), f.casefold()))
        made.append(Family(tuple(folders), min(p.files for p in members), min(p.share for p in members),
                           max(p.inside for p in members)))
    made.sort(key=lambda f: (-f.files * (len(f.folders) - 1), f.folders[0].casefold()))
    return made


@dataclass(frozen=True)
class Pair:
    first: str            # folder paths below the root, written with "/"
    second: str
    share: float          # about what part of the smaller folder's files are in the other too, 0..1
    files: int            # files in the smaller of the two
    inside: int = 0       # above 0: the two are not alike as wholes; this many folders inside them pair off


def _mark(name: str, size: int) -> int:
    digest = hashlib.blake2b(f"{name.casefold()}\x00{size}".encode("utf-8", "surrogatepass"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def find(files: Iterable[Tuple[str, int]], min_files: int = MIN_FILES, min_share: float = MIN_SHARE,
         stop=None, skip: Iterable[str] = ()) -> List[Pair]:
    """`files` is (path below the root written with "/", size). Empty files are left out (they are all alike).
    `skip`: folders (same spelling) whose contents are not compared at all, such as tool data: every Python
    environment looks like every other, and nobody needs telling."""
    marks: Dict[str, List[int]] = {}          # folder -> marks of every file below it
    left_out = {key.casefold() for key in skip}
    verdicts: Dict[str, bool] = {}            # folder (folded) -> is it inside something left out

    def skipped(folder: str) -> bool:
        key = folder.casefold()
        known = verdicts.get(key)
        if known is None:
            known = key in left_out or ("/" in key and skipped(key.rsplit("/", 1)[0]))
            verdicts[key] = known
        return known

    for relative, size in files:
        if size <= 0 or "/" not in relative:
            continue
        folder, name = relative.rsplit("/", 1)
        if (left_out and skipped(folder)) or page_parts(folder):
            continue
        mark = _mark(name, size)
        while True:
            marks.setdefault(folder, []).append(mark)
            if "/" not in folder:
                break
            folder = folder.rsplit("/", 1)[0]
    sketches: Dict[str, Tuple[int, ...]] = {}
    counts: Dict[str, int] = {}
    for folder, found in marks.items():
        distinct = set(found)
        if len(distinct) >= min_files:
            sketches[folder] = tuple(sorted(distinct)[:SKETCH])
            counts[folder] = len(distinct)
    del marks
    # A folder that holds nothing but one folder is the same thing as that folder: the inner one speaks for both.
    for folder in sorted(sketches, key=len, reverse=True):
        above = _parent(folder)
        while above and folder in sketches and counts.get(above) == counts[folder]:
            sketches.pop(above, None)
            above = _parent(above)
    buckets: Dict[int, List[str]] = {}
    for folder, sketch in sketches.items():
        for value in sketch[:BANDS]:
            buckets.setdefault(value, []).append(folder)
    seen, alike = set(), {}
    for members in buckets.values():
        if stop is not None and stop.is_set():
            return []
        if len(members) < 2 or len(members) > 50:      # a file found everywhere says nothing
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair = (a, b) if a.casefold() < b.casefold() else (b, a)
                if pair in seen or _inside(a, b) or _inside(b, a):
                    continue
                seen.add(pair)
                overlap = _share(sketches[a], sketches[b])
                # from the overlap of the two together to the part of the smaller one that is in the larger:
                # a backup made a while ago holds fewer files than the folder it was made from
                common = overlap / (1.0 + overlap) * (counts[a] + counts[b])
                share = min(1.0, common / min(counts[a], counts[b]))
                if overlap >= MIN_OVERLAP and share >= min_share:
                    alike[pair] = share
    if len(alike) > 3000:                               # keep the work bounded: the largest folders first
        keep = sorted(alike, key=lambda pair: -min(counts[pair[0]], counts[pair[1]]))[:3000]
        alike = {pair: alike[pair] for pair in keep}

    def covers(outer: str, inner: str) -> bool:
        return outer.casefold() == inner.casefold() or _inside(inner, outer)

    pairs = []
    for (a, b), share in alike.items():
        said = any((c, d) != (a, b) and ((covers(c, a) and covers(d, b)) or (covers(c, b) and covers(d, a)))
                   for c, d in alike)
        if not said:                                    # else a pair of folders above these already says it
            pairs.append(Pair(a, b, share, min(counts[a], counts[b])))
    pairs = _rolled_up(pairs)
    pairs.sort(key=lambda p: (-p.files, p.first.casefold(), p.second.casefold()))
    return pairs[:MOST_PAIRS]


ROLL_UP = 3             # this many folders inside two folders pairing off one to one: the two are said instead


def _rolled_up(pairs: List["Pair"]) -> List["Pair"]:
    """Two libraries that share most of their shelves are one finding, not one for every shelf. (The libraries
    themselves may differ too much to pair: each has shelves the other lacks.)"""
    by_parents: Dict[Tuple[str, str], List[Pair]] = {}
    for pair in pairs:
        above = (_parent(pair.first), _parent(pair.second))
        if above[0] and above[1] and above[0].casefold() != above[1].casefold():
            by_parents.setdefault(above, []).append(pair)
    out = [p for p in pairs if len(by_parents.get((_parent(p.first), _parent(p.second)), ())) < ROLL_UP]
    for (first, second), members in by_parents.items():
        if len(members) >= ROLL_UP:
            out.append(Pair(first, second, min(p.share for p in members), sum(p.files for p in members), len(members)))
    return out


def _parent(folder: str) -> str:
    return folder.rsplit("/", 1)[0] if "/" in folder else ""


def _inside(inner: str, outer: str) -> bool:
    return inner.casefold().startswith(outer.casefold() + "/")


def _share(a: Sequence[int], b: Sequence[int]) -> float:
    """About what part of the two folders' files are in both, from their sketches."""
    both = sorted(set(a) | set(b))[:SKETCH]
    if not both:
        return 0.0
    in_a, in_b = set(a), set(b)
    return sum(1 for value in both if value in in_a and value in in_b) / len(both)


def named_backups(folders: Sequence[str]) -> List[str]:
    """Those of a family whose name (their own or a folder above) says backup; none when all of them do,
    since then the names do not tell which to keep."""
    said = [f for f in folders if any(backup_name(part) for part in f.split("/"))]
    return [] if len(said) == len(folders) else said


def the_copy(pair: Pair) -> str:
    """Which of the two is the backup, when a name says so (the folder's own name or one above it); else ""."""
    def says(folder: str) -> bool:
        return any(backup_name(part) for part in folder.split("/"))
    first, second = says(pair.first), says(pair.second)
    if first != second:
        return pair.first if first else pair.second
    return ""
