"""A small remote control for the tray: start it, bring up the search box or the full window, update now,
free the graphics card and memory, quit; and a tick that starts the tray at login.

The remote never holds the index itself. Everything goes through the tray's summons, so closing the
remote changes nothing, and it works wherever `dugup tray` does. What the tray is doing is asked on a
thread of its own (a summons waits up to a few seconds when the tray is busy) and shown in a read-only
line at the top of the window, where a person looks.
"""

from __future__ import annotations

import queue
import sys
import threading
from typing import Callable, Optional

from . import summons, tray as tray_module

ASK_MS = 2000               # how often the tray is asked what it is doing
POLL_MS = 100
NOT_RUNNING = "The tray is not running."
FREE = "Free the graphics card and memory"
LOAD = "Load the models"
LOADING = "Loading the models..."


def startup_label(platform: Optional[str] = None) -> str:
    platform = platform or sys.platform
    return "Start with Windows" if platform.startswith("win") else "Start when I log in"


class Remote:
    """`ask(root, command)` is summons.summon; `launch(args)` starts a detached process; the startup
    three are tray.install_startup / remove_startup / startup_entry, each a seam for tests."""

    def __init__(self, root, layout, *, ask: Callable = summons.summon, launch: Callable = tray_module._detached,
                 install_startup: Optional[Callable] = None, remove_startup: Optional[Callable] = None,
                 startup_entry: Optional[Callable] = None, platform: Optional[str] = None,
                 say: Optional[Callable[[str], None]] = None) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.root, self.layout = root, layout
        self.ask, self.launch = ask, launch
        self.install_startup = install_startup or (lambda: tray_module.install_startup(layout))
        self.remove_startup = remove_startup or tray_module.remove_startup
        self.startup_entry = startup_entry or tray_module.startup_entry
        self.platform = platform or sys.platform
        self.say = say or (lambda text: None)
        self.closed = False
        self.answers: "queue.Queue[Optional[str]]" = queue.Queue()
        self.state: Optional[str] = None          # the tray's last answer; None: not running
        self.models: str = "none"                 # the tray's answer to "models": what is loaded, "loading", or "none"
        self.asking = False

        root.title("DugUp")
        frame = ttk.Frame(root, padding=10)
        frame.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="Asking the tray...")
        ttk.Label(frame, text="The tray is").grid(row=0, column=0, sticky="w")
        box = ttk.Entry(frame, textvariable=self.status, state="readonly", width=48)
        box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 8))
        self.buttons = {}
        rows = [("start", "Start tray", self.start_tray), ("popup", "Search box", self.popup),
                ("window", "Full window", self.window), ("update", "Update the index now", self.update),
                ("free", FREE, self.toggle_models), ("quit", "Quit tray", self.quit_tray)]
        for at, (name, label, action) in enumerate(rows, start=1):
            button = ttk.Button(frame, text=label, command=action)
            button.grid(row=at, column=0, columnspan=3, sticky="ew", pady=2)
            self.buttons[name] = button
        self.at_login = tk.BooleanVar(value=self.startup_entry().exists())
        ttk.Checkbutton(frame, text=startup_label(self.platform), variable=self.at_login,
                        command=self.toggle_startup).grid(row=len(rows) + 1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.note = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.note, state="readonly", width=48).grid(row=len(rows) + 2, column=0, columnspan=3,
                                                                                  sticky="ew", pady=(8, 0))
        frame.columnconfigure(1, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._show(None, None)
        self._ask_id = root.after(0, self._ask)
        self._poll_id = root.after(POLL_MS, self._poll)

    # -- what the tray is doing

    def _ask(self) -> None:
        if self.closed:
            return
        if not self.asking:
            self.asking = True
            threading.Thread(target=self._ask_now, daemon=True, name="dugup-remote-ask").start()
        self._ask_id = self.root.after(ASK_MS, self._ask)

    def _ask_now(self) -> None:
        try:
            answer = self.ask(self.layout.root, "state")
            models = self.ask(self.layout.root, "models") if answer is not None else None
        except Exception as exc:               # a summons never raises; a seam in a test might
            answer, models = f"could not be asked ({type(exc).__name__})", None
        self.answers.put((answer, models))

    def _poll(self) -> None:
        if self.closed:
            return
        while True:
            try:
                answer = self.answers.get_nowait()
            except queue.Empty:
                break
            self.asking = False
            self._show(*answer)
        self._poll_id = self.root.after(POLL_MS, self._poll)

    def _show(self, answer: Optional[str], models: Optional[str] = None) -> None:
        self.state = answer
        self.models = models or "none"
        running = answer is not None
        self.status.set(answer if running else NOT_RUNNING)
        self.buttons["start"].state(["disabled"] if running else ["!disabled"])
        for name in ("popup", "update", "free", "quit"):
            self.buttons[name].state(["!disabled"] if running else ["disabled"])
        if self.models == "loading":
            self.buttons["free"].configure(text=LOADING)
            self.buttons["free"].state(["disabled"])
        else:
            self.buttons["free"].configure(text=FREE if self.models != "none" else LOAD)

    @property
    def models_loaded(self) -> bool:
        return self.models not in ("none", "loading")

    def refresh(self) -> None:
        """Ask again soon (after a button), rather than waiting for the next tick."""
        try:
            self.root.after_cancel(self._ask_id)
        except Exception:
            pass
        self._ask_id = self.root.after(300, self._ask)

    # -- the buttons

    def start_tray(self) -> None:
        self.launch(tray_module.dugup_command(self.layout, "tray"))
        self.note.set("Starting the tray; it answers in a few seconds.")
        self.status.set("starting...")
        self.refresh()

    def popup(self) -> None:
        self._send("popup", "The search box was brought up.")

    def window(self) -> None:
        if self.state is not None and self.ask(self.layout.root, "window"):
            self.note.set("The full window is opening.")
        else:
            self.launch(tray_module.dugup_command(self.layout, "window"))
            self.note.set("The full window is opening (without the tray).")

    def update(self) -> None:
        self._send("update", "The index is updating now.")

    def toggle_models(self) -> None:
        """One button both ways: free the card when models are loaded, load them when it is free."""
        if self.models_loaded:
            self.free()
        else:
            self.load()

    def free(self) -> None:
        self._send("free", "The models were unloaded; the card and memory are free. The index stays warm; background "
                           "updates are paused until the models are loaded again or Update now.")

    def load(self) -> None:
        self._send("load", "Loading the models (a few seconds); background updates resume.")

    def quit_tray(self) -> None:
        self._send("quit", "The tray was told to quit; it unloads every model on the way out.")

    def _send(self, command: str, done: str) -> None:
        answer = self.ask(self.layout.root, command)
        self.note.set(done if answer else NOT_RUNNING)
        self.say(self.note.get())
        self.refresh()

    def toggle_startup(self) -> None:
        try:
            if self.at_login.get():
                path = self.install_startup()
                self.note.set(f"The tray will start at login ({path}).")
            else:
                path = self.remove_startup()
                self.note.set("The tray will no longer start at login." if path else "There was no start-at-login entry.")
        except OSError as exc:
            self.at_login.set(self.startup_entry().exists())
            self.note.set(f"The start-at-login entry could not be changed: {exc}")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for job in (self._ask_id, self._poll_id):
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass
