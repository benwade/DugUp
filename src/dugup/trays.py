"""One DugUp in the tray: the newest version.

Each copy of DugUp (an installed one, a source checkout) keeps its own data folder, and a tray answers
commands only through its own folder. So two copies' trays could not see each other: both ran, both wanted
the search key, and "Quit DugUp" in one never reached the other.

A starting tray now looks for the trays of other copies too. It finds them in the process list, works out
each one's data folder from its command line (the same rules the copy itself follows), and asks it for its
version. The newest version keeps the tray:

- a newer tray asks an older one to quit (the usual quit, so an index update under way stops cleanly), waits
  until it has gone, and takes over;
- an older tray, or one of the same version, brings up the running tray's search box and leaves.

A tray too old to say its version counts as older. A tray that is seen in the process list but cannot be
reached is never ended from here: it is named, with the command to end it.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import serverprocs

TRAY_LIKE = "%dugup%tray%"
GONE_SECONDS = 30.0          # how long a tray asked to quit gets to finish (an update under way stops first)
GONE_POLL = 0.25

Summon = Callable[[Path, str], Optional[str]]


# ---- versions ---------------------------------------------------------------------------------------------------

_VERSION = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(?:[.\-_]?(dev|alpha|a|beta|b|rc|c)[.\-_]?(\d*))?", re.IGNORECASE)
_STAGES = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "rc": 3, "c": 3}
_FINAL = 4


def version_key(text: Optional[str]) -> Optional[Tuple]:
    """A version as something that sorts: 0.2.9.dev0 < 0.2.9a1 < 0.2.9rc1 < 0.2.9 < 0.2.10. None when the text
    is no version (an older tray answers "no" to a question it does not know)."""
    found = _VERSION.match(text or "")
    if not found:
        return None
    release = tuple(int(n) for n in found.group(1).split("."))
    release = release + (0,) * max(0, 4 - len(release))
    stage = _STAGES[found.group(2).lower()] if found.group(2) else _FINAL
    return release, stage, int(found.group(3) or 0)


def is_newer(mine: Optional[str], theirs: Optional[str]) -> bool:
    """True when `mine` is a newer version than `theirs`. A version that cannot be read counts as oldest; two
    of those are equal, so neither is newer."""
    a, b = version_key(mine), version_key(theirs)
    if a is None:
        return False
    return b is None or a > b


# ---- reading a tray's command line ------------------------------------------------------------------------------

def is_tray(parts: Sequence[str]) -> bool:
    """True for `... dugup tray ...` that runs a tray, not one that only asks (`--quit`, `--startup`)."""
    lowered = [p.lower() for p in parts]
    for i, word in enumerate(lowered):
        if word != "tray":
            continue
        before, after = lowered[:i], lowered[i + 1:]
        if not any(p == "dugup" or ntpath.basename(p) in ("dugup", "dugup.exe") for p in before):
            return False
        return not any(a in ("--quit", "--startup") or a.startswith("--startup=") for a in after)
    return False


def data_dir_arg(parts: Sequence[str]) -> Optional[str]:
    """The folder given with --data-dir on a command line, or None."""
    for i, part in enumerate(parts):
        if part == "--data-dir" and i + 1 < len(parts):
            return parts[i + 1]
        if part.startswith("--data-dir="):
            return part.partition("=")[2]
    return None


def _same(a: os.PathLike | str, b: os.PathLike | str) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(a))) == os.path.normcase(os.path.abspath(os.fspath(b)))


def candidate_roots(parts: Sequence[str], per_user: Path,
                    home_of: Callable[[str], object]) -> List[Path]:
    """The data folder a tray with this command line uses, by the rules the copy itself follows: the folder it
    was given, else the one its marker beside the program chooses (with any note that keeps an older folder in
    use), else the per-user default. One folder, not a list of guesses: a guess that happened to be this
    tray's own folder once hid a tray started by hand. It is checked by asking the tray there."""
    given = data_dir_arg(parts)
    if given:
        return [Path(os.path.abspath(given))]
    program = parts[0] if parts else ""
    if program:
        home = home_of(ntpath.dirname(program) if "\\" in program else os.path.dirname(program))
        if home is not None:
            from .datadir import choose_beside
            return [Path(os.path.abspath(choose_beside(home, per_user).path))]   # type: ignore[arg-type]
    return [Path(os.path.abspath(per_user))]


# ---- the other trays --------------------------------------------------------------------------------------------

@dataclass
class Running:
    """A tray that answered: its data folder, its version (None when too old to say), the program it runs, the
    process ids seen for it (a venv's launcher and the Python it starts are two), and the one the tray itself
    wrote into its address file, which is the process waited for."""

    root: Path
    version: Optional[str]
    program: str = ""
    pids: List[int] = field(default_factory=list)
    tray_pid: Optional[int] = None

    def name(self) -> str:
        what = f"DugUp {self.version}" if self.version else "an earlier DugUp"
        return f"{what} ({self.program or self.root})"


@dataclass(frozen=True)
class Unreached:
    """A tray seen in the process list whose data folder could not be found, so it cannot be asked anything."""

    pid: int
    command_line: str


def ask_version(root: Path, summon: Summon) -> Optional[str]:
    answer = summon(root, "version")
    return answer if answer and version_key(answer) is not None else None


def survey(own_root: Path, own_pid: int, rows: Sequence[Tuple[int, str]], per_user: Path, summon: Summon,
           home_of: Callable[[str], object],
           pid_of: Optional[Callable[[Path], Optional[int]]] = None) -> Tuple[List[Running], List[Unreached]]:
    """The trays of other copies of DugUp, from the process list `rows` ((pid, command line) pairs): the ones
    that answered, one per data folder, and the ones that could not be reached. A process that may be using
    this tray's own folder is left out: it is this tray, the launcher that started it, or the tray already
    asked about through that folder."""
    pid_of = pid_of or address_pid
    found: Dict[str, Running] = {}
    silent: set = set()
    unreached: List[Unreached] = []
    for pid, line in rows:
        if pid == own_pid:
            continue
        parts = serverprocs.split_command_line(line)
        if not parts or not is_tray(parts):
            continue
        roots = candidate_roots(parts, per_user, home_of)
        if any(_same(r, own_root) for r in roots):
            continue
        for root in roots:
            key = os.path.normcase(os.path.abspath(root))
            if key in found:
                found[key].pids.append(pid)
                if pid == found[key].tray_pid:           # the tray's own process names its program best
                    found[key].program = parts[0]
                break
            if key in silent:
                continue
            if summon(root, "ping") == "pong":
                found[key] = Running(root, ask_version(root, summon), parts[0], [pid], pid_of(root))
                break
            silent.add(key)
        else:
            unreached.append(Unreached(pid, line))
    # the Python a venv's launcher starts names the base Python, whose folder holds no marker: it is unreached
    # by its own command line, yet it is the tray that answered through its launcher's folder
    answered = {r.tray_pid for r in found.values() if r.tray_pid}
    return list(found.values()), [u for u in unreached if u.pid not in answered]


def process_watch(pid: int) -> Callable[[], bool]:
    """"Is that process still running?", for a process seen now. Opened before the tray is asked to quit, so a
    later process given the same id is never mistaken for it. A process that cannot be opened has gone."""
    from . import parentwatch
    if os.name == "nt":
        alive = parentwatch.windows_alive(pid)
        return alive if alive is not None else (lambda: False)
    return parentwatch.posix_alive(pid)


def address_pid(root: Path) -> Optional[int]:
    """The process id a tray wrote into its address file (summons.py), or None."""
    from .summons import address_file
    try:
        return int(json.loads(address_file(root).read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def wait_gone(root: Path, summon: Summon, alive: Sequence[Callable[[], bool]] = (), timeout: float = GONE_SECONDS,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """True once the tray on `root` no longer answers and its processes have ended (only then are the search key
    and the index free); False when it is still there after `timeout` seconds."""
    started = clock()
    while summon(root, "ping") == "pong" or any(check() for check in alive):
        if clock() - started >= timeout:
            return False
        sleep(GONE_POLL)
    return True


# ---- deciding ---------------------------------------------------------------------------------------------------

@dataclass
class Settled:
    """What a starting tray found and did. `start` says whether it should go on and start."""

    start: bool
    mine: str
    brought_up: Optional[Running] = None     # not starting: the tray whose search box was brought up
    closed: List[Running] = field(default_factory=list)
    stuck: List[Running] = field(default_factory=list)
    unreached: List[Unreached] = field(default_factory=list)
    platform: str = ""

    def lines(self) -> List[str]:
        """What happened, in plain words, one line each. Empty when nothing else was running."""
        lines = []
        if self.brought_up is not None:
            lines.append(f"{self.brought_up.name()} is already in the tray, so this copy (DugUp {self.mine}) did "
                         "not start a second one. Its search box was brought up.")
        for other in self.closed:
            lines.append(f"Closed {other.name()} and took over the tray.")
        for other in self.stuck:
            lines.append(f"{other.name()} was asked to quit but is still running, so this tray did not start. "
                         "Quit it from its tray icon, then start DugUp again.")
        if self.unreached:
            pids = [u.pid for u in self.unreached]
            stop = (f"Stop-Process -Id {', '.join(map(str, pids))}" if (self.platform or sys.platform).startswith("win")
                    else f"kill {' '.join(map(str, pids))}")
            noun = "process" if len(pids) == 1 else "processes"
            lines.append(f"Another DugUp tray may be running that this one cannot reach ({noun} "
                         f"{', '.join(map(str, pids))}). If the search key does nothing, end it ({stop}), then start "
                         "DugUp again.")
        return lines


def _newest(running: Sequence[Running]) -> Running:
    best = running[0]
    for other in running[1:]:
        if is_newer(other.version, best.version):
            best = other
    return best


def settle(own_root: Path, mine: str, summon: Summon,
           listing: Callable[[], Optional[Sequence[Tuple[int, str]]]] = lambda: serverprocs.command_lines(TRAY_LIKE),
           per_user: Optional[Callable[[], Path]] = None, home_of: Optional[Callable[[str], object]] = None,
           own_pid: Optional[int] = None, watch: Callable[[int], Callable[[], bool]] = process_watch,
           gone: Optional[Callable[[Path, Sequence[Callable[[], bool]]], bool]] = None,
           pid_of: Optional[Callable[[Path], Optional[int]]] = None,
           platform: Optional[str] = None) -> Settled:
    """Decide whether a tray of version `mine`, on data folder `own_root`, should start, and close any older tray
    on the way. The tray on its own folder is asked first, so the usual case (the tray is already running, a
    shortcut was clicked again) costs one question and no process listing."""
    gone = gone or (lambda root, alive: wait_gone(root, summon, alive))
    pid_of = pid_of or address_pid
    running: List[Running] = []
    if summon(own_root, "ping") == "pong":
        here = Running(own_root, ask_version(own_root, summon), tray_pid=pid_of(own_root))
        if not is_newer(mine, here.version):
            summon(own_root, "popup")
            return Settled(False, mine, brought_up=here, platform=platform or "")
        running.append(here)
    rows = listing()
    unreached: List[Unreached] = []
    if rows:
        if per_user is None:
            from .datadir import default_data_dir
            per_user = default_data_dir
        if home_of is None:
            from .datadir import find_program_home
            home_of = find_program_home
        others, unreached = survey(own_root, os.getpid() if own_pid is None else own_pid, rows, per_user(),
                                   summon, home_of, pid_of)
        holding = [o for o in others if not is_newer(mine, o.version)]
        if holding:
            newest = _newest(holding)
            summon(newest.root, "popup")
            return Settled(False, mine, brought_up=newest, unreached=unreached, platform=platform or "")
        running += others
    closed, stuck = [], []
    for other in running:
        # the tray's own process, watched from before the quit (so its id cannot be reused yet); not every process
        # seen for it, since a shell whose command line names the tray is not the tray
        alive = [watch(other.tray_pid)] if other.tray_pid else []
        summon(other.root, "quit")
        (closed if gone(other.root, alive) else stuck).append(other)
    return Settled(not stuck, mine, closed=closed, stuck=stuck, unreached=unreached, platform=platform or "")
