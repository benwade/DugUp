"""Plumbing the commands share: opening the data folder and service, failures, saving lists."""

from __future__ import annotations

import logging
import logging.handlers
import os
from typing import List, Optional, TextIO
from .. import settings as settings_module
from ..datadir import DataDirError, DataLayout, ensure_data_dir, never_write_from_env, resolve_data_dir
from ..service import LocalService
from ..shapes import Failure
from .output import safe_write


# ---- commands --------------------------------------------------------------------------------------------

def _layout(args, environ, create: bool) -> DataLayout:
    never_write = list(args.never_write) + never_write_from_env(environ)
    data_dir = resolve_data_dir(args.data_dir, environ=environ, never_write=never_write, allow_synced=args.allow_synced)
    if create:
        ensure_data_dir(data_dir, never_write, args.allow_synced)
        return DataLayout(data_dir).ensure()
    return DataLayout(data_dir)


def _notes(args, environ, out: TextIO) -> None:
    """Notes every command shows when they apply: an older index waiting to be moved, administrator rights."""
    from .. import elevated
    from .data_cmds import pending_move_note
    note = pending_move_note(args, environ)
    if note:
        safe_write(note, out)
    if elevated.is_elevated():
        safe_write(elevated.LINE, out)


def _service(args, environ, out: TextIO, create: bool) -> Optional[LocalService]:
    layout = _layout(args, environ, create)
    _notes(args, environ, out)
    if not create and not layout.index.exists():
        safe_write(f"There is no index yet in {layout.root}. Start with: dugup roots add <folder>", out)
        return None
    settings, notes = settings_module.load(layout.config)
    for note in notes:
        safe_write(f"[config] {note}", out)
    return LocalService(layout, settings)


def _fail(failure: Failure, out: TextIO) -> int:
    safe_write(f"Could not do that ({failure.reason.value.replace('_', ' ')}): {failure.detail}", out)
    return 1


def _file_log(layout: DataLayout) -> Optional[logging.Handler]:
    """Counts and reasons only go to the log; never document text, never file names."""
    try:
        layout.logs.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(layout.logs / "dugup.log", maxBytes=1_000_000,
                                                       backupCount=3, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger = logging.getLogger("dugup")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def _chosen_roots(service: LocalService, asked: Optional[List[str]], out: TextIO) -> "Optional[List[str]] | int":
    if not asked:
        return None
    wanted = {os.path.normcase(os.path.abspath(r)) for r in asked}
    roots = [r.path for r in service.list_roots() if os.path.normcase(r.path) in wanted]
    if len(roots) != len(wanted):
        safe_write("One of the --root folders is not an indexed folder. See: dugup roots list", out)
        return 1
    return roots


def save_lines(target: str, lines, args, environ) -> str:
    """Write lines as UTF-8 (with a mark, so spreadsheets read it). Returns "" when done, else why not. Never into
    a folder the program must not write to or one that looks cloud-synced, and never over a file that is there."""
    from ..datadir import check_allowed
    path = os.path.abspath(os.path.expanduser(target))
    try:
        check_allowed(path, list(args.never_write) + never_write_from_env(environ), args.allow_synced)
    except DataDirError as exc:
        return f"The list was not written: {path} is {exc}."
    if os.path.exists(path):
        return f"The list was not written: {path} is there already. Give another name."
    try:
        with open(path, "x", encoding="utf-8-sig", newline="\r\n" if os.name == "nt" else "\n") as handle:
            for line in lines:
                handle.write(line + "\n")
    except OSError as exc:
        return f"The list was not written: {type(exc).__name__}: {exc}"
    return ""


def save_file_list(target: str, records, args, environ) -> str:
    """Write "path<TAB>reason" lines as UTF-8. Returns "" when done, else why not. Never into a folder the
    program must not write to or one that looks cloud-synced, and never over a file that is there already."""
    from ..datadir import check_allowed
    path = os.path.abspath(os.path.expanduser(target))
    try:
        check_allowed(path, list(args.never_write) + never_write_from_env(environ), args.allow_synced)
    except DataDirError as exc:
        return f"The list was not written: {path} is {exc}."
    if os.path.exists(path):
        return f"The list was not written: {path} is there already. Give another name."
    try:
        with open(path, "x", encoding="utf-8-sig", newline="\r\n" if os.name == "nt" else "\n") as handle:
            for record in records:
                handle.write(f"{record.path}\t{' '.join(str(record.error).split())}\n")
    except OSError as exc:
        return f"The list was not written: {type(exc).__name__}: {exc}"
    return ""
