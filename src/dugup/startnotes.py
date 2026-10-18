"""What the window says before it opens, when there is something to say.

Three notes, each shown only when it applies:

- An index found in the older per-user folder, with the folder beside the program still empty: DugUp offers
  to move it in. "Keep it where it is" is remembered and never asked again; closing the question asks again
  next time. Either way nothing is lost: until it is moved, the older folder stays in use.
- The folder beside the program is inside a folder a sync app uploads, so DugUp keeps the index in the
  per-user folder instead: said once, so it is never a silent surprise.
- Administrator rights: a short note that nothing is wrong, and that starting DugUp the ordinary way is the
  safer habit. It never stops anything.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from . import datamove, elevated

MOVE, KEEP, LATER = "move", "keep", "later"
SYNCED_TOLD = "synced-note-shown.txt"   # in the folder in use: the synced folder already told about


def synced_text(beside) -> str:
    from .syncfolders import CAVEAT
    return (f"DugUp is installed in {beside.home.folder}, which is inside {beside.synced.path}. {beside.synced.by} "
            "syncs that folder to the cloud, and DugUp's index holds the text of your files, so DugUp keeps the index "
            f"out of it, in {beside.path} instead.\n\nNothing is wrong, and nothing needs doing.\n\n"
            "To keep the index in a folder of your own choosing instead: uninstall DugUp (answer No when asked about "
            "deleting its data), install it again, and choose that folder on the page \"Where DugUp keeps its "
            "index\". DugUp then offers to move your index there.\n\n" + CAVEAT)


def tell_synced_once(parent, beside, tell: Callable) -> bool:
    """The synced-folder note, unless it was already shown for this folder. True when it was shown now."""
    import os
    record = beside.path / SYNCED_TOLD
    said = str(beside.home.data)
    try:
        if record.read_text(encoding="utf-8").strip() == said:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    tell(parent, "DugUp: where your index is kept", synced_text(beside))
    try:
        os.makedirs(beside.path, exist_ok=True)
        record.write_text(said + "\n", encoding="utf-8")
    except OSError:
        pass                                  # shown again next time: better than never
    return True


def ask_move(parent, old, new) -> str:
    """The question, as a small window: MOVE, KEEP, or LATER when it is closed."""
    import tkinter as tk
    from tkinter import ttk
    from .waitwindow import bring_to_front

    answer = tk.StringVar(master=parent, value="")
    win = tk.Toplevel(parent)
    win.title("DugUp")
    win.resizable(False, False)
    text = (f"DugUp found your index and settings in:\n{old}\n\n"
            f"DugUp now keeps everything in one folder, beside the program:\n{new}\n\n"
            "Move them there? The old folder is removed only after the copy is checked. "
            "Or keep using them where they are; DugUp will not ask again.")
    ttk.Label(win, text=text, wraplength=520, justify="left").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                   padx=16, pady=(16, 10))
    ttk.Button(win, text="Keep it where it is", command=lambda: answer.set(KEEP)).grid(row=1, column=0, sticky="w",
                                                                                      padx=16, pady=(4, 14))
    ttk.Button(win, text="Move it", command=lambda: answer.set(MOVE)).grid(row=1, column=1, sticky="e",
                                                                           padx=16, pady=(4, 14))
    win.protocol("WM_DELETE_WINDOW", lambda: answer.set(LATER))
    bring_to_front(win)
    win.wait_variable(answer)
    choice = answer.get()
    win.destroy()
    _let_go()
    return choice


def _let_go() -> None:
    """Collect a closed window's leftovers now, on the window's thread. Its tkinter variables sit in reference
    cycles, and collected later on a background thread they abort the process (see pickerwindow.py)."""
    import gc
    gc.collect()


def run_move(parent, old, new, mover: Callable = datamove.move) -> str:
    """Move with a wait window that shows how far it has got and can stop it. "" when moved, or why not."""
    from .waitwindow import WaitWindow

    stopping = threading.Event()
    finished = threading.Event()
    progress = {"done": 0, "total": 0}
    outcome: dict = {}

    def work() -> None:
        try:
            outcome["moved"] = mover(old, new, tell=lambda d, t: progress.update(done=d, total=t),
                                     stop=stopping.is_set)
        except datamove.MoveError as exc:
            outcome["why"] = exc.reason
        except Exception as exc:                 # reported, never raised into the window's loop
            outcome["why"] = f"{type(exc).__name__}: {exc}"
        finally:
            finished.set()

    wait = WaitWindow(parent, "DugUp", "Moving your index and settings ...", on_stop=stopping.set)
    threading.Thread(target=work, name="dugup-move", daemon=True).start()
    while not finished.wait(0.1):
        total = progress["total"]
        if total:
            wait.say(f"Moving your index and settings ... {int(100 * progress['done'] / total)}%")
        parent.update()
    wait.close()
    _let_go()
    if "why" in outcome:
        return outcome["why"]
    moved = outcome["moved"]
    said = [moved.left_behind] if moved.left_behind else []
    said += list(getattr(moved, "notes", []))
    return ("left:" + ". ".join(said)) if said else ""


def show(parent, title: str, text: str) -> None:
    """A note in a small window of its own, brought to the front and kept there until OK. (A standard message
    box over the hidden main window can open behind other windows, and a failed move then looks silent.)"""
    import tkinter as tk
    from tkinter import ttk
    from .waitwindow import bring_to_front

    win = tk.Toplevel(parent)
    win.title(title)
    win.resizable(False, False)
    ttk.Label(win, text=text, wraplength=520, justify="left").grid(row=0, column=0, sticky="w", padx=16, pady=(16, 10))
    ttk.Button(win, text="OK", command=win.destroy).grid(row=1, column=0, sticky="e", padx=16, pady=(4, 14))
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    bring_to_front(win, stay=True)
    parent.wait_window(win)
    _let_go()


def before_window(parent, args, environ, asker: Callable = ask_move, mover: Callable = run_move,
                  keeper: Callable = datamove.keep_where_it_is, tell: Callable = show,
                  is_elevated: Optional[Callable[[], bool]] = None) -> Optional[str]:
    """Show what applies before the main window opens. Returns MOVE, KEEP or LATER when the move was offered
    (MOVE only when it succeeded), or None when it was not."""
    from .datadir import beside_program

    if (is_elevated or elevated.is_elevated)():
        tell(parent, elevated.TITLE, elevated.TEXT)
    beside = beside_program(getattr(args, "data_dir", None), environ)
    if beside is not None and beside.synced is not None:
        tell_synced_once(parent, beside, tell)
    if beside is None or beside.move_from is None:
        return None
    old, new = beside.move_from, beside.home.data
    choice = asker(parent, old, new)
    if choice == KEEP:
        keeper(new, old)
        return KEEP
    if choice != MOVE:
        return LATER
    why = mover(parent, old, new)
    if why.startswith("left:"):
        tell(parent, "DugUp", f"Moved, with a note: {why[5:]}.")
        return MOVE
    if why:
        tell(parent, "DugUp", f"Your index was not moved: {why}.\n\nNothing was lost; DugUp keeps using it where "
                              "it is and will offer again next time.")
        return LATER
    return MOVE
