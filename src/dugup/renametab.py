"""The window's Rename tab: pick a folder, preview what its pictures would be called (a grid of thumbnails,
each with the name it has and the name it would get in a box to edit, and a tick to leave it alone), then
Rename, with a record file written as it goes, and Undo last rename. The captions are written on a background
thread; the window's own event queue brings the preview back, and Stop ends it.

The tab talks to the service only through plan_names, apply_names and undo_names, and to the window through
`host` (say, tell, events, thumbnails, service, choose_folder, stop_button plumbing). It keeps no state of
its own past the current preview and the last record it wrote.
"""

from __future__ import annotations

import os
import threading
from typing import Callable, List, Optional

import tkinter as tk
from tkinter import ttk

from .picturegrid import GAP, THUMB, bind_wheel
from .shapes import Failure, FailureReason

ROW_H = THUMB + 62


class RenameTab(ttk.Frame):
    def __init__(self, parent, host) -> None:
        super().__init__(parent, padding=4)
        self.host = host
        self.folder = tk.StringVar()
        self.source = tk.StringVar(value="results")      # "results": the pictures in the current results; "folder"
        self.results_text = tk.StringVar(value="the pictures in the current results (none yet)")
        self.pattern = tk.StringVar(value=host.service.settings.rename_pattern)
        self.with_caption = tk.BooleanVar(value=True)
        self.every = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Choose a folder and press Preview. Nothing is renamed until you say so.")
        self.plan = None
        self.entries: List[tk.StringVar] = []
        self.keep: List[tk.BooleanVar] = []
        self.photos: dict = {}
        self.marks: list = []
        self.worker: Optional[threading.Thread] = None
        self.stop = threading.Event()
        self.last_record = ""
        self.pending_follow = False
        self.ticked_paths: Optional[set] = None          # the paths to start ticked (the selection); None: every one
        self.auto_paths: Optional[list] = None            # what the preview in flight was asked for, when it was automatic
        self.shown_for: Optional[tuple] = None            # the selection the automatic preview was made for
        self.columnconfigure(1, weight=1)
        self.rowconfigure(4, weight=1)
        ttk.Label(self, text="Pictures:").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(self, textvariable=self.results_text, variable=self.source, value="results").grid(row=0, column=1, sticky="w", padx=4)
        folder_row = ttk.Frame(self)
        folder_row.grid(row=1, column=1, columnspan=2, sticky="ew")
        folder_row.columnconfigure(1, weight=1)
        ttk.Radiobutton(folder_row, text="a folder:", variable=self.source, value="folder").grid(row=0, column=0, sticky="w", padx=4)
        host._readonly(folder_row, self.folder, row=0, column=1, sticky="ew", padx=4)
        ttk.Button(folder_row, text="Choose...", command=self.choose).grid(row=0, column=2, padx=2)
        ttk.Label(self, text="Pattern:").grid(row=2, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.pattern).grid(row=2, column=1, sticky="ew", padx=4, pady=2)
        ticks = ttk.Frame(self)
        ticks.grid(row=2, column=2, sticky="w")
        ttk.Checkbutton(ticks, text="Describe each picture", variable=self.with_caption).pack(side="left")
        ttk.Checkbutton(ticks, text="Also pictures with real names", variable=self.every).pack(side="left", padx=(8, 0))
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 4))
        self.preview_button = ttk.Button(bar, text="Preview", command=self.preview)
        self.preview_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="Stop", command=self.stop.set, state="disabled")
        self.stop_button.pack(side="left", padx=4)
        self.tick_all_button = ttk.Button(bar, text="Tick all", command=lambda: self.tick_every(True), state="disabled")
        self.tick_all_button.pack(side="left", padx=(16, 2))
        self.untick_all_button = ttk.Button(bar, text="Untick all", command=lambda: self.tick_every(False), state="disabled")
        self.untick_all_button.pack(side="left", padx=2)
        self.rename_button = ttk.Button(bar, text="Rename as shown", command=self.rename, state="disabled")
        self.rename_button.pack(side="left", padx=(16, 4))
        self.undo_button = ttk.Button(bar, text="Undo last rename", command=self.undo, state="disabled")
        self.undo_button.pack(side="left")
        ttk.Label(bar, textvariable=self.status, wraplength=520).pack(side="left", padx=12)
        self.canvas = tk.Canvas(self, highlightthickness=0, background="#f4f4f4")
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.grid(row=4, column=0, columnspan=3, sticky="nsew")
        self.scroll.grid(row=4, column=3, sticky="ns")
        self.inner = ttk.Frame(self.canvas)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.canvas_window, width=e.width))
        bind_wheel(self, self.canvas)

    # -- preview

    def choose(self) -> None:
        chosen = self.host.choose_folder()
        if chosen:
            self.folder.set(chosen)
            self.source.set("folder")

    def results_changed(self, pictures: int) -> None:
        """The window's results changed: say how many pictures they hold, and start from them when there are any."""
        if pictures:
            self.results_text.set(f"the {pictures:,} picture(s) in the current results")
            if self.plan is None and not self.busy():
                self.source.set("results")
        else:
            self.results_text.set("the pictures in the current results (none yet)")
            if not self.folder.get():
                self.source.set("folder")

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def follow_selection(self) -> None:
        """The tab is open and the selection changed (or the tab was just opened): preview the selected
        picture(s) by themselves, so the tab shows the selected file the way the other tabs do. A preview
        already in hand for the same selection is left as it is."""
        if self.busy():
            self.pending_follow = True          # looked at again when the preview in flight comes back
            return
        self.pending_follow = False
        selected = [p for p in self.host.selected_paths() if p in set(self.host.result_pictures())]
        key = tuple(selected)
        if not selected or key == self.shown_for:
            return
        self.shown_for = key
        self.ticked_paths = set(selected)
        self._start(paths=selected, automatic=True)

    def preview(self) -> None:
        folder = self.folder.get().strip()
        paths = None
        if self.source.get() == "results":
            paths = self.host.result_pictures()
            if not paths:
                self.status.set("There are no pictures in the results. Search first, or choose a folder.")
                return
            selected = set(self.host.selected_paths()) & set(paths)
            self.ticked_paths = selected or None          # the selection starts ticked; with none, every one
        elif not folder:
            self.status.set("Choose a folder first.")
            return
        else:
            self.ticked_paths = None
        self.shown_for = None
        self._start(folder=folder, paths=paths)

    def _start(self, folder: str = "", paths=None, automatic: bool = False) -> None:
        if self.busy():
            self.status.set("A preview is already being made.")
            return
        ready = self.host.service.captions_ready() if self.with_caption.get() else True
        fetching = ready is not True and "not fetched" in str(ready)
        if ready is not True and not fetching:
            self.status.set(f"Captions cannot be written here: {ready}. Untick \"Describe each picture\" to use the other fields.")
            return
        self.stop.clear()
        service, events = self.host.service, self.host.events
        pattern, caption, every = self.pattern.get().strip() or None, self.with_caption.get(), self.every.get()
        stop = self.stop

        def work() -> None:
            try:
                found = service.plan_names(folder, pattern, caption, False, events.put, stop, every=every, paths=paths)
            except Exception as exc:
                found = Failure(FailureReason.CRASHED, f"{type(exc).__name__}: {exc}")
            events.put(("rename-plan", found))

        self.preview_button.configure(state="disabled")
        self.rename_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.auto_paths = list(paths) if automatic else None
        self.status.set(("The captioning model is fetched first, once (about 1.1 GB). " if fetching else "")
                        + ("Writing a sentence about each picture ..." if caption else "Working out the names ..."))
        self.worker = threading.Thread(target=work, name="dugup-window-rename", daemon=True)
        self.worker.start()

    def planned(self, found) -> None:
        """The preview came back (from the window's event loop)."""
        self.preview_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if self.pending_follow and self.auto_paths is not None:
            self.pending_follow = False
            self.shown_for = None
            self.follow_selection()             # the selection moved on while this one was being made
            return
        if isinstance(found, Failure):
            self.status.set(f"The preview could not be made: {found.detail}")
            return
        if found.stopped:
            self.status.set("Stopped before every picture was described; nothing was renamed. Press Preview to start again.")
            return
        self.plan = found
        self._draw()
        n = len(found.to_rename)
        took = f" ({found.captioned:,} described on the {found.device})" if found.captioned else ""
        if self.auto_paths is not None:
            self.status.set(f"The selected picture{'s' if len(self.auto_paths) > 1 else ''}{took}. Press Preview for every picture in the results."
                            if n else "The selected picture would be left alone (its name already says something, or it is protected). "
                                      "Press Preview for every picture in the results.")
        else:
            self.status.set(f"{n:,} picture(s) would be renamed{took}; {len(found.left_alone):,} left alone. Edit any name, untick any "
                            "you want to keep, then press Rename as shown." if n else "Nothing to rename here.")
        self.rename_button.configure(state="normal" if n else "disabled")
        for button in (self.tick_all_button, self.untick_all_button):
            button.configure(state="normal" if n else "disabled")
        self._count()

    def tick_every(self, on: bool) -> None:
        for keep in self.keep:
            keep.set(on)
        self._count()

    def _count(self) -> None:
        """The button says how many will go, and each row says which way it is."""
        chosen = sum(1 for keep in self.keep if keep.get())
        self.rename_button.configure(text=f"Rename {chosen:,} as shown" if chosen else "Rename as shown",
                                     state="normal" if chosen else "disabled")
        for index, (keep, marks) in enumerate(zip(self.keep, self.marks)):
            frame, label = marks
            if keep.get():
                label.configure(text="WILL BE RENAMED", foreground="#1b5e20")
                frame.configure(style="Rename.TFrame")
            else:
                label.configure(text="kept as it is", foreground="#777777")
                frame.configure(style="Keep.TFrame")

    def _draw(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()
        self.entries, self.keep, self.photos, self.marks = [], [], {}, []
        if self.plan is None:
            return
        style = ttk.Style(self)
        style.configure("Rename.TFrame", background="#e8f5e9")
        style.configure("Keep.TFrame", background="#ececec")
        for index, change in enumerate(self.plan.to_rename):
            row = ttk.Frame(self.inner, padding=(4, 4), style="Rename.TFrame")
            row.grid(row=index, column=0, sticky="ew", pady=1)
            row.columnconfigure(2, weight=1)
            keep = tk.BooleanVar(value=self.ticked_paths is None or change.old in self.ticked_paths)
            self.keep.append(keep)
            tick = ttk.Checkbutton(row, variable=keep, command=self._count, text="Rename")
            tick.grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, 4))
            picture = tk.Label(row, width=THUMB // 7, text="...", background="#e4e4e4", anchor="center")
            picture.grid(row=0, column=1, rowspan=3, padx=(2, 8))
            self._picture(picture, change.old, index)
            head = ttk.Frame(row, style="Rename.TFrame")
            head.grid(row=0, column=2, sticky="ew")
            ttk.Label(head, text=os.path.basename(change.old), foreground="#555555").pack(side="left")
            mark = ttk.Label(head, text="WILL BE RENAMED", foreground="#1b5e20", font=("TkDefaultFont", 9, "bold"))
            mark.pack(side="right")
            self.marks.append((row, mark))
            name = tk.StringVar(value=os.path.basename(change.new))
            self.entries.append(name)
            ttk.Entry(row, textvariable=name, width=70).grid(row=1, column=2, sticky="ew", pady=2)
            ttk.Label(row, text=change.caption, wraplength=520, foreground="#333333").grid(row=2, column=2, sticky="w")
        if self.plan.left_alone:
            why = "\n".join(f"{os.path.basename(c.old)}: {c.why}" for c in self.plan.left_alone[:30])
            more = f"\n... and {len(self.plan.left_alone) - 30:,} more" if len(self.plan.left_alone) > 30 else ""
            ttk.Label(self.inner, text=f"Left alone ({len(self.plan.left_alone):,}):\n{why}{more}", foreground="#555555",
                      justify="left").grid(row=len(self.plan.to_rename), column=0, sticky="w", padx=8, pady=8)
        self.canvas.yview_moveto(0)

    def _picture(self, label, path: str, index: int) -> None:
        image = self.host.thumbnails.ask(path)
        if image is None or isinstance(image, str):     # "gone": the file was not there
            return
        try:
            from PIL import ImageTk
            photo = ImageTk.PhotoImage(image)
        except Exception:
            label.configure(text="no picture")
            return
        self.photos[index] = photo
        label.configure(image=photo, text="", width=image.width)

    def thumb_arrived(self, path: str) -> None:
        if self.plan is None:
            return
        for index, change in enumerate(self.plan.to_rename):
            if change.old == path:
                for row in self.inner.winfo_children():
                    if int(row.grid_info().get("row", -1)) == index:
                        for widget in row.winfo_children():
                            if isinstance(widget, tk.Label) and widget.grid_info().get("column") == 1:
                                self._picture(widget, path, index)
                break

    # -- renaming

    def rename(self) -> None:
        if self.plan is None or self.busy():
            return
        chosen = []
        for change, keep, name in zip(self.plan.to_rename, self.keep, self.entries):
            if not keep.get():
                continue
            typed = name.get().strip()
            if not typed:
                continue
            change.new = os.path.join(os.path.dirname(change.old), typed)
            chosen.append(change)
        if not chosen:
            self.status.set("Nothing is ticked.")
            return
        answer = self.host.ask("Rename these pictures?",
                               f"{len(chosen):,} picture(s) will be renamed as shown.\n\nA record of every rename is kept so that "
                               "Undo can put the names back. Nothing is deleted.")
        if not answer:
            return
        from . import records
        try:
            record, handle = records.open_new(self.host.service.layout.root / "renames", "renames")
        except OSError as exc:
            self.status.set(f"The record could not be written ({type(exc).__name__}), so nothing was renamed.")
            return
        journal = records.Journal(handle)
        try:
            journal.outcome(f"pattern\t{self.plan.pattern}")
            from .naming import Plan
            done = self.host.service.apply_names(Plan(self.plan.pattern, chosen), journal=journal)
        except records.RecordError as exc:
            journal.close()
            self.status.set(f"The record could not be written ({exc}), so nothing was renamed.")
            return
        finally:
            journal.close()
        self.last_record = str(record)
        self.undo_button.configure(state="normal")
        self.rename_button.configure(state="disabled")
        text = f"Renamed {len(done.renamed):,} picture(s); left {len(done.skipped):,} alone. The record is in {record}."
        if done.record_problem:
            text = (f"Renamed {len(done.renamed):,} picture(s) and then stopped: the record could not be written "
                    f"({done.record_problem}). What was renamed:\n\n"
                    + ("\n".join(f"{os.path.basename(o)} -> {os.path.basename(n)}" for o, n in done.renamed) or "(nothing)"))
        if done.index_problems:
            text += f"\n\n{len(done.index_problems)} renamed file(s) could not be updated in the index; the next index update sees the new names."
        if done.skipped:
            text += "\n\n" + "\n".join(f"{os.path.basename(p)}: {why}" for p, why in done.skipped[:10])
        self.status.set(text.split("\n")[0])
        self.host.tell("Renamed", text)
        self.plan = None
        self.shown_for = None
        self._draw()

    def undo(self) -> None:
        if not self.last_record or self.busy():
            return
        try:
            with open(self.last_record, "r", encoding="utf-8-sig") as handle:
                text = handle.read()
        except OSError as exc:
            self.status.set(f"The record could not be read ({type(exc).__name__}).")
            return
        done = self.host.service.undo_names(text)
        self.undo_button.configure(state="disabled")
        message = f"Put back {len(done.renamed):,} name(s); left {len(done.skipped):,}."
        if done.skipped:
            message += "\n\n" + "\n".join(f"{os.path.basename(p)}: {why}" for p, why in done.skipped[:10])
        self.status.set(message.split("\n")[0])
        self.host.tell("Undone", message)

