"""The search box that a key brings up anywhere: type, see files, Enter opens one.

It is small on purpose. Names are searched first, in the folder the main window was last set to, then in
every folder; words in the files come after, when names did not find enough. Everything it does is done at
once on the window thread except opening the file, which goes to the desktop on a short thread, as the main
window does (a file on a cloud drive can take seconds to arrive). Ctrl+Enter hands the words to the full
window for the heavier searches.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional, Sequence, Tuple

from .shapes import Failure, SearchFilters, SearchRow, WHERE_ANYWHERE, WHERE_NAME

ROWS = 12                # results shown at most
SETTLE_MS = 120          # typing pauses this long before the search runs
WIDTH = 720


class Popup:
    def __init__(self, root: tk.Misc, service, folder_first: Callable[[], str], open_file: Callable[[str], str],
                 hand_to_window: Callable[[str], None], say: Optional[Callable[[str], None]] = None,
                 present_only: Callable[[], bool] = lambda: False, size: Optional[Tuple[int, int]] = None,
                 on_resize: Optional[Callable[[Tuple[int, int]], None]] = None) -> None:
        self.service, self.folder_first, self.open_file, self.hand_to_window = service, folder_first, open_file, hand_to_window
        self.present_only, self.on_resize = present_only, on_resize
        self.say = say or (lambda text: None)
        self.rows: List[SearchRow] = []
        self.pending: Optional[str] = None
        self.last_query = ""
        self.opener: Optional[threading.Thread] = None
        top = self.top = tk.Toplevel(root)
        top.withdraw()
        top.title("DugUp")
        top.attributes("-topmost", True)
        top.minsize(420, 200)
        top.bind("<Configure>", self._sized)
        self.size: Optional[Tuple[int, int]] = tuple(size) if size else None   # remembered between showings, and runs
        top.protocol("WM_DELETE_WINDOW", self.hide)
        frame = ttk.Frame(top, padding=8)
        frame.pack(fill="both", expand=True)
        self.query = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.query, font=("TkDefaultFont", 13), width=60)
        self.entry.pack(fill="x")
        self.entry.bind("<KeyRelease>", self._typed)
        self.list = ttk.Treeview(frame, columns=("name", "where"), show="", height=ROWS, selectmode="browse")
        self.list.column("name", width=300, stretch=False)
        self.list.column("where", width=WIDTH - 316)
        self.list.pack(fill="both", expand=True, pady=(6, 0))
        foot = ttk.Frame(frame)
        foot.pack(fill="x", pady=(4, 0))
        self.note = tk.StringVar(value="Enter opens the file, Ctrl+Enter searches in the window, Esc closes.")
        ttk.Label(foot, textvariable=self.note, foreground="#666666").pack(side="left", fill="x", expand=True)
        self.window_button = ttk.Button(foot, text="Open the full window", command=self._to_window)
        self.window_button.pack(side="right")
        ttk.Button(foot, text="?", width=2, command=self.about).pack(side="right", padx=(0, 4))
        for widget in (top, self.entry, self.list):
            widget.bind("<Escape>", lambda event: self.hide())
            widget.bind("<Return>", self._enter)
            widget.bind("<Control-Return>", lambda event: (self._to_window(), "break")[1])
        self.entry.bind("<Down>", lambda event: (self._move(1), "break")[1])
        self.entry.bind("<Up>", lambda event: (self._move(-1), "break")[1])
        self.list.bind("<Double-1>", self._enter)

    # -- showing and hiding

    def show(self) -> None:
        top = self.top
        top.update_idletasks()
        width, height = self.size or (WIDTH, max(top.winfo_reqheight(), 120))
        x = (top.winfo_screenwidth() - width) // 2
        y = max(40, top.winfo_screenheight() // 3 - height // 2)
        top.geometry(f"{width}x{height}+{x}+{y}")
        top.deiconify()
        top.lift()
        top.attributes("-topmost", True)
        self.entry.focus_force()
        self.entry.select_range(0, "end")
        self.entry.icursor("end")
        if self.query.get().strip() and not self.rows:
            self.search_now()

    def hide(self) -> None:
        self.top.withdraw()

    def _sized(self, event) -> None:
        if self.shown and event.widget is self.top and event.width > 50 and event.height > 50:
            self.size = (event.width, event.height)
            if self.on_resize is not None:
                self.on_resize(self.size)

    def about(self) -> None:
        from tkinter import messagebox
        from .about import about_text
        messagebox.showinfo("About DugUp", about_text(self.service.layout.root), parent=self.top)

    @property
    def shown(self) -> bool:
        return self.top.state() != "withdrawn"

    # -- searching

    def _typed(self, event=None) -> None:
        if event is not None and event.keysym in ("Up", "Down", "Return", "Escape", "Control_L", "Control_R", "Shift_L", "Shift_R"):
            return
        if self.pending is not None:
            self.top.after_cancel(self.pending)
        self.pending = self.top.after(SETTLE_MS, self.search_now)

    def search_now(self) -> None:
        self.pending = None
        words = self.query.get().strip()
        self.last_query = words
        self.rows = []
        if not words:
            self._fill([], "")
            return
        found: List[SearchRow] = []
        seen = set()
        folder = self.folder_first() or ""
        here = self.present_only()          # the window's "Only files that are here now" tick, remembered
        steps = []
        if folder:
            steps.append(SearchFilters(folder=folder, where=WHERE_NAME, present_only=here))
        steps.append(SearchFilters(where=WHERE_NAME, present_only=here))
        steps.append(SearchFilters(where=WHERE_ANYWHERE, present_only=here))
        note = ""
        for filters in steps:
            if len(found) >= ROWS:
                break
            page = self.service.search(words, ROWS, 0, "rank", False, filters)
            if isinstance(page, Failure):
                note = f"The search could not be done: {page.detail}"
                break
            for row in page.rows:
                if row.path not in seen:
                    seen.add(row.path)
                    found.append(row)
                if len(found) >= ROWS:
                    break
        self.rows = found
        self._fill(found, note or ("" if found else "Nothing found by name or by words. Ctrl+Enter tries the window."))

    def _fill(self, rows: Sequence[SearchRow], note: str) -> None:
        self.list.delete(*self.list.get_children())
        for at, row in enumerate(rows):
            self.list.insert("", "end", iid=str(at), values=(row.name, row.folder))
        if rows:
            self.list.selection_set("0")
            self.list.focus("0")
        self.note.set(note or "Enter opens the file, Ctrl+Enter searches in the window, Esc closes.")

    def _move(self, step: int) -> None:
        if not self.rows:
            return
        chosen = self.list.selection()
        at = int(chosen[0]) if chosen else -1
        at = max(0, min(len(self.rows) - 1, at + step))
        self.list.selection_set(str(at))
        self.list.focus(str(at))
        self.list.see(str(at))

    def chosen(self) -> Optional[SearchRow]:
        picked = self.list.selection()
        if not picked:
            return self.rows[0] if self.rows else None
        at = int(picked[0])
        return self.rows[at] if 0 <= at < len(self.rows) else None

    def _enter(self, event=None) -> str:
        if self.pending is not None:       # typed and pressed Enter at once: search first, then open
            self.top.after_cancel(self.pending)
            self.search_now()
        row = self.chosen()
        if row is None:
            return "break"
        self.open(row.path)
        self.hide()
        return "break"

    def open(self, path: str) -> None:
        def hand():
            try:
                problem = self.open_file(path)
            except Exception as exc:
                problem = f"Could not open it: {type(exc).__name__}: {exc}"
            if problem:
                self.say(problem)

        self.opener = threading.Thread(target=hand, name="dugup-popup-open", daemon=True)
        self.opener.start()

    def _to_window(self) -> None:
        self.hide()
        self.hand_to_window(self.query.get().strip())
