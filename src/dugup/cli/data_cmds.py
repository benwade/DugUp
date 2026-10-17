"""The `data` command: where the data folder is, and moving it beside the program."""

from __future__ import annotations

from typing import Callable, Optional

from .. import datamove
from ..datadir import ENV_DATA_DIR, BesideChoice, beside_program, kept_elsewhere, resolve_data_dir
from .output import format_bytes, safe_write

MOVE_NOTE = ("Note: DugUp found your index in {old}. DugUp now keeps its data beside the program, in {new}. "
             "To move it there: dugup data move. To keep using it where it is: dugup data keep. "
             "Until you choose, it stays in use where it is.")


def pending_move_note(args, environ) -> str:
    """The note to show while an older index waits to be moved or kept, or ""."""
    beside = beside_program(getattr(args, "data_dir", None), environ)
    if beside is None or beside.move_from is None:
        return ""
    return MOVE_NOTE.format(old=beside.move_from, new=beside.home.data)


def _where(args, environ, beside: Optional[BesideChoice], out) -> int:
    folder = resolve_data_dir(args.data_dir, environ=environ, allow_synced=True)
    safe_write(f"Data folder in use: {folder}", out)
    if args.data_dir:
        safe_write("Chosen by --data-dir.", out)
    elif environ.get(ENV_DATA_DIR):
        safe_write(f"Chosen by the {ENV_DATA_DIR} setting.", out)
    elif beside is None:
        safe_write("The per-user folder: there is no marker beside the program saying otherwise.", out)
    elif beside.move_from is not None:
        safe_write(f"Your index is here, in the per-user folder; the folder beside the program ({beside.home.data}) "
                   "has none yet. To move it there: dugup data move. To keep it here: dugup data keep.", out)
    elif kept_elsewhere(beside.home.data) is not None and folder != beside.home.data:
        safe_write(f"You chose to keep using this folder (the choice is noted in {beside.home.data}). "
                   "To move it beside the program after all: dugup data move.", out)
    else:
        safe_write(f"Beside the program, in {beside.home.folder}.", out)
    return 0


def _source(beside: BesideChoice):
    if beside.move_from is not None:
        return beside.move_from
    kept = kept_elsewhere(beside.home.data)
    return kept if kept is not None and kept.is_dir() and kept != beside.home.data else None


def _move(args, beside: Optional[BesideChoice], out, ask: Callable[[str], str]) -> int:
    if beside is None:
        safe_write("There is nowhere to move it: no marker beside the program names a folder for the data.", out)
        return 1
    old = _source(beside)
    if old is None:
        safe_write(f"Nothing to move: DugUp already keeps its data beside the program, in {beside.home.data}.", out)
        return 0
    new = beside.home.data
    if not args.yes:
        answer = ask(f"Move DugUp's data from {old} to {new}? The old folder is removed once the copy is checked. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            safe_write("Nothing was moved.", out)
            return 0
    shown = [-1]

    def tell(done: int, total: int) -> None:
        percent = int(100 * done / total) if total else 100
        if percent >= shown[0] + 10:
            shown[0] = percent
            safe_write(f"  {percent}% ({format_bytes(done)} of {format_bytes(total)})", out)

    try:
        moved = datamove.move(old, new, tell=tell)
    except datamove.MoveError as exc:
        safe_write(f"The data was not moved: {exc.reason}.", out)
        return 1
    safe_write(f"Moved: {moved.files:,} files, {format_bytes(moved.bytes)}, now in {moved.new}.", out)
    if moved.startup_updated:
        safe_write("The tray's start-at-login entry now points there too.", out)
    if moved.left_behind:
        safe_write(f"The old folder could not be removed: {moved.left_behind}.", out)
    return 0


def _keep(beside: Optional[BesideChoice], out) -> int:
    if beside is None:
        safe_write("There is no folder beside the program to choose against; the per-user folder is in use.", out)
        return 0
    if beside.move_from is None:
        kept = kept_elsewhere(beside.home.data)
        if kept is not None:
            safe_write(f"Already chosen: DugUp keeps using {kept}.", out)
        else:
            safe_write(f"Nothing to keep: DugUp's data is beside the program, in {beside.home.data}.", out)
        return 0
    datamove.keep_where_it_is(beside.home.data, beside.move_from)
    safe_write(f"DugUp will keep using {beside.move_from}, and will not ask again. "
               "To move it beside the program later: dugup data move.", out)
    return 0


def cmd_data(args, env, out, environ, ask: Callable[[str], str] = input) -> int:
    beside = beside_program(args.data_dir, environ)
    if args.data_command == "where":
        return _where(args, environ, beside, out)
    if args.data_command == "move":
        return _move(args, beside, out, ask)
    return _keep(beside, out)

