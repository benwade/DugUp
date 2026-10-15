"""A grid of pictures for the window: thumbnails of the results that are pictures, side by side, with their
scores, so likeness can be judged by eye. Thumbnails are made on a background thread (opening a photo is the
slow part) and handed to the window through its own event queue; the grid keeps them for the session.

Nothing here touches the index. With the picture library (pillow) missing, the grid shows names only.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import ttk

THUMB = 160            # the longer side of a thumbnail, in pixels
GAP = 8
LABEL_LINES = 2
TILE_W = THUMB + GAP
TILE_H = THUMB + 34 + GAP
KEEP = 600             # thumbnails kept in memory for the session, at most


def bind_wheel(area, canvas) -> None:
    """Scroll `canvas` with the mouse wheel while the pointer is anywhere over `area`, children included
    (tkinter delivers the wheel to the widget under the pointer, so the binding is made for the whole
    application while the pointer is inside and taken off when it leaves). Windows and macOS send
    <MouseWheel> with a delta; X11 sends Button-4 and Button-5."""
    def on_wheel(event):
        if event.delta:
            canvas.yview_scroll(-int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1), "units")
        return "break"

    def enter(event):
        area.bind_all("<MouseWheel>", on_wheel)
        area.bind_all("<Button-4>", lambda e: (canvas.yview_scroll(-1, "units"), "break")[1])
        area.bind_all("<Button-5>", lambda e: (canvas.yview_scroll(1, "units"), "break")[1])

    def leave(event):
        for name in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            area.unbind_all(name)

    area.bind("<Enter>", enter)
    area.bind("<Leave>", leave)


class Thumbnails:
    """Makes thumbnails in the background. `ask(path)` queues one; the result arrives as ("thumb", path,
    image or None) on the queue given, and is kept so a second ask answers at once."""

    def __init__(self, events: "queue.Queue") -> None:
        self.events = events
        self.made: Dict[str, object] = {}         # path -> PIL image (or None when it could not be made)
        self.wanted: "queue.Queue[str]" = queue.Queue()
        self.asked: set = set()
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    def ask(self, path: str) -> Optional[object]:
        with self.lock:
            if path in self.made:
                return self.made[path]
            if path in self.asked:
                return None
            self.asked.add(path)
        self.wanted.put(path)
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._work, name="dugup-thumbnails", daemon=True)
            self.thread.start()
        return None

    def _work(self) -> None:
        while True:
            try:
                path = self.wanted.get(timeout=2.0)
            except queue.Empty:
                return
            image = self._make(path)
            with self.lock:
                if len(self.made) >= KEEP:
                    for old in list(self.made)[: KEEP // 4]:
                        del self.made[old]
                self.made[path] = image
            self.events.put(("thumb", path, image))

    @staticmethod
    def _make(path: str):
        try:
            from PIL import Image, ImageOps
            with Image.open(path) as picture:
                picture = ImageOps.exif_transpose(picture)
                picture.thumbnail((THUMB, THUMB))
                return picture.convert("RGB")
        except Exception:
            return None


class PictureGrid(ttk.Frame):
    """The tiles. `rows` are SearchRow-like objects (path, name, rank); a click selects (on_select(index)),
    a double click opens (on_open(index))."""

    def __init__(self, parent, thumbnails: Thumbnails, on_select: Callable[[int], None],
                 on_open: Callable[[int], None]) -> None:
        super().__init__(parent)
        self.thumbnails = thumbnails
        self.on_select, self.on_open = on_select, on_open
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f4f4f4")
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", lambda event: self._reflow())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Double-1>", self._double)
        bind_wheel(self.canvas, self.canvas)
        self.rows: List[object] = []
        self.scores: bool = False
        self.photos: Dict[int, object] = {}          # tile index -> PhotoImage (kept, or tkinter drops it)
        self.selected: Optional[int] = None
        self.columns = 1
        self.heading = ""

    def show(self, rows: Sequence[object], scores: bool, heading: str = "") -> None:
        self.rows, self.scores, self.heading = list(rows), scores, heading
        self.photos.clear()
        self.selected = None
        self._reflow()

    def add(self, rows: Sequence[object]) -> None:
        self.rows.extend(rows)
        self._reflow()

    def select(self, index: Optional[int]) -> None:
        self.selected = index
        self._reflow()
        if index is not None and self.rows:
            row_at = index // max(1, self.columns)
            total_rows = (len(self.rows) + self.columns - 1) // max(1, self.columns)
            self.canvas.yview_moveto(row_at / max(1, total_rows))

    def thumb_arrived(self, path: str) -> None:
        """A thumbnail is ready: redraw if a tile wants it."""
        if any(getattr(r, "path", "") == path for r in self.rows):
            self._reflow()

    # -- drawing

    def _reflow(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(TILE_W, canvas.winfo_width())
        self.columns = max(1, width // TILE_W)
        top = 0
        if self.heading:
            canvas.create_text(GAP, GAP, anchor="nw", text=self.heading, font=("TkDefaultFont", 10, "bold"))
            top = 24
        if not self.rows:
            canvas.create_text(GAP, top + GAP, anchor="nw", text="No pictures to show. Search by looks, or choose a picture "
                                                               "and press \"Pictures like this\".", fill="#555555")
            canvas.configure(scrollregion=(0, 0, width, 60))
            return
        for index, row in enumerate(self.rows):
            col, line = index % self.columns, index // self.columns
            x, y = GAP + col * TILE_W, top + GAP + line * TILE_H
            if index == self.selected:
                canvas.create_rectangle(x - 3, y - 3, x + THUMB + 3, y + THUMB + 30, outline="#2b6cb0", width=3)
            path = getattr(row, "path", "")
            image = self.thumbnails.ask(path)
            if image is not None:
                photo = self.photos.get(index)
                if photo is None:
                    try:
                        from PIL import ImageTk
                        photo = ImageTk.PhotoImage(image)
                    except Exception:
                        photo = None
                    if photo is not None:
                        self.photos[index] = photo
                if photo is not None:
                    canvas.create_image(x + THUMB // 2, y + THUMB // 2, image=photo)
            else:
                canvas.create_rectangle(x, y, x + THUMB, y + THUMB, fill="#e4e4e4", outline="#cccccc")
                canvas.create_text(x + THUMB // 2, y + THUMB // 2, text="..." if path not in self.thumbnails.made else "no picture",
                                   fill="#888888")
            name = getattr(row, "name", os.path.basename(path))
            label = name if len(name) <= 22 else name[:19] + "..."
            score = f"  {getattr(row, 'rank', 0.0):.2f}" if self.scores else ""
            canvas.create_text(x + THUMB // 2, y + THUMB + 4, anchor="n", text=label + score, width=THUMB)
        lines = (len(self.rows) + self.columns - 1) // self.columns
        canvas.configure(scrollregion=(0, 0, width, top + GAP + lines * TILE_H))

    def _index_at(self, event) -> Optional[int]:
        x = self.canvas.canvasx(event.x) - GAP
        y = self.canvas.canvasy(event.y) - GAP - (24 if self.heading else 0)
        if x < 0 or y < 0:
            return None
        col, line = int(x // TILE_W), int(y // TILE_H)
        if col >= self.columns:
            return None
        index = line * self.columns + col
        return index if 0 <= index < len(self.rows) else None

    def _click(self, event) -> None:
        index = self._index_at(event)
        if index is not None:
            self.selected = index
            self._reflow()
            self.on_select(index)

    def _double(self, event) -> None:
        index = self._index_at(event)
        if index is not None:
            self.on_open(index)
