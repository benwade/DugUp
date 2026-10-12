"""The search window (tkinter): a client of the Service, like the command line.

Layout, after the search tools people know: what to look for and how to narrow it at the top, one row per
file on the left, and on the right what was found in the selected file, its details, and the state of
the index with buttons to bring it up to date.

Two halves. The first is plain logic with no window in it (what a size or a date typed by a person means,
the sort order that clicking column headers builds, the filters a form stands for), so it can be tested
anywhere. The second is the window itself.

Threads: the window's thread owns the reading connection, so searching happens there (it takes
milliseconds). Indexing and OCR run in one background thread with their own connection; what they report
comes back through a queue that the window empties ten times a second. Nothing but the window's thread
touches a widget.

Text a person may want to copy (paths, notes, counts) is shown in read-only entry fields, not labels.
"""

from __future__ import annotations

import datetime
import io
import os
import queue
import sys
import threading
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .cli import (format_bytes, format_date, format_seconds, megabytes, print_compacted, print_ocr_report, print_report,
                  progress_text)
from .desktop import Desktop
from .indexer import KEEP, REFUSE, REREAD, Progress
from .shapes import (MAX_SORT_KEYS, WHERE_ANYWHERE, WHERE_NAME, WHERE_OCR, WHERE_TEXT, Failure, FailureReason, FileKind,
                     Root, SearchFilters, SearchRow)

PAGE_ROWS = 200          # rows fetched at a time; "More" fetches the next lot
PASSAGE_CONTEXT = 200    # characters shown on each side of a match
PLACES_STEP = 25         # places added by each press of "More" under the passages
POLL_MS = 100

# ---- logic with no window in it -----------------------------------------------------------------------------

SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3}


def parse_size(text: str) -> Optional[int]:
    """"200k", "5 MB", "1.5g" or a plain number of bytes. Empty means no limit. Raises ValueError with a sentence."""
    cleaned = text.strip().lower().replace(",", "").replace(" ", "")
    if not cleaned:
        return None
    digits = cleaned.rstrip("abcdefghijklmnopqrstuvwxyz")
    unit = cleaned[len(digits):]
    try:
        number = float(digits)
    except ValueError:
        number = -1.0
    if unit not in SIZE_UNITS or number < 0 or number != number or number == float("inf"):
        raise ValueError(f"A size is a number with an optional k, m or g (for example 200k or 5m), not {text.strip()!r}.")
    return int(number * SIZE_UNITS[unit])


def parse_day(text: str, end_of_day: bool = False) -> Optional[float]:
    """A day as YYYY-MM-DD, in local time. Empty means no limit. With `end_of_day` the moment is the start
    of the NEXT day, so "before 2026-10-12" takes in the whole of the 12th."""
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        day = datetime.datetime.strptime(cleaned, "%Y-%m-%d")
        if end_of_day:
            day += datetime.timedelta(days=1)
        return day.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError(f"A date is written year-month-day (for example 2026-10-12), not {cleaned!r}.") from None


WHERE_LABELS = (("Anywhere", WHERE_ANYWHERE), ("In the text", WHERE_TEXT), ("In the file name", WHERE_NAME),
                ("In text read by OCR", WHERE_OCR))
KIND_LABELS = (("PDF", FileKind.PDF.value), ("Office", FileKind.OFFICE.value), ("Text", FileKind.TEXT.value),
               ("Pictures", FileKind.IMAGE.value))
ALL_FOLDERS = "All indexed folders"


def build_filters(folder: str = "", where: str = WHERE_ANYWHERE, kinds: Sequence[str] = (), after: str = "",
                  before: str = "", smallest: str = "", largest: str = "", exact: bool = False) -> SearchFilters:
    """The filters a filled-in form stands for. Raises ValueError with a sentence for the person who typed."""
    low, high = parse_size(smallest), parse_size(largest)
    start, end = parse_day(after), parse_day(before, end_of_day=True)
    if low is not None and high is not None and low > high:
        raise ValueError("The smallest size is larger than the largest size.")
    if start is not None and end is not None and start >= end:
        raise ValueError("The 'after' date is later than the 'before' date.")
    every = {value for _, value in KIND_LABELS}
    chosen = tuple(k for k in kinds if k in every)
    return SearchFilters(folder=folder or None, kinds=() if set(chosen) == every else chosen, where=where,
                         modified_after=start, modified_before=end, min_size=low, max_size=high, exact=bool(exact))


COLUMNS = (("name", "Name", 190), ("folder", "Folder", 195), ("kind", "Type", 60), ("size", "Size", 85),
           ("modified", "Modified", 140), ("matches", "Matches", 75))
LARGEST_FIRST = ("size", "modified", "matches")    # a first click on these sorts downwards
UP, DOWN = chr(0x2191), chr(0x2193)


class SortOrder:
    """Up to three sort keys, built the way people expect from clicking column headers: the column clicked
    becomes the first key and the keys already there move down one place. Clicking the first key again
    turns it round. With no key chosen, results come best match first."""

    def __init__(self) -> None:
        self.keys: List[Tuple[str, bool]] = []

    def click(self, key: str) -> None:
        if self.keys and self.keys[0][0] == key:
            self.keys[0] = (key, not self.keys[0][1])
            return
        rest = [(k, d) for k, d in self.keys if k != key]
        self.keys = ([(key, key in LARGEST_FIRST)] + rest)[:MAX_SORT_KEYS]

    def reset(self) -> None:
        self.keys = []

    def as_request(self):
        return list(self.keys) if self.keys else "rank"

    def describe(self) -> str:
        if not self.keys:
            return "best match first"
        titles = {key: title.lower() for key, title, _ in COLUMNS}
        return ", then ".join(f"{titles.get(k, k)} {DOWN if d else UP}" for k, d in self.keys)

    def mark(self, key: str) -> str:
        """What a column header shows beside its title: its place in the order and its direction."""
        for place, (k, down) in enumerate(self.keys, 1):
            if k == key:
                return f" {DOWN if down else UP}{place if len(self.keys) > 1 else ''}"
        return ""


def row_values(row: SearchRow) -> Tuple[str, ...]:
    flag = "  [missing]" if row.missing_since is not None else "  [folder not available]" if row.unavailable else ""
    return (row.name + flag, row.folder, row.kind, format_bytes(row.size), format_date(row.modified), f"{row.matches:,}")


def where_label(kind: str, page: int, file_kind: str) -> str:
    if kind == "name":
        return "In the file name"
    if kind == "ocr":
        return "In the picture (read by OCR)" if file_kind == FileKind.IMAGE.value else f"Page {page} (read by OCR)"
    return f"Page {page}" if page else "In the text"


# ---- the window ---------------------------------------------------------------------------------------------

class App:
    """`root` is a tkinter root (or Toplevel). `service` is a LocalService. The rest can be replaced in tests."""

    def __init__(self, root, service, desktop: Optional[Desktop] = None, ask=None, choose_folder=None) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk, self.ttk = tk, ttk
        self.root = root
        self.service = service
        self.desktop = desktop or Desktop()
        self.ask = ask or (lambda title, text: messagebox.askyesnocancel(title, text, parent=root))
        self.choose_folder = choose_folder or (lambda: filedialog.askdirectory(parent=root, mustexist=True))
        self.copy_text: Callable[[str], None] = self._to_clipboard
        self.order = SortOrder()
        self.rows: List[SearchRow] = []
        self.total = 0
        self.last: Optional[Tuple[str, SearchFilters]] = None    # the search the rows belong to
        self.events: "queue.Queue[object]" = queue.Queue()
        self.stop = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.closed = False
        self._build()
        self.refresh_folders()
        self.refresh_status()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = root.after(POLL_MS, self._poll)

    # -- building

    def _readonly(self, parent, variable, **grid):
        entry = self.ttk.Entry(parent, textvariable=variable, state="readonly")
        entry.grid(**grid)
        return entry

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        root = self.root
        root.title("DugUp")
        root.geometry("1360x760")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        form = ttk.Frame(root, padding=6)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self.words = tk.StringVar()
        self.folder = tk.StringVar(value=ALL_FOLDERS)
        self.where = tk.StringVar(value=WHERE_LABELS[0][0])
        self.after_day, self.before_day = tk.StringVar(), tk.StringVar()
        self.smallest, self.largest = tk.StringVar(), tk.StringVar()
        self.kinds: Dict[str, object] = {value: tk.BooleanVar(value=True) for _, value in KIND_LABELS}
        self.exact = tk.BooleanVar(value=False)

        ttk.Label(form, text="Words:").grid(row=0, column=0, sticky="w")
        self.words_entry = ttk.Entry(form, textvariable=self.words)
        self.words_entry.grid(row=0, column=1, columnspan=9, sticky="ew", padx=4)
        self.words_entry.bind("<Return>", lambda event: self.search())
        self.search_button = ttk.Button(form, text="Search", command=self.search)
        self.search_button.grid(row=0, column=10, padx=2)

        ttk.Label(form, text="Look in:").grid(row=1, column=0, sticky="w")
        self.folder_box = ttk.Combobox(form, textvariable=self.folder, state="readonly")
        self.folder_box.grid(row=1, column=1, sticky="ew", padx=4, pady=2)
        ttk.Label(form, text="Words are:").grid(row=1, column=2, sticky="e")
        ttk.Combobox(form, textvariable=self.where, state="readonly", width=20,
                     values=[label for label, _ in WHERE_LABELS]).grid(row=1, column=3, padx=4)
        for at, (label, value) in enumerate(KIND_LABELS):
            ttk.Checkbutton(form, text=label, variable=self.kinds[value]).grid(row=1, column=4 + at, padx=2)
        ttk.Checkbutton(form, text="Exact form", variable=self.exact).grid(row=1, column=8, padx=(14, 2))
        ttk.Label(form, text="(off: education also finds educate)").grid(row=1, column=9, columnspan=2, sticky="w")

        narrow = ttk.Frame(form)
        narrow.grid(row=2, column=0, columnspan=11, sticky="w", pady=2)
        for text, variable, width in (("Modified on or after:", self.after_day, 11), ("up to and including:", self.before_day, 11),
                                      ("Size at least:", self.smallest, 8), ("at most:", self.largest, 8)):
            ttk.Label(narrow, text=text).pack(side="left", padx=(0, 2))
            entry = ttk.Entry(narrow, textvariable=variable, width=width)
            entry.pack(side="left", padx=(0, 10))
            entry.bind("<Return>", lambda event: self.search())
        ttk.Label(narrow, text="(dates as 2026-10-12; sizes as 200k, 5m, 1g)").pack(side="left")
        ttk.Button(narrow, text="Clear filters", command=self.clear_filters).pack(side="left", padx=10)

        panes = ttk.PanedWindow(root, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew", padx=6)

        left = ttk.Frame(panes)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        bar = ttk.Frame(left)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Sorted by:").grid(row=0, column=0, sticky="w")
        self.order_text = tk.StringVar(value=self.order.describe())
        self._readonly(bar, self.order_text, row=0, column=1, sticky="ew", padx=4)
        ttk.Button(bar, text="Best match first", command=self.reset_order).grid(row=0, column=2)
        self.tree = ttk.Treeview(left, columns=[key for key, _, _ in COLUMNS], show="headings", selectmode="browse")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title, command=lambda key=key: self.sort_by(key))
            self.tree.column(key, width=width, anchor="e" if key in ("size", "matches") else "w", stretch=key in ("name", "folder"))
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        across = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=across.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        across.grid(row=2, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", lambda event: self.show_selected())
        self.tree.bind("<Double-1>", lambda event: self.open_selected())
        self.tree.bind("<Return>", lambda event: self.open_selected())
        self.tree.bind("<Button-3>", self._menu)
        actions = ttk.Frame(left)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=3)
        for text, command in (("Open", self.open_selected), ("Edit", self.edit_selected),
                              ("Show in folder", self.show_in_folder), ("Copy path", self.copy_path)):
            ttk.Button(actions, text=text, command=command).pack(side="left", padx=2)
        self.more_button = ttk.Button(actions, text="More results", command=self.more, state="disabled")
        self.more_button.pack(side="right", padx=2)
        self.menu = tk.Menu(root, tearoff=False)
        for text, command in (("Open", self.open_selected), ("Edit", self.edit_selected),
                              ("Show in folder", self.show_in_folder), ("Copy path", self.copy_path)):
            self.menu.add_command(label=text, command=command)
        panes.add(left, weight=3)

        right = ttk.Notebook(panes)
        self.tabs = right
        found = ttk.Frame(right, padding=4)
        found.columnconfigure(1, weight=1)
        found.rowconfigure(3, weight=1)
        self.path_text, self.facts_text, self.note_text = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for at, (label, variable) in enumerate((("Path:", self.path_text), ("File:", self.facts_text), ("State:", self.note_text))):
            ttk.Label(found, text=label).grid(row=at, column=0, sticky="w")
            self._readonly(found, variable, row=at, column=1, sticky="ew", padx=4, pady=1)
        self.hits = tk.Text(found, wrap="word", height=10, state="disabled", font="TkDefaultFont")
        self.hits.tag_configure("hit", background="#ffe58a", foreground="#000000")
        self.hits.tag_configure("where", font=("TkDefaultFont", 9, "bold"))
        hits_scroll = ttk.Scrollbar(found, orient="vertical", command=self.hits.yview)
        self.hits.configure(yscrollcommand=hits_scroll.set)
        self.hits.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=4)
        hits_scroll.grid(row=3, column=2, sticky="ns")
        self.places_button = ttk.Button(found, text="More...", command=self.more_places, state="disabled")
        self.places_button.grid(row=4, column=0, columnspan=2, sticky="w")
        self.places: Optional[list] = None      # every place in the selected file, once asked for
        self.places_shown = 0
        right.add(found, text="Found in this file")

        index = ttk.Frame(right, padding=4)
        index.columnconfigure(0, weight=1)
        index.rowconfigure(2, weight=1)
        buttons = ttk.Frame(index)
        buttons.grid(row=0, column=0, sticky="ew")
        self.run_buttons = []
        for text, command in (("Add folder...", self.add_folder), ("Update the index", lambda: self.start("index")),
                              ("Read pictures and scans (OCR)", lambda: self.start("ocr"))):
            button = ttk.Button(buttons, text=text, command=command)
            button.pack(side="left", padx=2)
            self.run_buttons.append(button)
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop_work, state="disabled")
        self.stop_button.pack(side="left", padx=2)
        ttk.Button(buttons, text="Refresh", command=self.refresh_status).pack(side="left", padx=2)
        self.progress_text = tk.StringVar(value="Nothing is running.")
        self._readonly(index, self.progress_text, row=1, column=0, sticky="ew", pady=3)
        self.status = tk.Text(index, wrap="word", state="disabled", font="TkDefaultFont")
        self.status.grid(row=2, column=0, sticky="nsew")
        self.bar = ttk.Progressbar(index, mode="determinate", maximum=100.0)
        self.bar.grid(row=3, column=0, sticky="ew", pady=3)
        right.add(index, text="Index")
        panes.add(right, weight=2)
        self.panes = panes
        self._sash_placed = False
        panes.bind("<Configure>", self._place_sash)

        self.message = tk.StringVar(value="Type words and press Enter.")
        self._readonly(root, self.message, row=2, column=0, sticky="ew", padx=6, pady=4)
        self.words_entry.focus_set()

    def _place_sash(self, event=None) -> None:
        """Once, when the window first has its real size: give the results the wider side. After that the
        divider is the person's to drag."""
        width = self.panes.winfo_width()
        if self._sash_placed or width < 400:
            return
        self._sash_placed = True
        self.panes.sashpos(0, int(width * 0.58))

    # -- small helpers

    def say(self, text: str) -> None:
        self.message.set(text)

    def _set_text(self, widget, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def selected(self) -> Optional[SearchRow]:
        chosen = self.tree.selection()
        if not chosen:
            return None
        index = int(chosen[0])
        return self.rows[index] if 0 <= index < len(self.rows) else None

    # -- searching

    def filters(self) -> SearchFilters:
        folder = self.folder.get()
        where = dict(WHERE_LABELS).get(self.where.get(), WHERE_ANYWHERE)
        kinds = [value for value, variable in self.kinds.items() if variable.get()]    # type: ignore[attr-defined]
        if not kinds:
            raise ValueError("No kind of file is ticked, so nothing can be found. Tick at least one.")
        return build_filters("" if folder == ALL_FOLDERS else folder, where, kinds, self.after_day.get(),
                             self.before_day.get(), self.smallest.get(), self.largest.get(), self.exact.get())

    def clear_filters(self) -> None:
        self.folder.set(ALL_FOLDERS)
        self.where.set(WHERE_LABELS[0][0])
        for variable in (self.after_day, self.before_day, self.smallest, self.largest):
            variable.set("")
        for variable in self.kinds.values():
            variable.set(True)    # type: ignore[attr-defined]
        self.exact.set(False)

    def search(self) -> None:
        words = self.words.get().strip()
        if not words:
            self.say("Type one or more words to look for.")
            return
        try:
            filters = self.filters()
        except ValueError as exc:
            self.say(str(exc))
            return
        self.last = (words, filters)
        self._fetch(0)

    def more(self) -> None:
        if self.last is not None and len(self.rows) < self.total:
            self._fetch(len(self.rows))

    def _fetch(self, offset: int) -> None:
        words, filters = self.last    # type: ignore[misc]
        page = self.service.search(words, PAGE_ROWS, offset, self.order.as_request(), False, filters)
        if isinstance(page, Failure):
            self.say(f"The search could not be done: {page.detail}")
            return
        if offset == 0:
            self.rows = []
            self.tree.delete(*self.tree.get_children())
            self._show_nothing()
        for row in page.rows:
            self.tree.insert("", "end", iid=str(len(self.rows)), values=row_values(row))
            self.rows.append(row)
        self.total = page.total
        self.more_button.configure(state="normal" if len(self.rows) < self.total else "disabled")
        if self.total == 0:
            self.say(f"No files found for: {words}")
        else:
            self.say(f"Showing {len(self.rows):,} of {self.total:,} file(s) for: {words}")
        if offset == 0 and self.rows:
            self.tree.selection_set("0")
            self.tree.focus("0")

    def sort_by(self, key: str) -> None:
        self.order.click(key)
        self._order_changed()

    def reset_order(self) -> None:
        self.order.reset()
        self._order_changed()

    def _order_changed(self) -> None:
        self.order_text.set(self.order.describe())
        for key, title, _ in COLUMNS:
            self.tree.heading(key, text=title + self.order.mark(key))
        if self.last is not None:
            self._fetch(0)     # sorting is done by the index, over every match, not over the rows on screen

    # -- the selected file

    def _show_nothing(self) -> None:
        for variable in (self.path_text, self.facts_text, self.note_text):
            variable.set("")
        self._set_text(self.hits, "")
        self.places, self.places_shown = None, 0
        self.places_button.configure(text="More...", state="disabled")

    def show_selected(self) -> None:
        row = self.selected()
        if row is None:
            self._show_nothing()
            return
        self.path_text.set(row.path)
        self.facts_text.set(f"{row.kind}, {format_bytes(row.size)}, modified {format_date(row.modified)}")
        record = self.service.store.get_file(row.path)
        note = row.state + (f": {record.error}" if record is not None and record.error else "")
        if row.missing_since is not None:
            keep = self.service.settings.missing_retention_days * 86400.0
            note += f". Not at this path at the last scan; kept in the index until {format_date(row.missing_since + keep)[:10]}"
        elif row.unavailable:
            note += ". Its folder is not available right now; the file is kept in the index"
        self.note_text.set(note)
        self.places, self.places_shown = None, 0
        hits = self.hits
        hits.configure(state="normal")
        hits.delete("1.0", "end")
        shown = self._insert_places(row, row.evidence)
        if not shown:
            hits.insert("end", "The passages are no longer in the index (the file was read again). Search again.")
        elif row.matches > shown:
            hits.insert("end", f"These are the {shown} best of {row.matches:,} places in this file.")
        hits.configure(state="disabled")
        more = shown and row.matches > shown
        self.places_button.configure(text=f"More... (all {row.matches:,} places, in page order)" if more else "More...",
                                     state="normal" if more else "disabled")

    def _insert_places(self, row: SearchRow, evidence) -> int:
        """Write passages at the end of the passage box (which must be open for writing). Returns how many."""
        hits, shown = self.hits, 0
        for item in evidence:
            passage = self.service.get_evidence(item, PASSAGE_CONTEXT)
            if isinstance(passage, Failure):
                continue
            hits.insert("end", where_label(passage.kind.value, passage.page, row.kind) + "\n", "where")
            before, match, after = (passage.text[:passage.start], passage.text[passage.start:passage.end],
                                    passage.text[passage.end:])
            hits.insert("end", before)
            hits.insert("end", match, "hit")
            hits.insert("end", after + "\n\n")
            shown += 1
        return shown

    def more_places(self) -> None:
        """The first press replaces the best few places with the first lot of ALL places, in reading order;
        each later press adds the next lot."""
        row = self.selected()
        if row is None or self.last is None:
            return
        hits = self.hits
        hits.configure(state="normal")
        if self.places is None:
            self.places = list(self.service.more_evidence(row.file_id, self.last[0], self.last[1]))
            self.places_shown = 0
            hits.delete("1.0", "end")
        batch = self.places[self.places_shown:self.places_shown + PLACES_STEP]
        self._insert_places(row, batch)
        self.places_shown += len(batch)
        hits.configure(state="disabled")
        total = len(self.places)
        if self.places_shown >= total:
            self.places_button.configure(text=f"All {total:,} places are shown", state="disabled")
        else:
            self.places_button.configure(text=f"More... ({self.places_shown:,} of {total:,} shown)", state="normal")

    def _with_selected(self, action: Callable[[str], str], done: str) -> None:
        row = self.selected()
        if row is None:
            self.say("Select a file first.")
            return
        problem = action(row.path)
        self.say(problem or f"{done}: {row.path}")

    def open_selected(self) -> None:
        self._with_selected(self.desktop.open_file, "Opened")

    def edit_selected(self) -> None:
        self._with_selected(self.desktop.edit_file, "Opened for editing")

    def show_in_folder(self) -> None:
        self._with_selected(self.desktop.show_in_folder, "Shown in its folder")

    def copy_path(self) -> None:
        row = self.selected()
        if row is None:
            self.say("Select a file first.")
            return
        self.copy_text(row.path)
        self.say(f"Copied: {row.path}")

    def _to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _menu(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.menu.tk_popup(event.x_root, event.y_root)

    # -- the index

    def refresh_folders(self) -> None:
        self.folder_box.configure(values=[ALL_FOLDERS] + [r.path for r in self.service.list_roots()])
        if self.folder.get() not in self.folder_box.cget("values"):
            self.folder.set(ALL_FOLDERS)

    def status_text(self) -> str:
        service = self.service
        status = service.indexing_status()
        counts = status["counts"]
        lines = [f"Data folder: {service.layout.root}",
                 f"{status['files']:,} file(s), {status['segments']:,} piece(s) of text"]
        order = ["done", "partial", "pending", "skipped", "failed", "missing"]
        parts = [f"{name} {counts[name]:,}" for name in order if counts.get(name)]
        lines.append("States: " + (", ".join(parts) if parts else "nothing indexed yet"))
        if counts.get("pending"):
            lines.append(f"{counts['pending']:,} file(s) wait to be read: Update the index.")
        tally = service.attention_counts()
        if tally["waiting_for_ocr"]:
            lines.append(f"{tally['waiting_for_ocr']:,} file(s) wait for OCR: Read pictures and scans.")
        if tally["attention"]:
            lines.append(f"{tally['attention']:,} file(s) failed or were read only in part:")
            for record in service.needs_attention(limit=20):
                lines.append(f"    [{record.state.value}] {record.path}")
                lines.append(f"        {record.error}")
            if tally["attention"] > 20:
                lines.append(f"    ... and {tally['attention'] - 20:,} more (dugup status --attention)")
        for engine, count in sorted(service.old_ocr_pages().items()):
            lines.append(f"{count:,} page(s) were read by another OCR version ({engine or 'unknown'}).")
        lines.append("")
        if not status["roots"]:
            lines.append("No folders yet. Add one with the button above.")
        for root in status["roots"]:
            away = "" if root["reachable"] else "  [NOT AVAILABLE at the last scan; its files are kept]"
            lines.append(f"{root['path']}{away}")
            lines.append(f"    {sum(root['counts'].values()):,} file(s); last scan {format_date(root['last_scan'])}")
            if root["exclude"]:
                lines.append(f"    skipping: {', '.join(root['exclude'])}")
        lines.append("")
        used, free, limit = service.layout.index_bytes(), service.layout.free_bytes(), service.settings.max_index_mb
        lines.append(f"The index uses {megabytes(used)}; {megabytes(free)} free on that drive; "
                     + (f"spending limit {limit:,} MB." if limit else "no spending limit (dugup space --limit MB)."))
        return "\n".join(lines)

    def refresh_status(self, extra: str = "") -> None:
        try:
            text = self.status_text()
        except Exception as exc:      # the status is a convenience; it must never take the window down
            text = f"The state of the index could not be read ({type(exc).__name__}: {exc})."
        self._set_text(self.status, (extra.rstrip() + "\n\n" if extra else "") + text)

    def add_folder(self) -> None:
        chosen = self.choose_folder()
        if not chosen:
            return
        result = self.service.set_root(Root(os.path.normpath(chosen)))
        if isinstance(result, Failure):
            self.say(f"That folder could not be added: {result.detail}")
            return
        self.refresh_folders()
        self.refresh_status()
        self.say(f"Added {result.path}. Update the index to read it.")

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def start(self, what: str, on_settings_change: str = REFUSE) -> None:
        """Start indexing ("index") or OCR ("ocr") in the background."""
        if self.busy():
            self.say("Something is already running. Stop it first, or wait.")
            return
        if not self.service.list_roots():
            self.say("There is no folder to index yet. Add one first.")
            return
        self.stop = threading.Event()
        stop, events, service = self.stop, self.events, self.service

        def work() -> None:
            try:
                if what == "ocr":
                    report = service.run_ocr(stop, events.put)
                else:
                    report = service.run_indexing(stop, events.put, None, on_settings_change)
            except Exception as exc:      # never die silently in the background
                report = Failure(FailureReason.CRASHED, f"{type(exc).__name__}: {exc}")
            events.put((what, report))

        self.worker = threading.Thread(target=work, name=f"dugup-window-{what}", daemon=True)
        for button in self.run_buttons:
            button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress_text.set("Starting ...")
        self.bar.configure(value=0.0)
        self.tabs.select(1)
        self.worker.start()

    def stop_work(self) -> None:
        if self.busy():
            self.stop.set()
            self.progress_text.set("Stopping. What is not finished stays queued.")

    def _poll(self) -> None:
        if self.closed:
            return
        self.drain()
        self._poll_id = self.root.after(POLL_MS, self._poll)

    def drain(self) -> None:
        """Take in everything the background run has reported. Only the newest progress line is shown."""
        latest: Optional[Progress] = None
        finished: List[Tuple[str, object]] = []
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, Progress):
                latest = item
            else:
                finished.append(item)    # type: ignore[arg-type]
        if latest is not None and not finished:
            self.progress_text.set(progress_text(latest))
            self.bar.configure(value=latest.percent if latest.phase in ("read", "ocr") else 0.0)
        for what, report in finished:
            self._finished(what, report)

    def _finished(self, what: str, report) -> None:
        if self.worker is not None:
            self.worker.join()
            self.worker = None
        for button in self.run_buttons:
            button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.bar.configure(value=0.0)
        out = io.StringIO()
        if isinstance(report, Failure):
            self.progress_text.set("It could not run.")
            self.refresh_status(f"Could not do that ({report.reason.value.replace('_', ' ')}): {report.detail}")
            return
        if getattr(report, "settings_changed", ()):
            self.progress_text.set("Nothing was done: the settings changed.")
            answer = self.ask("The settings have changed",
                              "The settings that shape the index have changed since it was built:\n"
                              + ", ".join(report.settings_changed) + "\n\n"
                              "Yes: read every file again with the new settings (OCR text is kept).\n"
                              "No: keep what is stored; only new and changed files use the new settings.\n"
                              "Cancel: do nothing now.")
            if answer is not None:
                self.start("index", REREAD if answer else KEEP)
            return
        if what == "ocr":
            print_ocr_report(report, out)
        else:
            print_report(report, out, self.service.settings.missing_retention_days)
        if report.paused:
            out.write(f"It paused itself: {report.paused}.\n")
        if report.stopped:
            out.write("Stopped. Start it again to continue where it stopped.\n")
        elif not report.paused:
            # the background thread leaves tidying to this one, which owns the reading connection
            self.progress_text.set("Finishing ...")
            self.root.update_idletasks()
            print_compacted(self.service.compact_if_worthwhile(), out)
        self.progress_text.set(f"Finished in {format_seconds(report.seconds)}." if not report.stopped else "Stopped.")
        self.refresh_folders()
        self.refresh_status(out.getvalue())

    # -- closing

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        worker = self.worker
        if worker is not None:
            worker.join()        # a stop request ends a run promptly; nothing is lost
        try:
            self.root.after_cancel(self._poll_id)
        except Exception:
            pass
        self.service.close()
        self.root.destroy()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`dugup-window`, and `dugup window`. Opens the window on the default data folder (or --data-dir)."""
    from .cli import main as cli_main
    return cli_main(["window"] + list(argv if argv is not None else sys.argv[1:]))
