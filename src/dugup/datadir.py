"""The one data folder (guide 5a and 5c).

Everything the program writes lives under one folder, so deleting the program's
data is deleting that folder. The folder is chosen, in order, by the command
line, the environment, then the per-user default. It is never inside a folder
the user said the program must not write to, and by default never inside a
folder that looks cloud-synced.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

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


def never_write_from_env(environ: Optional[Mapping[str, str]] = None) -> list:
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_NEVER_WRITE, "")
    return [item for item in raw.split(os.pathsep) if item]


def resolve_data_dir(
    cli_value: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    default: Callable[[], Path] = default_data_dir,
    never_write: Iterable[os.PathLike | str] = (),
    allow_synced: bool = False,
) -> Path:
    """Pick the data folder: command line, then environment, then the default."""
    environ = os.environ if environ is None else environ
    chosen = cli_value or environ.get(ENV_DATA_DIR) or None
    path = Path(chosen).expanduser() if chosen else Path(default())
    path = Path(os.path.abspath(path))
    check_allowed(path, never_write, allow_synced)
    return path


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

    def folders(self) -> list:
        return [self.logs, self.cache, self.models, self.tmp]

    def ensure(self) -> "DataLayout":
        for folder in self.folders():
            folder.mkdir(parents=True, exist_ok=True)
        return self
