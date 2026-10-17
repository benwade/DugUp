"""What the window says before it opens, when there is something to say.

Two notes, each shown only when it applies:

- An index found in the older per-user folder, with the folder beside the program still empty: DugUp offers
  to move it in. "Keep it where it is" is remembered and never asked again; closing the question asks again
  next time. Either way nothing is lost: until it is moved, the older folder stays in use.
- Administrator rights: a short note that nothing is wrong, and that starting DugUp the ordinary way is the
  safer habit. It never stops anything.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from . import datamove, elevated

MOVE, KEEP, LATER = "move", "keep", "later"


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
    return choice


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
    if "why" in outcome:
        return outcome["why"]
    moved = outcome["moved"]
    return f"left:{moved.left_behind}" if moved.left_behind else ""


def show(parent, title: str, text: str) -> None:
    from tkinter import messagebox
    messagebox.showinfo(title, text, parent=parent)


def before_window(parent, args, environ, asker: Callable = ask_move, mover: Callable = run_move,
                  keeper: Callable = datamove.keep_where_it_is, tell: Callable = show,
                  is_elevated: Optional[Callable[[], bool]] = None) -> Optional[str]:
    """Show what applies before the main window opens. Returns MOVE, KEEP or LATER when the move was offered
    (MOVE only when it succeeded), or None when it was not."""
    from .datadir import beside_program

    if (is_elevated or elevated.is_elevated)():
        tell(parent, elevated.TITLE, elevated.TEXT)
    beside = beside_program(getattr(args, "data_dir", None), environ)
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
        tell(parent, "DugUp", f"Moved. The old folder could not be removed: {why[5:]}.")
        return MOVE
    if why:
        tell(parent, "DugUp", f"Your index was not moved: {why}.\n\nNothing was lost; DugUp keeps using it where "
                              "it is and will offer again next time.")
        return LATER
    return MOVE
