"""The setup wizard and the Settings window: the picture of setup.SetupPlan.

Pages: a welcome (what is about to happen, and the offer to choose everything for the person), this
computer (what was found, with a short speed test of the picture reader), one page for each group of
settings the catalogue has something to ask about, and a summary. Nothing is saved until Finish.

No setting is named in this file. A row is drawn from its catalogue entry: a tick box for yes or no, a
list for a fixed set of choices, a box to type in otherwise.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Dict, List, Optional

from . import catalogue
from .catalogue import Entry, Machine
from .setup import WARNING, WHY_QUESTIONS, SetupPlan

POLL_MS = 100
WELCOME, COMPUTER, SUMMARY = "welcome", "computer", "summary"


class SetupWizard:
    """`look(stop)` returns a catalogue.Machine and may take a while (it is called on a background thread).
    `on_finish(plan)` saves; it returns "" when that worked, else what went wrong."""

    def __init__(self, parent, plan: SetupPlan, look: Optional[Callable[[threading.Event], Machine]],
                 on_finish: Callable[[SetupPlan], str], on_close: Optional[Callable[[], None]] = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.plan, self.look, self.on_finish, self.on_close = plan, look, on_finish, on_close
        self.closed = self.finished = False
        self.express = True                 # "set it up for me": skip the pages of questions
        self.looking: Optional[threading.Thread] = None
        self.stop = threading.Event()
        self.events: "queue.Queue[object]" = queue.Queue()
        self.at = 0
        self.widgets: Dict[str, object] = {}       # setting name -> the tk variable of its row
        self.hints: Dict[str, object] = {}
        self._hint_boxes: Dict[str, tuple] = {}

        win = self.window = tk.Toplevel(parent)
        win.title("Set up DugUp" if plan.first_run else "DugUp settings")
        win.geometry("900x640")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)
        self.heading = tk.StringVar()
        ttk.Label(win, textvariable=self.heading, font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        self.body = ttk.Frame(win)
        self.body.grid(row=1, column=0, sticky="nsew", padx=10)
        self.body.columnconfigure(0, weight=1)
        self.message = tk.StringVar()
        ttk.Entry(win, textvariable=self.message, state="readonly").grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        bar = ttk.Frame(win)
        bar.grid(row=3, column=0, sticky="e", padx=10, pady=10)
        self.back_button = ttk.Button(bar, text="Back", command=self.back)
        self.next_button = ttk.Button(bar, text="Next", command=self.next)
        self.cancel_button = ttk.Button(bar, text="Cancel", command=self.close)
        for button in (self.cancel_button, self.back_button, self.next_button):
            button.pack(side="left", padx=4)
        self.choice = tk.StringVar(value="express")
        win.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = win.after(POLL_MS, self._poll)
        self.show()
        if plan.first_run and look is not None:
            self.start_looking()

    # -- the sequence of pages

    def sequence(self) -> List[str]:
        """Page names in order. The pages of questions are left out on the express path."""
        pages = [WELCOME, COMPUTER] if self.plan.first_run else []
        if not (self.plan.first_run and self.express):
            pages += [group.key for group in self.plan.pages()]
        return pages + [SUMMARY]

    def page(self) -> str:
        pages = self.sequence()
        self.at = max(0, min(self.at, len(pages) - 1))
        return pages[self.at]

    def next(self) -> None:
        if self.page() == SUMMARY:
            self.finish()
            return
        if self.page() == WELCOME:
            self.express = self.choice.get() == "express"
        self.at += 1
        self.show()

    def back(self) -> None:
        self.at -= 1
        self.show()

    # -- drawing

    def _paragraph(self, text: str, row: int, lines: int = 3, indent: int = 0) -> None:
        """Wrapped text that can be selected and copied."""
        box = self.tk.Text(self.body, height=lines, wrap="word", relief="flat", borderwidth=0, font="TkDefaultFont",
                           background=self.window.cget("background"))
        box.insert("1.0", text)
        box.configure(state="disabled")
        box.grid(row=row, column=0, sticky="ew", padx=(indent, 0), pady=(0, 8 if not indent else 0))

    def _line(self, text: str, row: int, variable=None, indent: int = 0):
        variable = variable or self.tk.StringVar(value=text)
        entry = self.ttk.Entry(self.body, textvariable=variable, state="readonly")
        entry.grid(row=row, column=0, sticky="ew", padx=(indent, 0), pady=1)
        entry.variable = variable        # keeps the variable alive as long as its box
        return variable

    def show(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.widgets, self.hints, self._hint_boxes = {}, {}, {}
        name = self.page()
        self.message.set("")
        self.back_button.configure(state="normal" if self.at > 0 else "disabled")
        self.next_button.configure(text="Finish" if name == SUMMARY else "Next")
        if name == WELCOME:
            self._welcome()
        elif name == COMPUTER:
            self._computer()
        elif name == SUMMARY:
            self._summary()
        else:
            self._group(name)

    def _welcome(self) -> None:
        self.heading.set("Welcome to DugUp")
        self._paragraph("DugUp finds your files by the words in them, including words in pictures and scanned pages. "
                        "Everything stays on this computer.", 0, 2)
        self._paragraph(WARNING, 1, 4)
        self._paragraph(WHY_QUESTIONS, 2, 4)
        ttk = self.ttk
        ttk.Radiobutton(self.body, variable=self.choice, value="express",
                        text="Set it up for me (recommended). DugUp looks at this computer, chooses, and shows what it chose.").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Radiobutton(self.body, variable=self.choice, value="choose",
                        text="Let me choose. The same choices, a page at a time, each starting from DugUp's suggestion.").grid(row=4, column=0, sticky="w", pady=2)

    def _computer(self) -> None:
        self.heading.set("This computer")
        self._paragraph("DugUp looks at what this computer has, so that its suggestions fit. It also times the picture "
                        "reader on a made-up page, on the processor and on the graphics card if there is one. That takes "
                        "up to a minute and reads none of your files.", 0, 3)
        self.facts = [self._line(line, 1 + i) for i, line in enumerate(self.plan.computer_lines())]
        self.test_text = self.tk.StringVar(value="The test is running ..." if self.busy() else "")
        self._line("", 6, self.test_text)
        self.skip_button = self.ttk.Button(self.body, text="Skip the test", command=self.skip_test)
        self.skip_button.grid(row=7, column=0, sticky="w", pady=6)
        if not self.busy():
            self.skip_button.configure(state="disabled")

    def _group(self, key: str) -> None:
        group = catalogue.group(key)
        self.heading.set(group.title)
        self._paragraph(group.about, 0, 3)
        row = 1
        for entry in self.plan.rows(key):
            row = self._row(entry, row)

    def _row(self, entry: Entry, row: int) -> int:
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.body)
        frame.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        frame.columnconfigure(1, weight=1)
        value = self.plan.values[entry.name]
        kind = catalogue.kind(entry.name)
        if kind == "bool":
            variable = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(frame, text=entry.label, variable=variable,
                            command=lambda n=entry.name, v=variable: self.changed(n, "yes" if v.get() else "no")).grid(row=0, column=0, sticky="w")
        elif entry.choices:
            words = [text for _, text in entry.choices]
            variable = tk.StringVar(value=catalogue.show(entry.name, value))
            ttk.Label(frame, text=entry.label + ":").grid(row=0, column=0, sticky="w", padx=(0, 8))
            box = ttk.Combobox(frame, textvariable=variable, values=words, state="readonly", width=44)
            box.grid(row=0, column=1, sticky="w")
            box.bind("<<ComboboxSelected>>", lambda event, n=entry.name, v=variable: self.changed(n, self._choice(n, v.get())))
        else:
            variable = tk.StringVar(value=str(value) if not isinstance(value, float) else f"{value:g}")
            ttk.Label(frame, text=entry.label + (f" ({entry.unit})" if entry.unit else "") + ":").grid(row=0, column=0, sticky="w", padx=(0, 8))
            box = ttk.Entry(frame, textvariable=variable, width=20)
            box.grid(row=0, column=1, sticky="w")
            for event in ("<FocusOut>", "<Return>"):
                box.bind(event, lambda e, n=entry.name, v=variable: self.changed(n, v.get()))
        self.widgets[entry.name] = variable
        self._paragraph(entry.help + ((" " + entry.why) if entry.why else ""), row + 1, 2 if entry.why else 1, indent=18)
        hint = self.tk.StringVar(value=self.plan.hint(entry.name))
        box = ttk.Entry(self.body, textvariable=hint, state="readonly")
        box.variable = hint
        self.hints[entry.name] = hint
        self._hint_boxes[entry.name] = (box, row + 2)
        self._place_hint(entry.name)
        return row + 3

    def _place_hint(self, name: str) -> None:
        """The line that says why a value was chosen is shown only when there is something to say."""
        box, row = self._hint_boxes[name]
        if self.hints[name].get():
            box.grid(row=row, column=0, sticky="ew", padx=(18, 0), pady=1)
        else:
            box.grid_remove()

    def _choice(self, name: str, words: str) -> object:
        return next((value for value, text in catalogue.ENTRIES[name].choices if text == words), words)

    def _summary(self) -> None:
        self.heading.set("What will be set up" if self.plan.first_run else "Your settings")
        text = self.tk.Text(self.body, wrap="word", height=22, font="TkDefaultFont")
        text.grid(row=0, column=0, sticky="nsew")
        self.body.rowconfigure(0, weight=1)
        lines: List[str] = []
        if self.plan.first_run:
            lines += ["This computer", *("  " + line for line in self.plan.computer_lines()), ""]
        for title, rows in self.plan.summary():
            lines += [title, *("  " + line for line in rows), ""]
        if self.plan.first_run:
            lines += ["Next you choose which folders to index. " + WARNING]
        lines += ["", "Every choice can be changed later under Settings."]
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        self.summary_text = "\n".join(lines)
        if self.busy():
            self.message.set("The speed test is still running; its result will be added here when it ends.")

    # -- changes

    def changed(self, name: str, typed: object) -> None:
        problem = self.plan.set(name, typed)
        self.message.set(f"That will not do: {problem}." if problem else "")
        if name in self.hints:
            self.hints[name].set(self.plan.hint(name))
            self._place_hint(name)

    # -- looking at the computer

    def busy(self) -> bool:
        return self.looking is not None and self.looking.is_alive()

    def start_looking(self) -> None:
        look, stop, events = self.look, self.stop, self.events

        def work() -> None:
            try:
                events.put(look(stop))
            except Exception as exc:          # the test is a help, never a hurdle
                events.put(exc)

        self.looking = threading.Thread(target=work, name="dugup-setup-look", daemon=True)
        self.looking.start()
        if self.page() == COMPUTER:
            self.show()

    def skip_test(self) -> None:
        self.stop.set()
        self.message.set("The test was skipped. The processor will be used unless you choose the graphics card yourself.")
        if hasattr(self, "skip_button"):
            self.skip_button.configure(state="disabled")

    def drain(self) -> None:
        while True:
            try:
                found = self.events.get_nowait()
            except queue.Empty:
                return
            if self.looking is not None:
                self.looking.join()
                self.looking = None
            if isinstance(found, Machine) and not self.stop.is_set():
                self.plan.use_machine(found)
            if not self.closed:
                self.show()
                if isinstance(found, Exception):
                    self.message.set("The computer could not be looked at closely; the usual defaults are used.")

    def _poll(self) -> None:
        if self.closed:
            return
        self.drain()
        self._poll_id = self.window.after(POLL_MS, self._poll)

    # -- ending

    def finish(self) -> None:
        problem = self.on_finish(self.plan)
        if problem:
            self.message.set(problem)
            return
        self.finished = True
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.looking is not None:
            self.looking.join(5)
        try:
            self.window.after_cancel(self._poll_id)
        except Exception:
            pass
        self.window.destroy()
        # tkinter's variables are let go here, on the window's thread (see pickerwindow.py)
        self.heading = self.message = self.choice = None
        self.widgets, self.hints, self._hint_boxes = {}, {}, {}
        self.facts = []
        self.test_text = None
        if self.on_close is not None:
            self.on_close()
