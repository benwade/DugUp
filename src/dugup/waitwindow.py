"""A small window that says something is going on, in the middle of the screen where it will be seen.

A line at the bottom of a window is not seen. Anything a person has to wait for gets this: what is being
done, how far it has got, a bar that moves, and a way to stop.
"""

from __future__ import annotations

from typing import Callable, Optional


class WaitWindow:
    def __init__(self, parent, title: str, text: str, on_stop: Optional[Callable[[], None]] = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.closed = False
        self.on_stop = on_stop
        win = self.window = tk.Toplevel(parent)
        win.title(title)
        win.resizable(False, False)
        shown = _shown(parent)
        try:
            if shown:
                win.transient(parent)         # stays in front of the main window
        except tk.TclError:                   # (a window tied to a minimized one would be hidden with it)
            pass
        self.text = tk.StringVar(value=text)
        ttk.Label(win, textvariable=self.text, wraplength=520, justify="left").grid(row=0, column=0, columnspan=2,
                                                                                    sticky="w", padx=16, pady=(16, 8))
        self.bar = ttk.Progressbar(win, mode="indeterminate", length=520)
        self.bar.grid(row=1, column=0, columnspan=2, padx=16, pady=4)
        self.bar.start(40)
        self.stop_button = ttk.Button(win, text="Stop", command=self.stop)
        self.stop_button.grid(row=2, column=1, sticky="e", padx=16, pady=(8, 14))
        if on_stop is None:
            self.stop_button.grid_remove()
        win.protocol("WM_DELETE_WINDOW", self.stop)
        win.update_idletasks()
        try:                                   # in the middle of the window it belongs to, or of the screen
            if shown:
                x = parent.winfo_rootx() + max(0, (parent.winfo_width() - win.winfo_width()) // 2)
                y = parent.winfo_rooty() + max(0, (parent.winfo_height() - win.winfo_height()) // 3)
            else:
                x = max(0, (win.winfo_screenwidth() - win.winfo_width()) // 2)
                y = max(0, (win.winfo_screenheight() - win.winfo_height()) // 3)
            win.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        bring_to_front(win)

    def say(self, text: str) -> None:
        if not self.closed:
            self.text.set(text)

    def stop(self) -> None:
        if self.closed:
            return
        self.text.set("Stopping ...")
        self.stop_button.configure(state="disabled")
        if self.on_stop is not None:
            self.on_stop()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.bar.stop()
        except Exception:
            pass
        self.window.destroy()
        self.text = None            # let go on the window's thread (see pickerwindow.py)


def _shown(window) -> bool:
    try:
        return window.state() == "normal"
    except Exception:
        return False


def bring_to_front(window) -> None:
    """Put a window in front of the others and give it the keyboard. It is made "always on top" for a moment
    only, because Windows otherwise leaves a new window behind the one in use."""
    try:
        window.lift()
        window.attributes("-topmost", True)
        window.after(300, lambda: _not_topmost(window))
        window.focus_force()
    except Exception:
        pass


def _not_topmost(window) -> None:
    try:
        window.attributes("-topmost", False)
    except Exception:
        pass
