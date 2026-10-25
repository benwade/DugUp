"""The one data folder (guide 5a and 5c).

Everything the program writes lives under one folder, so deleting the program's
data is deleting that folder. The folder is chosen, in order, by the command
line, the environment, a marker file beside the program, then the per-user
default. It is never inside a folder the user said the program must not write
to, and by default never inside a folder that looks cloud-synced.

The marker (`dugup-data-folder.txt`) is how one folder holds both the program
and its data: the installer writes one, and a source checkout carries one. It
names the data folder beside it and the program's own folders that are never
indexed. An index already in the per-user folder is never left behind
silently: until it is moved in, or the user chooses to keep it where it is,
that older folder stays the one in use.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Mapping, Optional, Tuple

import platformdirs

APP_NAME = "DugUp"
ENV_DATA_DIR = "DUGUP_DATA_DIR"
ENV_NEVER_WRITE = "DUGUP_NEVER_WRITE"  # folders separated by os.pathsep

# Folder names that usually mean "synced to a cloud service". A guard, not a
# law: allow_synced=True overrides it (the user's folders, the user's choice).
SYNC_FOLDER_NAMES = frozenset(
    {"dropbox", "google drive", "my drive", "icloud drive", "iclouddrive", "box sync", "nextcloud"}
)


class DataDirError(Exception):
    """The data folder cannot be used. `reason` says why, in plain words."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def default_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


# ---- the folder beside the program -------------------------------------------------------------------------

MARKER_NAME = "dugup-data-folder.txt"
KEPT_ELSEWHERE = "use-folder.txt"   # in the folder beside the program: the user kept an older data folder in use
MARKER_SEARCH_DEPTH = 5             # src/dugup -> checkout is 2 up; <app>/python/Lib/site-packages/dugup is 4


@dataclass(frozen=True)
class ProgramHome:
    """The program's own folder, found by its marker: where its data lives and what is never indexed."""

    folder: Path                  # the folder holding the marker
    data: Path                    # the data folder the marker names (beside it)
    skip: Tuple[Path, ...] = ()   # the program's own folders, never indexed (the data folder always is)

    def never_indexed(self) -> Tuple[Path, ...]:
        return (self.data,) + tuple(self.skip)


def read_marker(path: os.PathLike | str) -> ProgramHome:
    """Read a marker file: `data = <folder>` (default `data`) and any number of `skip = <folder>` lines, each
    relative to the marker's own folder. Blank lines and lines starting with # are notes. Unknown names are
    ignored, so a newer marker still works with an older DugUp."""
    path = Path(path)
    folder = Path(os.path.abspath(path.parent))
    data = "data"
    skip: List[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip().lower(), value.strip().strip('"')
        if name == "data" and value:
            data = value
        elif name == "skip" and value:
            skip.append(value)
    return ProgramHome(folder, Path(os.path.abspath(folder / data)),
                       tuple(Path(os.path.abspath(folder / s)) for s in skip))


def find_program_home(start: Optional[os.PathLike | str] = None, depth: int = MARKER_SEARCH_DEPTH) -> Optional[ProgramHome]:
    """The marker beside the program, looked for from this package's folder upwards, or None."""
    here = Path(os.path.abspath(start if start is not None else Path(__file__).parent))
    for folder in [here, *here.parents][:depth + 1]:
        marker = folder / MARKER_NAME
        if marker.is_file():
            try:
                return read_marker(marker)
            except (OSError, UnicodeDecodeError):
                return None   # an unreadable marker is no marker: the per-user default stays in use
    return None


def has_index(folder: os.PathLike | str) -> bool:
    return (Path(folder) / "index.sqlite").is_file()


def kept_elsewhere(data: os.PathLike | str) -> Optional[Path]:
    """The older data folder the user chose to keep using, when that choice is recorded beside the program."""
    note = Path(data) / KEPT_ELSEWHERE
    try:
        lines = note.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            return Path(line)
    return None


@dataclass(frozen=True)
class BesideChoice:
    """What the folder beside the program means right now."""

    path: Path                            # the data folder to use
    home: ProgramHome
    move_from: Optional[Path] = None      # an index in the older per-user folder, not yet moved in or kept
    synced: object = None                 # the synced folder that kept the data from beside the program
    why: str = field(default="", compare=False)   # in plain words, which rule chose `path` (for diagnosis)


def choose_beside(home: ProgramHome, per_user: Path) -> BesideChoice:
    """The data folder beside the program, unless the user kept an older one, or an older index is waiting
    for the user to say what to do with it: until then that older folder stays in use, so nothing looks lost."""
    kept = kept_elsewhere(home.data)
    if kept is not None and kept.is_dir():
        return BesideChoice(kept, home, why=f"a note beside the program ({Path(home.data) / KEPT_ELSEWHERE}) sends it there")
    if not has_index(home.data):
        synced = synced_folder(home.data)
        if synced is not None:        # beside the program is uploaded by a sync app: the index stays out of it
            return BesideChoice(per_user, home, synced=synced,
                                why=f"the folder beside the program ({home.data}) is synced to the cloud, so the "
                                    "per-user folder is used")
    if not has_index(home.data) and has_index(per_user) and not is_within(per_user, home.data):
        return BesideChoice(per_user, home, move_from=per_user,
                            why="an index in the per-user folder is waiting to be moved beside the program")
    return BesideChoice(home.data, home, why=f"the marker beside the program ({Path(home.folder) / MARKER_NAME})")


def beside_program(cli_value: Optional[str] = None, environ: Optional[Mapping[str, str]] = None,
                   home: Optional[Callable[[], Optional[ProgramHome]]] = None,
                   default: Optional[Callable[[], Path]] = None) -> Optional[BesideChoice]:
    """The choice the marker makes, or None when the command line, the environment or a missing marker decides."""
    environ = os.environ if environ is None else environ
    if cli_value or environ.get(ENV_DATA_DIR):
        return None
    found = (home or find_program_home)()
    return choose_beside(found, Path(os.path.abspath((default or default_data_dir)()))) if found is not None else None


def is_within(path: os.PathLike | str, root: os.PathLike | str) -> bool:
    """True when `path` is `root` or below it, compared on whole path parts."""
    p = os.path.normcase(os.path.abspath(os.fspath(path)))
    r = os.path.normcase(os.path.abspath(os.fspath(root)))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


def _looks_synced(part: str) -> bool:
    lowered = part.lower()
    return lowered in SYNC_FOLDER_NAMES or lowered.startswith("onedrive")


def check_allowed(path: os.PathLike | str, never_write: Iterable[os.PathLike | str] = (), allow_synced: bool = False) -> None:
    """Raise DataDirError when `path` must not be used as the data folder."""
    for root in never_write:
        if str(root) and is_within(path, root):
            raise DataDirError(f"inside a folder the program must never write to: {root}")
    if not allow_synced:
        for part in Path(path).parts:
            if _looks_synced(part):
                raise DataDirError(
                    f"looks like a cloud-synced folder ('{part}'); choose another folder, "
                    "or allow it explicitly if you are sure"
                )
        synced = synced_folder(path)
        if synced is not None:
            raise DataDirError(
                f"inside {synced.path}, which {synced.by} syncs to the cloud: the index (the text of your files) "
                "would be uploaded with it. Choose another folder, or allow it explicitly if you are sure. "
                + syncfolders_caveat()
            )


def syncfolders_caveat() -> str:
    from . import syncfolders
    return syncfolders.CAVEAT


def synced_folder(path: os.PathLike | str):
    """The folder a sync app says it syncs that `path` lies in, or None (syncfolders.py)."""
    from . import syncfolders
    return syncfolders.survey().synced_by(path)


def never_write_from_env(environ: Optional[Mapping[str, str]] = None) -> list:
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_NEVER_WRITE, "")
    return [item for item in raw.split(os.pathsep) if item]


def resolve_data_dir(
    cli_value: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    default: Optional[Callable[[], Path]] = None,
    never_write: Iterable[os.PathLike | str] = (),
    allow_synced: bool = False,
    home: Optional[Callable[[], Optional[ProgramHome]]] = None,
) -> Path:
    """Pick the data folder: command line, then environment, then the marker beside the program, then the
    default."""
    return explain_data_dir(cli_value, environ, default, never_write, allow_synced, home)[0]


def explain_data_dir(
    cli_value: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    default: Optional[Callable[[], Path]] = None,
    never_write: Iterable[os.PathLike | str] = (),
    allow_synced: bool = False,
    home: Optional[Callable[[], Optional[ProgramHome]]] = None,
) -> Tuple[Path, str]:
    """The data folder resolve_data_dir picks, and in plain words which rule picked it. When a program cannot
    find its index, the rule is usually the answer (a leftover note, another copy of the program)."""
    environ = os.environ if environ is None else environ
    if cli_value:
        path, why = Path(cli_value).expanduser(), "it was given on the command line (--data-dir)"
    elif environ.get(ENV_DATA_DIR):
        path, why = Path(environ[ENV_DATA_DIR]).expanduser(), f"the {ENV_DATA_DIR} environment variable names it"
    else:
        default = default or default_data_dir
        beside = beside_program(None, environ, home, default)
        if beside is not None:
            path, why = beside.path, beside.why or "the marker beside the program"
        else:
            path, why = Path(default()), "it is the per-user default (no marker was found beside the program)"
    path = Path(os.path.abspath(path))
    check_allowed(path, never_write, allow_synced)
    return path, why


def ensure_data_dir(path: os.PathLike | str, never_write: Iterable[os.PathLike | str] = (), allow_synced: bool = False) -> Path:
    """Create the folder if needed, restrict it to the owner, prove it is writable."""
    path = Path(os.path.abspath(os.fspath(path)))
    check_allowed(path, never_write, allow_synced)
    try:
        if path.exists() and not path.is_dir():
            raise DataDirError(f"a file (not a folder) already exists at {path}")
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path, stat.S_IRWXU)  # 0700
        fd, probe = tempfile.mkstemp(dir=path, prefix=".probe-")
        os.close(fd)
        os.remove(probe)
    except DataDirError:
        raise
    except OSError as exc:
        raise DataDirError(f"cannot create or write to the folder: {exc.strerror or exc}") from exc
    return path


@dataclass(frozen=True)
class DataLayout:
    """Where everything lives inside the one data folder."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))   # a plain string is accepted too

    @property
    def index(self) -> Path:
        return self.root / "index.sqlite"

    @property
    def config(self) -> Path:
        return self.root / "config.toml"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    def index_bytes(self) -> int:
        """The size of the index on disk right now, with the files SQLite keeps beside it."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.stat(str(self.index) + suffix).st_size
            except OSError:
                pass
        return total

    def free_bytes(self) -> Optional[int]:
        """Free space on the drive that holds the data folder, or None when it cannot be told."""
        import shutil
        try:
            return int(shutil.disk_usage(self.root).free)
        except OSError:
            return None

    def folders(self) -> list:
        return [self.logs, self.cache, self.models, self.tmp]

    def ensure(self) -> "DataLayout":
        for folder in self.folders():
            folder.mkdir(parents=True, exist_ok=True)
        return self
