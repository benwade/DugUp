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
from .shapes import LEVEL_MEANING, LEVEL_NAMES, LEVEL_OCR, LEVEL_WORDS, LEVELS, POLICY_NAMES_ONLY, FileKind, Root
from .suggest import SAFE, suggest_excludes, safe_folders
from .survey import CODE, DOCUMENTS, MEDIA, OTHER, PICTURES, TEXT, Rates, Row, group_of

LEVEL_LABELS = {LEVEL_NAMES: "Names only", LEVEL_WORDS: "Words", LEVEL_OCR: "Words and scans", LEVEL_MEANING: "Everything"}
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
    safe = {key.casefold() for key in safe_folders(root, pairs, extensions)}
    patterns = tuple(s.pattern for s in suggest_excludes(root, pairs, extensions) if s.group == SAFE)
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
    return Tree(base, top, patterns, tool_files)


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
        return Guess(True, book_level, "the name suggests a book library (a guess; change it if you want the books read)")
    readable, pictures = row.readable, row.to_ocr
    if readable + pictures == 0:
        if row.cloud_only:
            return Guess(True, LEVEL_NAMES, "its files are kept in the cloud, not on this machine")
        return Guess(True, LEVEL_NAMES, "nothing here that can be read (sound, video, programs or other kinds)")
    if pictures >= picture_min and pictures >= picture_fraction * (readable + pictures):
        return Guess(True, LEVEL_WORDS, "mostly pictures: they are not read for words unless you choose Words and scans")
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
                return self.levels[key][1]
        return self.base_level

    def own_level(self, rel: str) -> Optional[str]:
        found = self.levels.get(rel.casefold())
        return found[1] if found else None

    def cost(self, rates: Rates, rel: str = "") -> float:
        """Estimated index growth for a folder and everything below it, under the choices made."""
        node = self.node(rel)
        if node is None or not self.ticked(rel) or (node.tool and self.skip_tool):
            return 0.0
        at = LEVELS.index(self.level(rel))
        total = node.own.cost(rates)[at]
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
        elif level in LEVELS:
            if not rel:
                self.base_level = level
            elif level == self.level(rel.rsplit("/", 1)[0] if "/" in rel else ""):
                self.levels.pop(rel.casefold(), None)       # the same as the folder above: nothing to remember
            else:
                self.levels[rel.casefold()] = (rel, level)

    def next_level(self, rel: str) -> str:
        """Step to the next level, round and round (what one click on the level does)."""
        level = LEVELS[(LEVELS.index(self.level(rel)) + 1) % len(LEVELS)]
        self.set_level(rel, level)
        return level

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
