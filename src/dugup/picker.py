"""Choosing what to index in a folder, subfolder by subfolder, starting from a reasonable guess.

Pure: it works on a listing (paths, sizes, cloud placeholders) and touches no disk. The window draws it
(window.py), and a test can drive it without a window.

  build()        the folders below a root as a tree, each with what it holds
  guess()        a starting choice for one folder, with the reason in words
  Plan           the choices a person has made so far: in or out for each folder, and how far indexing goes
  Plan.to_root() those choices as a Root (skip rules and levels), ready for Service.set_root

A folder is in or out (the tick). A folder that is in has a level; one that was given none follows the
folder above it. Folders of tool data (downloaded requirements, version-control data) are not shown one
by one: a single switch skips them all, as `dugup roots suggest --apply-safe` does.

The guesses are starting points, not rules. Each says why, and the numbers behind them are settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from .filesource import ScanRules, excluded
from .shapes import (LEVEL_MEANING, LEVEL_NAMES, LEVEL_OCR, LEVEL_WORDS, LEVELS, OPTION_ALIKE, OPTION_MEANING, OPTION_OCR,
                     OPTION_WORDS, OPTIONS, POLICY_NAMES_ONLY, FileKind, Root, level_of, level_words, normalize_level,
                     options_of)
from .suggest import safe_skips
from .survey import CODE, DOCUMENTS, MEDIA, OTHER, PICTURES, TEXT, Rates, Row, group_of

LEVEL_LABELS = {LEVEL_NAMES: "Names only", LEVEL_WORDS: "Words", LEVEL_OCR: "Words and scans", LEVEL_MEANING: "Everything"}
OPTION_LABELS = {OPTION_WORDS: "Words", OPTION_OCR: "Scans", OPTION_MEANING: "Meaning", OPTION_ALIKE: "Pictures alike"}
NOT_YET = (OPTION_MEANING, OPTION_ALIKE)      # remembered now, used when searching by meaning is built
PICTURE_FOLDER = level_of([OPTION_WORDS, OPTION_ALIKE])   # documents are read, pictures are matched by what they show


def label(level: str) -> str:
    """A level in a few words: one of the usual names, or its choices listed."""
    spelled = normalize_level(level) or LEVEL_NAMES
    return LEVEL_LABELS.get(spelled) or level_words(spelled).capitalize()
TRASH_FOLDERS = frozenset({"trash", ".trash", ".trashes", "$recycle.bin", "recycle bin", "recycled"})


@dataclass
class Node:
    name: str
    rel: str                                   # the path below the root, written with "/"; "" is the root
    total: Row = field(default_factory=lambda: Row(""))     # everything below this folder
    own: Row = field(default_factory=lambda: Row(""))       # the files directly in it
    children: Dict[str, "Node"] = field(default_factory=dict)   # by folded name
    tool: bool = False                         # tool data: hidden, and skipped by the one switch

    def sorted_children(self, with_tools: bool = False) -> List["Node"]:
        """Largest first, the way a person looks for what matters."""
        shown = [c for c in self.children.values() if with_tools or not c.tool]
        return sorted(shown, key=lambda c: (-c.total.bytes, c.name.casefold()))

    def walk(self) -> Iterator["Node"]:
        yield self
        for child in self.children.values():
            yield from child.walk()


@dataclass(frozen=True)
class Tree:
    root: str
    top: Node
    tool_patterns: Tuple[str, ...]             # skip rules that leave out all tool data found
    tool_files: int
    copies: Tuple = ()                         # copies.Pair: folders that look like copies of each other


def _add(row: Row, group: str, size: int, offline: bool, tool: bool) -> None:
    row.files += 1
    if tool:                         # counted, and otherwise kept out of the picture a person sees
        row.not_worth += 1
        return
    row.bytes += max(0, size)
    row.groups[group] += 1
    if offline:
        row.cloud_only += 1
    elif group in (DOCUMENTS, TEXT):
        row.readable += 1
    elif group == PICTURES:
        row.to_ocr += 1


def build(root: str, files: Iterable[Tuple[str, int, bool]],
          extensions: Optional[Mapping[str, FileKind]] = None) -> Tree:
    """`files` is (path, size, is a cloud placeholder) for every file below root."""
    files = list(files)
    base = root.rstrip("/\\")
    pairs = [(path, size) for path, size, _ in files]
    keys, rules = safe_skips(root, pairs, extensions)
    safe = {key.casefold() for key in keys}
    patterns = tuple(rules)
    top = Node(base.replace("\\", "/").rsplit("/", 1)[-1] or base, "")
    tool_files = 0
    for path, size, offline in files:
        relative = path[len(base) + 1:].replace("\\", "/")
        parts = relative.split("/")
        group = group_of(path, extensions)
        node, in_tool = top, False
        chain = [top]
        for depth, part in enumerate(parts[:-1]):
            child = node.children.get(part.casefold())
            if child is None:
                rel = "/".join(parts[:depth + 1])
                child = node.children[part.casefold()] = Node(part, rel, tool=rel.casefold() in safe)
            in_tool = in_tool or child.tool
            node = child
            chain.append(node)
        tool_files += 1 if in_tool else 0
        for above in chain:
            _add(above.total, group, size, offline, in_tool)
        _add(node.own, group, size, offline, in_tool)
    # A drive is named with its slash ("G:\\"): "G:" alone means "wherever I last was on G:", which is not a place.
    from . import copies
    alike = tuple(copies.find(((path[len(base) + 1:].replace("\\", "/"), size) for path, size in pairs), skip=keys))
    return Tree(root if base.endswith(":") else base, top, patterns, tool_files, alike)


def _in_tool(top: Node, rel: str) -> bool:
    node = top
    for part in rel.split("/"):
        node = node.children.get(part.casefold())
        if node is None:
            return False
        if node.tool:
            return True
    return False


@dataclass(frozen=True)
class Guess:
    ticked: bool
    level: str
    reason: str


def guess(node: Node, book_folders: Sequence[str] = (), book_level: str = LEVEL_NAMES,
          picture_fraction: float = 0.8, picture_min: int = 20) -> Guess:
    """Where one folder starts. Never the last word: every row can be changed."""
    row = node.total
    name = node.name.casefold()
    useful = row.files - row.not_worth
    if name in TRASH_FOLDERS:
        return Guess(False, LEVEL_MEANING, "a trash folder")
    if node.tool or (row.files and useful == 0):
        return Guess(False, LEVEL_MEANING, "tool data and downloaded requirements, nothing a person wrote")
    if book_level and name in {b.casefold() for b in book_folders}:
        return Guess(True, normalize_level(book_level) or LEVEL_NAMES, "the name suggests a book library (a guess; change it if you want the books read)")
    readable, pictures = row.readable, row.to_ocr
    if readable + pictures == 0:
        if row.cloud_only:
            return Guess(True, LEVEL_NAMES, "its files are kept in the cloud, not on this machine")
        return Guess(True, LEVEL_NAMES, "nothing here that can be read (sound, video, programs or other kinds)")
    if pictures >= picture_min and pictures >= picture_fraction * (readable + pictures):
        return Guess(True, PICTURE_FOLDER, "mostly pictures: matched by what they show, and not read for words unless you tick Scans")
    if pictures:
        return Guess(True, LEVEL_MEANING, "documents and pictures together: scans are read too")
    return Guess(True, LEVEL_MEANING, "documents and text")


def literal(rel: str) -> str:
    """A folder's path as a skip rule that means exactly that folder ("/a/b"), even when its name has
    characters a pattern would read as wildcards."""
    return "/" + "".join(f"[{c}]" if c in "*?[" else c for c in rel)


class Plan:
    """The choices so far. Folders nobody touched follow the folder above them."""

    def __init__(self, tree: Tree, skip_tool: bool = True, base_level: str = LEVEL_MEANING,
                 kept_rules: Sequence[str] = (), include: Sequence[str] = (), policy: str = "open") -> None:
        self.tree = tree
        self.skip_tool = skip_tool
        self.base_level = base_level
        self.kept_rules = tuple(kept_rules)    # skip rules that were there before and are not folder ticks
        self.include = tuple(include)
        self.policy = policy
        self.unticked: Dict[str, str] = {}     # folded rel -> rel
        self.levels: Dict[str, Tuple[str, str]] = {}   # folded rel -> (rel, level)
        self.reasons: Dict[str, str] = {}      # folded rel -> why it started the way it did
        self.copies: List[tuple] = []          # (copies.Family, the folders left out): what the guess found
        self._nodes = {node.rel.casefold(): node for node in tree.top.walk()}

    # -- reading

    def node(self, rel: str) -> Optional[Node]:
        return self._nodes.get(rel.casefold())

    def _above(self, rel: str) -> Iterator[str]:
        """The folder itself, then each folder above it, ending with the root ("")."""
        key = rel.casefold()
        while True:
            yield key
            if not key:
                return
            key = key.rsplit("/", 1)[0] if "/" in key else ""

    def ticked(self, rel: str) -> bool:
        """In or out, taking the folders above into account: nothing below a folder that is out is in."""
        return not any(key in self.unticked for key in self._above(rel) if key)

    def own_tick(self, rel: str) -> bool:
        return rel.casefold() not in self.unticked

    def level(self, rel: str) -> str:
        for key in self._above(rel):
            if key in self.levels:
                return normalize_level(self.levels[key][1]) or LEVEL_NAMES
        return normalize_level(self.base_level) or LEVEL_MEANING

    def has(self, rel: str, option: str) -> bool:
        return option in options_of(self.level(rel))

    def differs(self, rel: str) -> bool:
        """True when this folder has a choice of its own and does not simply follow the folder above."""
        return bool(rel) and rel.casefold() in self.levels

    def inside_with_own_choice(self, rel: str) -> List[str]:
        """Folders below this one that have a level or a tick of their own."""
        prefix = rel.casefold() + "/" if rel else ""
        found = [pair[0] for key, pair in self.levels.items() if key.startswith(prefix) and key != rel.casefold() and key]
        found += [path for key, path in self.unticked.items() if key.startswith(prefix) and key != rel.casefold()]
        return sorted(set(found), key=str.casefold)

    def own_level(self, rel: str) -> Optional[str]:
        found = self.levels.get(rel.casefold())
        return found[1] if found else None

    def cost(self, rates: Rates, rel: str = "") -> float:
        """Estimated index growth for a folder and everything below it, under the choices made."""
        node = self.node(rel)
        if node is None or not self.ticked(rel) or (node.tool and self.skip_tool):
            return 0.0
        total = node.own.cost_for(rates, options_of(self.level(rel)))
        if not self.skip_tool:       # tool files are then indexed like any others: count their names at least
            total += node.own.not_worth * rates.name
        for child in node.children.values():
            total += self.cost(rates, child.rel)
        return total

    def counts(self) -> Tuple[int, int]:
        """(folders in, folders out) among those shown directly below the root."""
        shown = self.tree.top.sorted_children()
        inside = sum(1 for child in shown if self.ticked(child.rel))
        return inside, len(shown) - inside

    # -- changing

    def set_ticked(self, rel: str, ticked: bool) -> None:
        if not rel:
            return                   # the root itself is always in: removing it is another action
        if ticked:
            self.unticked.pop(rel.casefold(), None)
        else:
            self.unticked[rel.casefold()] = rel

    def set_level(self, rel: str, level: Optional[str]) -> None:
        """None: follow the folder above again."""
        if level is None:
            self.levels.pop(rel.casefold(), None)
            if not rel:
                self.base_level = LEVEL_MEANING
        elif normalize_level(level) is not None:
            level = normalize_level(level)
            if not rel:
                self.base_level = level
            elif level == self.level(rel.rsplit("/", 1)[0] if "/" in rel else ""):
                self.levels.pop(rel.casefold(), None)       # the same as the folder above: nothing to remember
            else:
                self.levels[rel.casefold()] = (rel, level)

    def next_level(self, rel: str) -> str:
        """Step through the four usual combinations, round and round. A combination that is not one of them
        starts again at the first."""
        now = self.level(rel)
        level = LEVELS[(LEVELS.index(now) + 1) % len(LEVELS)] if now in LEVELS else LEVELS[0]
        self.set_level(rel, level)
        return level

    def set_option(self, rel: str, option: str, on: bool) -> str:
        """Turn one choice on or off for a folder, starting from what applies to it now. Scans and meaning
        bring words with them; turning words off turns those off too. Returns the level that results."""
        chosen = set(options_of(self.level(rel)))
        if on:
            chosen.add(option)
        else:
            chosen.discard(option)
            if option == OPTION_WORDS:
                chosen -= {OPTION_OCR, OPTION_MEANING}
        self.set_level(rel, level_of(chosen))
        return self.level(rel)

    def apply_to_everything_inside(self, rel: str) -> int:
        """Make every folder below this one follow it again: their own levels and ticks are taken away.
        Returns how many folders that changed."""
        doomed = self.inside_with_own_choice(rel)
        for path in doomed:
            self.levels.pop(path.casefold(), None)
            self.unticked.pop(path.casefold(), None)
        return len(doomed)

    def follow_above(self, rel: str) -> None:
        """Take this folder's own level away, so it follows the folder above it."""
        if rel:
            self.levels.pop(rel.casefold(), None)

    # -- the result

    def to_root(self) -> Root:
        """The choices as skip rules and levels. A folder that is out becomes one skip rule for exactly
        that folder; folders below it need none."""
        rules = list(self.kept_rules)
        if self.skip_tool:
            rules += [p for p in self.tree.tool_patterns]
        for key, rel in sorted(self.unticked.items()):
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if self.ticked(parent) if parent else True:
                rules.append(literal(rel))
        seen, exclude = set(), []
        for rule in rules:
            if rule.casefold() not in seen:
                seen.add(rule.casefold())
                exclude.append(rule)
        levels = [("", self.base_level)] if self.base_level != LEVEL_MEANING and self.policy != POLICY_NAMES_ONLY else []
        levels += [pair for key, pair in sorted(self.levels.items()) if self.ticked(pair[0])]
        return Root(self.tree.root, self.policy, self.include, tuple(exclude), levels=tuple(levels))

    def still_skipped(self) -> List[str]:
        """Folders that are ticked here but that an older, broader skip rule still leaves out."""
        rules = ScanRules((), self.kept_rules)
        return [node.rel for node in self.tree.top.walk()
                if node.rel and not node.tool and self.own_tick(node.rel) and self.ticked(node.rel)
                and excluded(node.rel, rules)
                and not excluded(node.rel.rsplit("/", 1)[0] if "/" in node.rel else "", rules)]


def plan_from_guess(tree: Tree, settings=None) -> Plan:
    """The plan a new folder starts with: a guess for each folder directly below it."""
    books = [b.strip() for b in (settings.book_folders if settings else "").split(";") if b.strip()]
    plan = Plan(tree)
    for child in tree.top.sorted_children():
        made = guess(child, books, settings.book_folder_level if settings else LEVEL_NAMES,
                     settings.picture_heavy_fraction if settings else 0.8, settings.picture_heavy_min if settings else 20)
        plan.reasons[child.rel.casefold()] = made.reason
        if not made.ticked:
            plan.set_ticked(child.rel, False)
        else:
            plan.set_level(child.rel, made.level)
    from . import copies
    for family in copies.families(tree.copies):
        folders = [f for f in family.folders if plan.ticked(f)]
        if len(folders) < 2:
            continue                                   # all but one are left out already
        if family.inside:      # alike in parts only: each also holds what the other lacks, so nothing is left out
            for one in folders:
                others = ", ".join(f"\"{f}\"" for f in folders if f != one)
                plan.reasons[one.casefold()] = (f"{family.inside} folders inside are copies of folders in {others}; all are "
                                                "included, open it to untick the ones you do not need")
            plan.copies.append((family, ()))
            continue
        about = f"about {family.share:.0%} of the same file names and sizes"
        backups = copies.named_backups(folders)
        kept = [f for f in folders if f not in backups]
        for backup in backups:
            plan.set_ticked(backup, False)
            plan.reasons[backup.casefold()] = (f"looks like a backup of \"{kept[0]}\" ({about}), so it is left out; "
                                               "tick it if you want both")
        for one in kept:
            others = [f for f in kept if f != one]
            before = plan.reasons.get(one.casefold(), "")
            if others:
                shown = ", ".join(f"\"{f}\"" for f in others[:3]) + (f" and {len(others) - 3} more" if len(others) > 3 else "")
                note = (f"looks like a copy of {shown} ({about}); all are included, untick the ones you do not need")
                plan.reasons[one.casefold()] = note
            elif backups:
                plan.reasons[one.casefold()] = (before + "; " if before else "") + \
                    f"\"{backups[0]}\" looks like a backup of this and is left out"
        plan.copies.append((family, tuple(backups)))
    return plan


def plan_from_root(tree: Tree, root: Root) -> Plan:
    """The plan of a folder that is already indexed: what its skip rules and levels say now."""
    tool = {p.casefold() for p in tree.tool_patterns}
    present = {p.casefold() for p in root.exclude}
    rules = ScanRules((), tuple(root.exclude))
    plan = Plan(tree, skip_tool=bool(tool) and tool <= present or not tool, include=root.include, policy=root.policy,
                base_level=root.base_level)
    ticks = set()
    for node in tree.top.walk():
        if not node.rel or node.tool:
            continue
        parent = node.rel.rsplit("/", 1)[0] if "/" in node.rel else ""
        if literal(node.rel).casefold() in present and not excluded(parent, rules):
            plan.set_ticked(node.rel, False)
            ticks.add(literal(node.rel).casefold())
    plan.kept_rules = tuple(p for p in root.exclude if p.casefold() not in ticks and p.casefold() not in tool)
    for folder, level in root.levels:
        if folder == "":
            plan.base_level = level
        else:
            plan.levels[folder.casefold()] = (folder, level)
    return plan
