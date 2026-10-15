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
GONE = "gone"          # in `made`: the file was not at its path when the thumbnail was tried


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

    def retry_failures(self) -> None:
        """Forget the ones that could not be made, so they are tried again: on a cloud drive a picture that
        was not fetched yet may well be there now."""
        with self.lock:
            for path in [p for p, image in self.made.items() if image is None or image is GONE]:
                del self.made[path]
                self.asked.discard(path)

    def pending(self) -> int:
        """How many thumbnails are asked for and not yet made."""
        with self.lock:
            return len(self.asked) - len(self.made)

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
        except FileNotFoundError:
            return GONE
        except Exception:
            return None

    def why_not(self, path: str) -> str:
        """What a tile says instead of a thumbnail."""
        if path not in self.made:
            return "loading..."
        return "not here now" if self.made[path] is GONE else "no picture"


class PictureGrid(ttk.Frame):
    """The tiles. `rows` are SearchRow-like objects (path, name, rank). A click selects one tile; Ctrl-click
    adds or removes one; Shift-click takes the range from the last plain click. Every change is reported as
    on_select(indexes), the selected tiles in order; a double click opens (on_open(index))."""

    def __init__(self, parent, thumbnails: Thumbnails, on_select: Callable[[List[int]], None],
                 on_open: Callable[[int], None]) -> None:
        super().__init__(parent)
        self.thumbnails = thumbnails
        self.on_select, self.on_open = on_select, on_open
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f4f4f4")
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._scrolled)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", lambda event: self._reflow())
        self.drawn: Tuple[int, int] = (0, -1)        # the lines of tiles on the canvas right now (first, last)
        self.redraw_pending = False
        self.reflowing = False
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Double-1>", self._double)
        bind_wheel(self.canvas, self.canvas)
        self.rows: List[object] = []
        self.scores: bool = False
        self.photos: Dict[int, object] = {}          # tile index -> PhotoImage (kept, or tkinter drops it)
        self.chosen: List[int] = []                  # the selected tiles, in the order they were chosen
        self.anchor: Optional[int] = None            # where a Shift-click range starts: the last plain click
        self.columns = 1
        self.heading = ""
        self.badges: Dict[str, Tuple[str, str]] = {}      # path -> (text, colour) drawn over the tile's corner

    def show(self, rows: Sequence[object], scores: bool, heading: str = "", badges: Optional[Dict[str, Tuple[str, str]]] = None) -> None:
        self.rows, self.scores, self.heading = list(rows), scores, heading
        self.badges = dict(badges or {})
        self.photos.clear()
        self.thumbnails.retry_failures()
        self.chosen, self.anchor = [], None
        self._reflow()

    def add(self, rows: Sequence[object]) -> None:
        self.rows.extend(rows)
        self._reflow()

    @property
    def selected(self) -> Optional[int]:
        """The first selected tile, or None."""
        return self.chosen[0] if self.chosen else None

    def select(self, index: Optional[int]) -> None:
        """Select one tile (or none) and scroll to it."""
        self.select_many([index] if index is not None else [])

    def select_many(self, indexes: Sequence[int]) -> None:
        """Make exactly these tiles the selection (from the list beside, say); the first is scrolled into view."""
        self.chosen = [i for i in indexes if 0 <= i < len(self.rows)]
        if self.chosen:
            self.anchor = self.chosen[0]
        self._reflow()
        if self.chosen and self.rows:
            row_at = self.chosen[0] // max(1, self.columns)
            total_rows = (len(self.rows) + self.columns - 1) // max(1, self.columns)
            self.canvas.yview_moveto(row_at / max(1, total_rows))

    def thumb_arrived(self, path: str) -> None:
        """A thumbnail is ready: redraw, once, if a tile on screen wants it. Many arrive in a burst, so the
        redraw is put off until the window is idle and they share one."""
        first, last = self.drawn
        if self.thumbnails.pending() == 0:
            self._redraw_soon()          # the last one: the loading note goes
            return
        for index in range(first * self.columns, min(len(self.rows), (last + 1) * self.columns)):
            if getattr(self.rows[index], "path", "") == path:
                self._redraw_soon()
                return

    def _redraw_soon(self) -> None:
        if not self.redraw_pending:
            self.redraw_pending = True
            self.after_idle(self._redraw_now)

    def _redraw_now(self) -> None:
        self.redraw_pending = False
        self._reflow()

    def _scrolled(self, first: str, last: str) -> None:
        """The canvas moved: the scrollbar follows, and tiles that came into view are drawn."""
        self.scroll.set(first, last)
        if not self.reflowing and self._visible_lines() != self.drawn:
            self._redraw_soon()

    def _visible_lines(self) -> Tuple[int, int]:
        """The first and last line of tiles that are on screen, with one line of margin either side."""
        canvas = self.canvas
        top = 24 if self.heading else 0
        height = max(TILE_H, canvas.winfo_height())
        y0 = canvas.canvasy(0)
        first = max(0, int((y0 - top - GAP) // TILE_H) - 1)
        last = int((y0 + height - top - GAP) // TILE_H) + 1
        lines = (len(self.rows) + self.columns - 1) // max(1, self.columns)
        return first, min(last, max(0, lines - 1))

    # -- drawing

    def _reflow(self) -> None:
        """Draw the tiles on screen (and one line either side). The rest exist only as geometry: thousands of
        pictures cost nothing until they are scrolled to, and their thumbnails are not asked for until then."""
        self.reflowing = True
        try:
            self._draw()
        finally:
            self.reflowing = False

    def _draw(self) -> None:
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
            self.drawn = self._visible_lines()
            return
        lines = (len(self.rows) + self.columns - 1) // self.columns
        canvas.configure(scrollregion=(0, 0, width, top + GAP + lines * TILE_H))
        first, last = self._visible_lines()
        self.drawn = (first, last)
        shown = range(first * self.columns, min(len(self.rows), (last + 1) * self.columns))
        for index in list(self.photos):
            if index not in shown:
                del self.photos[index]         # off screen: tkinter's copy goes, the thumbnail itself is kept
        for index in shown:
            row = self.rows[index]
            col, line = index % self.columns, index // self.columns
            x, y = GAP + col * TILE_W, top + GAP + line * TILE_H
            chosen = index in self.chosen
            if chosen:
                canvas.create_rectangle(x - 3, y - 3, x + THUMB + 3, y + THUMB + 30, outline="#2b6cb0", width=3)
            path = getattr(row, "path", "")
            image = self.thumbnails.ask(path)
            if image is GONE:
                image = None
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
                canvas.create_text(x + THUMB // 2, y + THUMB // 2, text=self.thumbnails.why_not(path), fill="#888888")
            badge = self.badges.get(path)
            if badge is not None:
                text, colour = badge
                canvas.create_rectangle(x, y, x + 8 + 7 * len(text), y + 18, fill=colour, outline=colour)
                canvas.create_text(x + 4, y + 9, anchor="w", text=text, fill="white", font=("TkDefaultFont", 8, "bold"))
            if chosen:      # a tick in the top right corner: selected, unmistakably
                canvas.create_oval(x + THUMB - 22, y + 2, x + THUMB - 2, y + 22, fill="#2b6cb0", outline="white", width=2)
                canvas.create_text(x + THUMB - 12, y + 12, text="\u2713", fill="white", font=("TkDefaultFont", 11, "bold"))
            name = getattr(row, "name", os.path.basename(path))
            label = name if len(name) <= 22 else name[:19] + "..."
            score = f"  {getattr(row, 'rank', 0.0):.2f}" if self.scores else ""
            canvas.create_text(x + THUMB // 2, y + THUMB + 4, anchor="n", text=label + score, width=THUMB)
        waiting = self.thumbnails.pending()
        if waiting:         # said where the eye is, in the corner of the grid: on a cloud drive this is a download
            note = f"Loading {waiting:,} thumbnail{'s' if waiting != 1 else ''}..."
            y0 = canvas.canvasy(0) + 4
            canvas.create_rectangle(width - 8 - 7 * len(note), y0, width - 4, y0 + 20, fill="#2b6cb0", outline="#2b6cb0")
            canvas.create_text(width - 8, y0 + 10, anchor="e", text=note, fill="white", font=("TkDefaultFont", 9, "bold"))

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
        if index is None:
            return
        control, shift = bool(event.state & 0x0004), bool(event.state & 0x0001)
        if control:
            if index in self.chosen:
                self.chosen.remove(index)
            else:
                self.chosen.append(index)
            if self.anchor is None:
                self.anchor = index
        elif shift and self.anchor is not None:
            low, high = sorted((self.anchor, index))
            self.chosen = list(range(low, high + 1))
        else:
            self.chosen, self.anchor = [index], index
        self._reflow()
        self.on_select(list(self.chosen))

    def _double(self, event) -> None:
        index = self._index_at(event)
        if index is not None:
            self.on_open(index)
