"""The folder picker: tick the folders to index and say how far to go in each (the picture of picker.Plan).

One row for each folder. Click the box to take a folder in or out. Click its level to step through
Names only, Words, Words and scans, Everything. A folder follows the one above it until it is given a
level of its own. Nothing is changed until Apply.

All the deciding is in picker.py; this file only draws it and passes clicks on.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from .picker import NOT_YET, OPTION_LABELS, Node, Plan, label
from .shapes import OPTIONS
from .survey import CODE, DOCUMENTS, MEDIA, OTHER, PICTURES, TEXT, Rates

IN, OUT = "☑", "☐"       # a ticked and an empty box
COLUMNS = (("tick", "In", 36),
           *((option, OPTION_LABELS[option] + ("*" if option in NOT_YET else ""), 112 if option == "alike" else 76) for option in OPTIONS),
           ("holds", "What is there", 300), ("cost", "Index grows by", 116), ("why", "Notes", 380))
OPTION_COLUMN = {f"#{index + 2}": option for index, option in enumerate(OPTIONS)}     # "#1" is the tick
PLACEHOLDER = "more:"


def size_text(count: float) -> str:
    mb = count / 1048576
    if mb >= 1000:
        return f"{mb / 1024:,.1f} GB"
    return f"{mb:,.0f} MB" if mb >= 10 else f"{mb:,.1f} MB"


def holds_text(node: Node) -> str:
    row = node.total
    files = row.files - row.not_worth
    parts = []
    for count, word in ((row.groups[DOCUMENTS], "documents"), (row.groups[TEXT], "text"), (row.groups[PICTURES], "pictures"),
                        (row.groups[MEDIA], "sound/video"), (row.groups[CODE], "code"), (row.groups[OTHER], "other")):
        if count:
            parts.append(f"{count:,} {word}")
    text = f"{files:,} file(s), {size_text(row.bytes)}"
    if row.cloud_only:
        text += f"; {row.cloud_only:,} in the cloud only"
    return text + (": " + ", ".join(parts[:4]) if parts else "")


class PickerDialog:
    """`parent` is a tkinter window. `on_apply(plan)` is called with the plan when Apply is pressed;
    it returns "" when the plan was taken, else what went wrong (the dialog then stays open and says so)."""

    def __init__(self, parent, plan: Plan, rates: Rates, on_apply: Callable[[Plan], str], known: bool = False,
                 fresh: Optional[Callable[[], Plan]] = None, on_close: Optional[Callable[[], None]] = None) -> None:
        self.on_close = on_close
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.plan, self.rates, self.on_apply, self.fresh = plan, rates, on_apply, fresh
        self.closed = False
        self.applied = False
        win = self.window = tk.Toplevel(parent)
        win.title("Choose what to index")
        win.geometry("1400x700")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(2, weight=1)

        self.where = tk.StringVar(value=plan.tree.root)
        ttk.Entry(win, textvariable=self.where, state="readonly").grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self.help = tk.StringVar(value="Click a box to change it. \"In\" takes a folder in or out; the others say what is done with "
                                       "its files. Folders inside follow the folder above. Nothing changes until Apply.")
        ttk.Entry(win, textvariable=self.help, state="readonly").grid(row=1, column=0, sticky="ew", padx=6, pady=2)

        frame = ttk.Frame(win)
        frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=2)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = self.tree = ttk.Treeview(frame, columns=[key for key, _, _ in COLUMNS], selectmode="browse")
        tree.heading("#0", text="Folder")
        tree.column("#0", width=260, stretch=False)
        for key, title, width in COLUMNS:
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=key == "why", anchor="center" if key == "tick" or key in OPTIONS else "w")
        tree.tag_configure("out", foreground="#888888")
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        tree.bind("<Button-1>", self._click)
        tree.bind("<<TreeviewOpen>>", self._opened)
        tree.bind("<<TreeviewSelect>>", self._selected)
        tree.bind("<space>", lambda event: self._key(self.toggle))
        tree.bind("<Key-l>", lambda event: self._key(self.step_level))

        actions = ttk.Frame(win)
        actions.grid(row=3, column=0, sticky="w", padx=6, pady=(2, 2))
        self.inside_button = ttk.Button(actions, text="Apply to everything inside", command=self.apply_inside)
        self.inside_button.pack(side="left", padx=(0, 6))
        self.follow_button = ttk.Button(actions, text="Follow the folder above", command=self.follow_above)
        self.follow_button.pack(side="left", padx=(0, 6))
        self.note = tk.StringVar(value="* Meaning and Pictures alike are remembered now and used once searching by meaning is built. "
                                       "Scans and Meaning need Words, so ticking one ticks Words too.")
        ttk.Entry(win, textvariable=self.note, state="readonly").grid(row=4, column=0, sticky="ew", padx=6, pady=2)
        self.skip_tool = tk.BooleanVar(value=plan.skip_tool)
        self.tool_box = ttk.Checkbutton(
            win, variable=self.skip_tool, command=self._tool_changed,
            text=f"Leave out tool data and downloaded requirements ({plan.tree.tool_files:,} file(s): package folders, "
                 "version-control data, caches)")
        self.tool_box.grid(row=5, column=0, sticky="w", padx=6, pady=2)
        if not plan.tree.tool_files:
            self.tool_box.state(["disabled"])

        self.total = tk.StringVar()
        ttk.Entry(win, textvariable=self.total, state="readonly").grid(row=6, column=0, sticky="ew", padx=6, pady=2)
        self.message = tk.StringVar(value="This folder is already indexed: these are its current choices." if known else
                                    "These are starting guesses. Change anything that does not fit.")
        ttk.Entry(win, textvariable=self.message, state="readonly").grid(row=7, column=0, sticky="ew", padx=6, pady=2)

        buttons = ttk.Frame(win)
        buttons.grid(row=8, column=0, sticky="e", padx=6, pady=6)
        if fresh is not None:
            ttk.Button(buttons, text="Start over from the guesses", command=self.start_over).pack(side="left", padx=4)
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="left", padx=4)
        self.apply_button = ttk.Button(buttons, text="Apply", command=self.apply)
        self.apply_button.pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", self.close)
        self.fill()

    # -- drawing

    def _iid(self, rel: str) -> str:
        return "n:" + rel

    def _rel(self, iid: str) -> Optional[str]:
        return iid[2:] if iid.startswith("n:") else None

    def _values(self, node: Node):
        plan = self.plan
        inside = plan.ticked(node.rel)
        marks = tuple((IN if plan.has(node.rel, option) else OUT) if inside else "" for option in OPTIONS)
        why = plan.reasons.get(node.rel.casefold(), "")
        if not plan.own_tick(node.rel):
            why = why or "left out"
        elif not why and inside:
            why = "its own choice: differs from the folder above" if plan.differs(node.rel) else \
                ("follows the folder above" if "/" in node.rel else "")
        return (IN if plan.own_tick(node.rel) else OUT, *marks, holds_text(node),
                size_text(plan.cost(self.rates, node.rel)) if inside else "", why)

    def _insert(self, parent: str, node: Node) -> None:
        iid = self._iid(node.rel)
        self.tree.insert(parent, "end", iid=iid, text=node.name, values=self._values(node),
                         tags=() if self.plan.ticked(node.rel) else ("out",))
        if any(not child.tool for child in node.children.values()):
            self.tree.insert(iid, "end", iid=PLACEHOLDER + iid)      # so the row can be opened; filled when it is

    def fill(self) -> None:
        tree = self.tree
        tree.delete(*tree.get_children(""))
        for child in self.plan.tree.top.sorted_children():
            self._insert("", child)
        self.refresh()

    def _opened(self, event=None) -> None:
        self.open(self._rel(self.tree.focus()) or "")

    def open(self, rel: str) -> None:
        """Show the folders inside one row (they are only drawn when asked for)."""
        iid = self._iid(rel)
        marker = PLACEHOLDER + iid
        if self.tree.exists(marker):
            self.tree.delete(marker)
            node = self.plan.node(rel)
            for child in (node.sorted_children() if node else []):
                self._insert(iid, child)
        self.tree.item(iid, open=True)

    def refresh(self) -> None:
        """Bring every drawn row and the total in line with the plan."""
        def visit(parent: str) -> None:
            for iid in self.tree.get_children(parent):
                rel = self._rel(iid)
                node = self.plan.node(rel) if rel is not None else None
                if node is None:
                    continue
                self.tree.item(iid, values=self._values(node), tags=() if self.plan.ticked(rel) else ("out",))
                visit(iid)
        visit("")
        inside, outside = self.plan.counts()
        learned = "from your own index" if self.rates.learned else "a starting guess"
        level = label(self.plan.base_level)
        self.total.set(f"{inside:,} folder(s) in, {outside:,} out. Files directly in this folder: {level}. "
                       f"The index would grow by about {size_text(self.plan.cost(self.rates))} (a rough estimate, {learned}).")

    # -- clicks

    def _click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        iid = self.tree.identify_row(event.y)
        rel = self._rel(iid) if iid else None
        if rel is None:
            return
        column = self.tree.identify_column(event.x)
        if column == "#1":
            self.toggle(rel)
        elif column in OPTION_COLUMN:
            self.toggle_option(rel, OPTION_COLUMN[column])

    def _selected(self, event=None) -> None:
        """Say the selected row's reason in full below (the column may be too narrow for it)."""
        rel = self._rel(self.tree.focus() or "")
        if rel is None or self.closed:
            return
        why = self.plan.reasons.get(rel.casefold(), "")
        name = rel.rsplit("/", 1)[-1]
        if why:
            self.message.set(f"{name}: {why}.")
        elif not self.plan.ticked(rel):
            self.message.set(f"{name}: left out.")
        else:
            follows = "" if self.plan.own_level(rel) or "/" not in rel else ", following the folder above"
            self.message.set(f"{name}: {label(self.plan.level(rel))}{follows}.")

    def _key(self, action) -> str:
        rel = self._rel(self.tree.focus())
        if rel is not None:
            action(rel)
        return "break"

    def toggle(self, rel: str) -> None:
        self.plan.set_ticked(rel, not self.plan.own_tick(rel))
        self.plan.reasons.pop(rel.casefold(), None)       # the starting reason no longer describes the row
        self.refresh()

    def toggle_option(self, rel: str, option: str) -> None:
        """Tick or untick one of the choices for a folder (a click in its column)."""
        if not self.plan.ticked(rel):
            self.message.set("That folder is left out. Tick its \"In\" box first.")
            return
        self.plan.set_option(rel, option, not self.plan.has(rel, option))
        self.plan.reasons.pop(rel.casefold(), None)
        name = rel.rsplit("/", 1)[-1]
        self.refresh()
        self.message.set(f"{name}: {label(self.plan.level(rel))}. Folders inside it follow, unless they have a choice of their own.")

    def apply_inside(self) -> None:
        """Make every folder inside the selected one follow it again."""
        rel = self._rel(self.tree.focus() or "")
        if rel is None:
            self.message.set("Select a folder first.")
            return
        changed = self.plan.apply_to_everything_inside(rel)
        self.refresh()
        name = rel.rsplit("/", 1)[-1]
        self.message.set(f"{changed} folder(s) inside {name} had their own choice and now follow it." if changed else
                         f"Every folder inside {name} already follows it.")

    def follow_above(self) -> None:
        """Take the selected folder's own choice away."""
        rel = self._rel(self.tree.focus() or "")
        if rel is None:
            self.message.set("Select a folder first.")
            return
        name = rel.rsplit("/", 1)[-1]
        if not self.plan.differs(rel):
            self.message.set(f"{name} already follows the folder above.")
            return
        self.plan.follow_above(rel)
        self.plan.reasons.pop(rel.casefold(), None)
        self.refresh()
        self.message.set(f"{name} follows the folder above again: {label(self.plan.level(rel))}.")

    def step_level(self, rel: str) -> None:
        if not self.plan.ticked(rel):
            self.message.set("That folder is left out. Tick its box first.")
            return
        self.plan.next_level(rel)
        self.plan.reasons.pop(rel.casefold(), None)
        self.refresh()

    def _tool_changed(self) -> None:
        self.plan.skip_tool = bool(self.skip_tool.get())
        self.refresh()

    def start_over(self) -> None:
        if self.fresh is None:
            return
        self.plan = self.fresh()
        self.skip_tool.set(self.plan.skip_tool)
        self.message.set("Back to the starting guesses.")
        self.fill()

    # -- ending

    def apply(self) -> None:
        problem = self.on_apply(self.plan)
        if problem:
            self.message.set(problem)
            return
        self.applied = True
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.window.destroy()
            # tkinter's variables must be let go here, on the window's thread. Left to the garbage collector
            # they may be freed on a background thread, which tkinter does not allow (it can hang on Windows).
            self.where = self.help = self.total = self.message = self.skip_tool = self.note = None
            if self.on_close is not None:
                self.on_close()
