"""Downloading DugUp's optional models in a small window: what, how big, how far, and a way to stop.

The installer opens this when "download everything now" is ticked; `dugup download --window` opens it too.
The downloads run on a worker thread; the window only reads how far they have got. Stopping keeps what came
so far, and the next run continues from there.
"""

from __future__ import annotations

import gc
import threading
from typing import List

from . import downloads
from .models import megabytes
from .shapes import Failure, FailureReason


def run_in_window(chosen: List[downloads.Download], layout, keep_free: int, notice: str = "",
                  fetch=downloads.fetch, root=None, close_when_done: bool = False) -> int:
    """Show the window and download. 0 when everything chosen is here at the end, 1 otherwise."""
    import tkinter as tk
    from tkinter import ttk

    own_root = root is None
    root = root or tk.Tk()
    if own_root:
        from .identity import dress
        dress(root)
    root.title("DugUp: downloading")
    root.resizable(False, False)
    room = downloads.room_for(chosen, layout.free_bytes(), keep_free)
    text = []
    if notice:
        text.append(notice)
        text.append("")
    text.append(downloads.describe(chosen, room))
    ttk.Label(root, text="\n".join(text), wraplength=560, justify="left").grid(row=0, column=0, columnspan=2,
                                                                              sticky="w", padx=16, pady=(16, 8))
    total = max(1, room.needed)
    bar = ttk.Progressbar(root, mode="determinate", length=560, maximum=total)
    bar.grid(row=1, column=0, columnspan=2, padx=16, pady=4)
    status = ttk.Label(root, text="Starting ...", wraplength=560, justify="left")
    status.grid(row=2, column=0, columnspan=2, sticky="w", padx=16, pady=4)
    stopping = threading.Event()
    finished = threading.Event()
    state = {"done": 0, "total": room.needed, "title": "", "result": None}
    button = ttk.Button(root, text="Stop", command=stopping.set)
    button.grid(row=3, column=1, sticky="e", padx=16, pady=(8, 14))
    closed = {"value": False}

    def close() -> None:
        closed["value"] = True
        stopping.set()
        if finished.is_set() or not state.get("started"):
            root.quit()

    root.protocol("WM_DELETE_WINDOW", close)

    def work() -> None:
        try:
            state["result"] = fetch(chosen, lambda d, t, title: state.update(done=d, total=t, title=title),
                                    stopping, layout.free_bytes, keep_free)
        except Exception as exc:                  # said in the window, never raised into its loop
            state["result"] = Failure(FailureReason.OTHER, f"{type(exc).__name__}: {exc}")
        finally:
            finished.set()

    def end() -> None:
        from .cli.download_cmds import summary
        got = state["result"]
        bar.configure(value=total if not isinstance(got, Failure) and not got.failed and not got.stopped
                      else bar.cget("value"))
        status.configure(text=summary(got, chosen))
        button.configure(text="Close", command=root.quit)
        if close_when_done or closed["value"]:
            root.quit()

    def poll() -> None:
        if finished.is_set():
            end()
            return
        bar.configure(value=min(state["done"], total))
        if state["title"]:
            whole = state["total"] or total
            status.configure(text=f"{state['title']}: {megabytes(state['done'])} of {megabytes(whole)} "
                                  f"({int(100 * state['done'] / whole) if whole else 100}%)"
                                  + ("  Stopping ..." if stopping.is_set() else ""))
        root.after(100, poll)

    if not room.needed:
        status.configure(text="Everything chosen is already here.")
        button.configure(text="Close", command=root.quit)
        state["result"] = downloads.Fetched([d.key for d in chosen], [])
        finished.set()
    elif not room.enough:
        status.configure(text="Nothing was downloaded: there is not enough room on that drive.")
        button.configure(text="Close", command=root.quit)
        state["result"] = Failure(FailureReason.OTHER, "not enough room")
        finished.set()
    else:
        state["started"] = True
        threading.Thread(target=work, name="dugup-downloads", daemon=True).start()
        root.after(100, poll)
    if close_when_done and finished.is_set():
        root.after_idle(root.quit)
    root.mainloop()
    got = state["result"]
    ok = got is not None and not isinstance(got, Failure) and not got.failed and not got.stopped
    if own_root:
        root.destroy()
    else:
        for child in root.winfo_children():
            child.destroy()
    gc.collect()                                 # a closed window's leftovers go now, on this thread
    return 0 if ok else 1
