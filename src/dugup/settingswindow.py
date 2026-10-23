"""The settings window: every setting, found by its group or by searching, with what it costs the machine.

It opens on a short list, the settings people change in everyday use (catalogue.SHORT_LIST), plus any
setting that differs from its default, so nothing changed is out of sight. "Show all settings" brings in
the rest; a search always looks through every setting.

Drawn from the catalogue: no setting is named in this file. Groups down the left (and "All"); on the right
the group's settings, the everyday ones first, then the ones that command the most of the machine, then the
rest; under each its line of help and, where it has one, what it costs in a coloured tag. A search box at
the top filters by label, help and name across every group. A setting that differs from its default is
marked and has a Reset beside it. "Suggest for this machine" looks at the computer (on a thread) and fills in
the catalogue's suggestions, each with its reason, without saving; Save writes everything that changed.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Dict, List, Optional, Tuple

from . import catalogue
from .catalogue import Entry, Machine
from .shapes import Failure

POLL_MS = 100
ALL = "all"
ORDER = {"heavy": 0, "medium": 1, "light": 2, "": 3}
COLOURS = {"heavy": "#b03a2e", "medium": "#b7791f", "light": "#2f855a"}


def sorted_entries(entries: List[Entry]) -> List[Entry]:
    """Everyday first, then by what they cost the machine, then as catalogued."""
    return sorted(entries, key=lambda e: (not e.everyday, ORDER[catalogue.cost(e.name)[0]]))


def matches(entry: Entry, words: str) -> bool:
    """Whether a search finds this setting: every word typed appears in its label, help, name or group."""
    hay = " ".join((entry.label, entry.help, entry.why, entry.name, catalogue.group(entry.group).title,
                    catalogue.cost(entry.name)[1])).lower()
    return all(w in hay for w in words.lower().split())


class SettingsWindow:
    """`values`: the settings as they stand (name -> value). `look(stop)` returns a Machine, slowly, on a
    thread. `save(changes)` returns "" or what went wrong."""

    def __init__(self, parent, values: Dict[str, object], save: Callable[[Dict[str, object]], str],
                 look: Optional[Callable[[threading.Event], Machine]] = None,
                 on_close: Optional[Callable[[], None]] = None) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.values = dict(values)
        self.edits: Dict[str, object] = {}          # name -> the parsed value as it stands in the form
        self.problems: Dict[str, str] = {}
        self.save_hook, self.look, self.on_close = save, look, on_close
        self.closed = False
        self.looking: Optional[threading.Thread] = None
        self.stop = threading.Event()
        self.events: "queue.Queue[object]" = queue.Queue()
        self.widgets: Dict[str, object] = {}
        self.reasons: Dict[str, str] = {}           # name -> why a suggestion was made
        self.current = ALL
        self.advanced = tk.BooleanVar(value=False)

        win = self.window = tk.Toplevel(parent)
        win.title("DugUp settings")
        win.geometry("1040x720")
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.columnconfigure(1, weight=1)
        win.rowconfigure(1, weight=1)

        top = ttk.Frame(win)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 4))
        ttk.Label(top, text="Find a setting:").pack(side="left")
        self.query = tk.StringVar()
        box = ttk.Entry(top, textvariable=self.query, width=36)
        box.pack(side="left", padx=6)
        box.bind("<KeyRelease>", lambda event: self.redraw())
        ttk.Checkbutton(top, text="Show all settings (advanced)", variable=self.advanced,
                        command=self._advanced_chosen).pack(side="left", padx=(6, 0))
        self.suggest_button = ttk.Button(top, text="Suggest for this machine", command=self.suggest, state="normal" if look else "disabled")
        self.suggest_button.pack(side="left", padx=(12, 2))
        ttk.Label(top, text="(fills in what fits this computer; nothing is saved until you press Save)", foreground="#666666").pack(side="left")

        self.groups = tk.Listbox(win, exportselection=False, width=26, activestyle="none")
        self.groups.grid(row=1, column=0, sticky="ns", padx=(10, 4))
        self.group_keys: List[str] = [ALL] + [g.key for g in catalogue.GROUPS]
        self.groups.insert("end", "All settings")
        for g in catalogue.GROUPS:
            self.groups.insert("end", g.title)
        self.groups.selection_set(0)
        self.groups.bind("<<ListboxSelect>>", lambda event: self._group_chosen())
        self.groups.grid_remove()                    # the groups are for "Show all settings"

        right = ttk.Frame(win)
        right.grid(row=1, column=1, sticky="nsew", padx=(4, 10))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(right, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(right, orient="vertical", command=self.canvas.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=bar.set)
        self.body = ttk.Frame(self.canvas)
        self.body_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.body_id, width=e.width))
        from .picturegrid import bind_wheel
        bind_wheel(self.canvas, self.canvas)

        foot = ttk.Frame(win)
        foot.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 10))
        self.message = tk.StringVar()
        ttk.Entry(foot, textvariable=self.message, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(foot, text="Close", command=self.close).pack(side="right")
        self.save_button = ttk.Button(foot, text="Save", command=self.save)
        self.save_button.pack(side="right", padx=6)
        self.redraw()
        self._poll_id = win.after(POLL_MS, self._poll)

    # -- drawing

    def _group_chosen(self) -> None:
        chosen = self.groups.curselection()
        if chosen:
            self.current = self.group_keys[chosen[0]]
            self.redraw()

    def _advanced_chosen(self) -> None:
        if self.advanced.get():
            self.groups.grid()
        else:
            self.groups.grid_remove()
            self.current = ALL
            self.groups.selection_clear(0, "end")
            self.groups.selection_set(0)
        self.redraw()

    def short(self) -> bool:
        """Showing the short list: not every setting, and no search."""
        return not self.advanced.get() and not self.query.get().strip()

    def shown(self) -> List[Entry]:
        words = self.query.get().strip()
        if self.short():
            return [e for e in catalogue.ENTRIES.values()
                    if e.name in catalogue.SHORT_LIST or self._value(e.name) != catalogue.default(e.name)]
        entries = list(catalogue.ENTRIES.values()) if (self.current == ALL or words) else catalogue.in_group(self.current)
        if words:
            entries = [e for e in entries if matches(e, words)]
        return entries

    def redraw(self) -> None:
        tk, ttk = self.tk, self.ttk
        for child in self.body.winfo_children():
            child.destroy()
        self.widgets.clear()
        entries = self.shown()
        words = self.query.get().strip()
        row = 0
        if not entries:
            ttk.Label(self.body, text="No setting matches those words.", foreground="#666666").grid(row=0, column=0, sticky="w", pady=8)
            return
        by_group: Dict[str, List[Entry]] = {}
        for entry in entries:
            by_group.setdefault(entry.group, []).append(entry)
        short = self.short()
        if short:
            ttk.Label(self.body, text="The settings most people change", font=("TkDefaultFont", 12, "bold")).grid(
                row=row, column=0, sticky="w", pady=(4, 0))
            row += 1
            ttk.Label(self.body, text="Everything else (speed, safety, how files are read) is under Show all settings, "
                                      "at the top. The search box looks through every setting.", wraplength=700,
                      justify="left", foreground="#444444").grid(row=row, column=0, sticky="w")
            row += 1
        for g in catalogue.GROUPS:
            if g.key not in by_group:
                continue
            if short:
                ttk.Label(self.body, text=g.title, font=("TkDefaultFont", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(10, 0))
                row += 1
                for entry in by_group[g.key]:
                    row = self._row(entry, row)
                continue
            if self.current == ALL or words:
                ttk.Label(self.body, text=g.title, font=("TkDefaultFont", 12, "bold")).grid(row=row, column=0, sticky="w", pady=(12, 0))
                row += 1
            if not words:
                ttk.Label(self.body, text=g.about, wraplength=700, justify="left", foreground="#444444").grid(row=row, column=0, sticky="w")
                row += 1
            for entry in sorted_entries(by_group[g.key]):
                row = self._row(entry, row)
        self.canvas.yview_moveto(0)

    def _value(self, name: str) -> object:
        return self.edits.get(name, self.values[name])

    def _row(self, entry: Entry, row: int) -> int:
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.body)
        frame.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        frame.columnconfigure(2, weight=1)
        value = self._value(entry.name)
        kind = catalogue.kind(entry.name)
        name = entry.name
        if kind == "bool":
            variable = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(frame, text=entry.label, variable=variable,
                            command=lambda n=name, v=variable: self.changed(n, "yes" if v.get() else "no")).grid(row=0, column=0, sticky="w")
        elif entry.choices:
            variable = tk.StringVar(value=catalogue.show(name, value))
            ttk.Label(frame, text=entry.label + ":").grid(row=0, column=0, sticky="w", padx=(0, 8))
            box = ttk.Combobox(frame, textvariable=variable, values=[t for _, t in entry.choices], state="readonly", width=40)
            box.grid(row=0, column=1, sticky="w")
            box.bind("<<ComboboxSelected>>", lambda e, n=name, v=variable: self.changed(n, self._choice(n, v.get())))
        else:
            variable = tk.StringVar(value=str(value) if not isinstance(value, float) else f"{value:g}")
            ttk.Label(frame, text=entry.label + (f" ({entry.unit})" if entry.unit else "") + ":").grid(row=0, column=0, sticky="w", padx=(0, 8))
            box = ttk.Entry(frame, textvariable=variable, width=22)
            box.grid(row=0, column=1, sticky="w")
            for event in ("<FocusOut>", "<Return>"):
                box.bind(event, lambda e, n=name, v=variable: self.changed(n, v.get()))
        self.widgets[name] = variable
        level, what = catalogue.cost(name)
        marks = ttk.Frame(frame)
        marks.grid(row=0, column=3, sticky="e")
        if level:
            tk.Label(marks, text=f" {level} ", fg="white", bg=COLOURS[level], font=("TkDefaultFont", 8, "bold")).pack(side="left", padx=4)
        if self._value(name) != catalogue.default(name):
            ttk.Label(marks, text="changed", foreground="#2b6cb0").pack(side="left", padx=4)
            ttk.Button(marks, text="Reset", width=6, command=lambda n=name: self.reset(n)).pack(side="left")
        text = entry.help + ((" " + entry.why) if entry.why else "")
        if what:
            text += f"  {catalogue.COST_WORDS[level].capitalize()}: {what}."
        if entry.zero:
            text += f"  0 means {entry.zero}."
        ttk.Label(self.body, text=text, wraplength=700, justify="left", foreground="#444444").grid(row=row + 1, column=0, sticky="w", padx=(18, 0))
        row += 2
        note = self.problems.get(name) or self.reasons.get(name)
        if note:
            ttk.Label(self.body, text=note, wraplength=700, justify="left",
                      foreground="#b03a2e" if name in self.problems else "#2f855a").grid(row=row, column=0, sticky="w", padx=(18, 0))
            row += 1
        return row

    def _choice(self, name: str, words: str) -> object:
        return next((value for value, text in catalogue.ENTRIES[name].choices if text == words), words)

    # -- changes

    def changed(self, name: str, text: object) -> None:
        parsed = catalogue.parse(name, text)
        if isinstance(parsed, Failure):
            self.problems[name] = parsed.detail
            self.message.set(f"{catalogue.ENTRIES[name].label}: {parsed.detail}")
        else:
            self.problems.pop(name, None)
            if parsed == self.values[name]:
                self.edits.pop(name, None)
            else:
                self.edits[name] = parsed
            self.message.set(f"{len(self.edits)} change(s) not yet saved." if self.edits else "")
        self.redraw()

    def reset(self, name: str) -> None:
        self.changed(name, catalogue.default(name))

    def save(self) -> None:
        if self.problems:
            self.message.set("Fix the settings marked in red first.")
            return
        if not self.edits:
            self.message.set("Nothing to save.")
            return
        problem = self.save_hook(dict(self.edits))
        if problem:
            self.message.set(problem)
            return
        self.values.update(self.edits)
        count = len(self.edits)
        self.edits.clear()
        self.reasons.clear()
        self.message.set(f"Saved {count} setting(s).")
        self.redraw()

    # -- suggestions

    def suggest(self) -> None:
        if self.look is None or (self.looking is not None and self.looking.is_alive()):
            return
        self.suggest_button.configure(state="disabled")
        self.message.set("Looking at this computer (cores, memory, disk, the graphics card) ...")
        look, stop, events = self.look, self.stop, self.events

        def work():
            try:
                events.put(("machine", look(stop)))
            except Exception as exc:
                events.put(("problem", f"{type(exc).__name__}: {exc}"))

        self.looking = threading.Thread(target=work, name="dugup-settings-look", daemon=True)
        self.looking.start()

    def _suggested(self, machine: Machine) -> None:
        found = catalogue.suggestions(machine)
        taken = 0
        for name, (value, reason) in found.items():
            self.reasons[name] = f"Suggested: {reason}."
            if value != self._value(name):
                self.changed(name, value)
                taken += 1
        self.message.set(f"{taken} setting(s) filled in for this computer ({machine.cores} cores); the reasons are "
                         "under each. Press Save to keep them." if taken else "This computer's settings already fit it.")
        self.redraw()

    def _poll(self) -> None:
        if self.closed:
            return
        while True:
            try:
                what, report = self.events.get_nowait()
            except queue.Empty:
                break
            self.suggest_button.configure(state="normal")
            if what == "machine":
                self._suggested(report)
            else:
                self.message.set(f"The computer could not be looked at: {report}")
        self._poll_id = self.window.after(POLL_MS, self._poll)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        try:
            self.window.after_cancel(self._poll_id)
        except Exception:
            pass
        self.window.destroy()
        if self.on_close is not None:
            self.on_close()
