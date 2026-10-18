"""Which folders on this computer are synced to a cloud service, as the sync apps themselves record it.

A folder's name is not enough: Google Drive for desktop can back up any folder (D:\\Projects, say), and
OneDrive can take over Documents. DugUp keeps the text of your files in its index, so it must not put the
index where a sync app will upload it, unless you say so. The sync apps record what they sync:

- Windows' own register of sync folders (SyncRootManager): OneDrive, and most cloud apps on Windows 10/11
- OneDrive's account settings (its folder, even before Windows lists it)
- Google Drive for desktop: the folders it backs up from this computer and its own drive letter, in its
  settings database (read here without changing it)
- Dropbox: its info.json

When a sync app is there but its list cannot be read, that is said ("unread"), and the folder is treated as
not synced: a warning, never a refusal, since refusing would stop people who use no sync app at all.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class SyncedFolder:
    path: Path
    by: str                 # the app, in words: "Google Drive for desktop", "OneDrive", ...


@dataclass
class Survey:
    folders: List[SyncedFolder] = field(default_factory=list)
    unread: List[str] = field(default_factory=list)       # apps that are here but whose list could not be read

    def synced_by(self, path: os.PathLike | str) -> Optional[SyncedFolder]:
        """The synced folder `path` lies in (or is), or None."""
        target = _norm(path)
        best = None
        for folder in self.folders:
            root = _norm(folder.path)
            try:
                inside = os.path.commonpath([target, root]) == root
            except ValueError:                   # different drives
                inside = False
            if inside and (best is None or len(root) > len(_norm(best.path))):
                best = folder                    # the deepest one names the right app
        return best


def _norm(path: os.PathLike | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


Source = Callable[[], Tuple[List[SyncedFolder], List[str]]]


# ---- the sources ----------------------------------------------------------------------------------------------

def windows_sync_roots() -> Tuple[List[SyncedFolder], List[str]]:
    """HKLM ...\\Explorer\\SyncRootManager\\<provider>!<SID>!<account>\\UserSyncRoots: SID -> folder."""
    try:
        import winreg
    except ImportError:
        return [], []
    found: List[SyncedFolder] = []
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager")
    except OSError:
        return [], []
    for name in _subkeys(winreg, base):
        provider = name.split("!")[0] or "a sync app"
        try:
            roots = winreg.OpenKey(base, name + r"\UserSyncRoots")
        except OSError:
            continue
        index = 0
        while True:
            try:
                _, data, _ = winreg.EnumValue(roots, index)
            except OSError:
                break
            index += 1
            if isinstance(data, str) and data.strip():
                found.append(SyncedFolder(Path(data), provider))
    return found, []


def onedrive_accounts() -> Tuple[List[SyncedFolder], List[str]]:
    try:
        import winreg
    except ImportError:
        return [], []
    try:
        accounts = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts")
    except OSError:
        return [], []
    found = []
    for name in _subkeys(winreg, accounts):
        try:
            folder, _ = winreg.QueryValueEx(winreg.OpenKey(accounts, name), "UserFolder")
        except OSError:
            continue
        if folder:
            found.append(SyncedFolder(Path(folder), "OneDrive"))
    return found, []


def _subkeys(winreg, key) -> Iterable[str]:
    index = 0
    while True:
        try:
            yield winreg.EnumKey(key, index)
        except OSError:
            return
        index += 1


def google_drive(local_app_data: Optional[Path] = None) -> Tuple[List[SyncedFolder], List[str]]:
    """Google Drive for desktop's root_preference_sqlite.db: `roots.last_seen_absolute_path` holds each folder
    it backs up from this computer; `media` rows named "Google Drive" hold its own drive's letter."""
    base = Path(local_app_data or os.environ.get("LOCALAPPDATA") or "") / "Google" / "DriveFS"
    database = base / "root_preference_sqlite.db"
    if not base.exists():
        return [], []
    if not database.is_file():
        return [], ["Google Drive for desktop"]
    rows = _read_rows(database, ("SELECT last_seen_absolute_path FROM roots",
                                 "SELECT last_mount_point FROM media WHERE name = 'Google Drive'"))
    if rows is None:
        return [], ["Google Drive for desktop"]
    folders = [SyncedFolder(Path(value), "Google Drive for desktop") for value in rows
               if isinstance(value, str) and value.strip()]
    return folders, []


def _read_rows(database: Path, queries: Sequence[str]) -> Optional[List[object]]:
    """Every first column the queries return, read without touching the app's own file (a copy is read when
    the app has it open). None when it cannot be read."""
    def read(path: Path) -> List[object]:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            values: List[object] = []
            for query in queries:
                try:
                    values += [row[0] for row in conn.execute(query)]
                except sqlite3.OperationalError as exc:
                    if "no such" not in str(exc):         # a table a newer version renamed: the rest still count
                        raise
            return values
        finally:
            conn.close()
    try:
        return read(database)
    except sqlite3.Error:
        pass
    try:
        with tempfile.TemporaryDirectory(prefix="dugup-sync-") as folder:
            copy = Path(folder) / database.name
            shutil.copyfile(database, copy)
            for suffix in ("-wal", "-shm"):
                side = Path(str(database) + suffix)
                if side.exists():
                    shutil.copyfile(side, Path(str(copy) + suffix))
            return read(copy)
    except (OSError, sqlite3.Error):
        return None


def dropbox(places: Optional[Sequence[Path]] = None) -> Tuple[List[SyncedFolder], List[str]]:
    if places is None:
        places = [Path(os.environ.get(name, "")) / "Dropbox" / "info.json" for name in ("LOCALAPPDATA", "APPDATA")]
        places.append(Path.home() / ".dropbox" / "info.json")
    found: List[SyncedFolder] = []
    unread: List[str] = []
    for info in places:
        if not info.is_file():
            continue
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unread.append("Dropbox")
            continue
        for account in data.values() if isinstance(data, dict) else ():
            if isinstance(account, dict) and isinstance(account.get("path"), str):
                found.append(SyncedFolder(Path(account["path"]), "Dropbox"))
    return found, sorted(set(unread))


DEFAULT_SOURCES: Tuple[Source, ...] = (windows_sync_roots, onedrive_accounts, google_drive, dropbox)

_cached: Optional[Survey] = None


def survey(sources: Optional[Sequence[Source]] = None, fresh: bool = False) -> Survey:
    """What the sync apps here say they sync (looked up once per run unless `fresh`)."""
    global _cached
    if sources is None and _cached is not None and not fresh:
        return _cached
    result = Survey()
    for source in sources if sources is not None else DEFAULT_SOURCES:
        try:
            folders, unread = source()
        except Exception:                      # a sync app's own records are never a reason to stop DugUp
            folders, unread = [], [getattr(source, "__name__", "a sync app")]
        result.folders += folders
        result.unread += [name for name in unread if name not in result.unread]
    if sources is None:
        _cached = result
    return result


def unread_note(path: os.PathLike | str, found: Optional[Survey] = None) -> str:
    """The warning to show when a sync app's list could not be read, or ""."""
    found = found or survey()
    if not found.unread:
        return ""
    return (f"Note from DugUp: it could not read which folders {' and '.join(found.unread)} syncs, so it cannot "
            f"tell whether {path} is synced to the cloud. If it is, the index (the text of your files) is uploaded "
            "with it; dugup data where shows the folder in use.")
