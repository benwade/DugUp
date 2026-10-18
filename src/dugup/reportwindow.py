"""The "Report a problem" window: what happened, the report itself (editable), and how to send it.

It runs as a program of its own (`dugup report`), so it opens even while another DugUp window is frozen.
The report is gathered on a thread (the health check can take a few seconds) and shown when ready.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Callable, Optional

from . import report

INTRO = ("Tell us what happened, read the report below, then choose how to send it. Nothing leaves this computer "
         "until you choose.")
WHY = ("What the report holds, and why: the DugUp version and your Windows version (problems often belong to one); "
       "the health check, which says what is installed and working; the end of DugUp's log, which holds counts and "
       "reasons, never your documents' text or file names; and, if a window froze, where the program was stuck "
       "(program locations only). Folder and file names are replaced with numbered stand-ins (folder-01, "
       "file-01.pdf) that keep what helps find a problem: the drive type, file endings, and each path's length and "
       "unusual characters.\n\n"
       "What it leaves out: your files, their text, your search words, and folder and file names. Your own "
       "description is sent as you write it, so leave names out of it.\n\n"
       "Please note: a GitHub issue is public, so anyone can read it. Read the report first, and change or remove "
       "anything you would rather not share. Opening GitHub needs a free GitHub account; without one, save the "
       "report to a file instead.")


def bold_font():
    import tkinter.font as tkfont
    font = tkfont.nametofont("TkDefaultFont").copy()
    font.configure(weight="bold")
    return font


class ReportWindow:
    """`open_url`, `copy` and `save_to` are seams for tests."""

    def __init__(self, root, data_dir: Optional[str], *, open_url: Optional[Callable[[str], object]] = None,
                 copy: Optional[Callable[[str], None]] = None, ask_save: Optional[Callable[[], str]] = None,
                 gather: Callable[..., str] = report.gather) -> None:
        import tkinter as tk
        from tkinter import ttk
        import webbrowser

        self.root, self.data_dir = root, data_dir
        self.open_url = open_url or webbrowser.open
        self.copy = copy or self._to_clipboard
        self.ask_save = ask_save or self._ask_save
        self.gather = gather
        self.answers: "queue.Queue[str]" = queue.Queue()
        self.closed = False

        root.title("Report a problem with DugUp")
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=INTRO, wraplength=620, justify="left", font=bold_font()).grid(row=0, column=0,
                                                                                          columnspan=4, sticky="w")
        ttk.Label(frame, text=WHY, wraplength=620, justify="left").grid(row=1, column=0, columnspan=4, sticky="w",
                                                                         pady=(6, 0))
        ttk.Label(frame, text="What happened?").grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 2))
        self.what = tk.Text(frame, height=4, width=80, wrap="word")
        self.what.grid(row=3, column=0, columnspan=4, sticky="ew")
        ttk.Label(frame, text="The report (you can change or remove anything in it):").grid(row=4, column=0, columnspan=4,
                                                                                              sticky="w", pady=(10, 2))
        self.text = tk.Text(frame, height=18, width=80, wrap="none")
        self.text.grid(row=5, column=0, columnspan=3, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        scroll.grid(row=5, column=3, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        self.text.insert("1.0", "Gathering the report (a few seconds)...")
        self.hide_names = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Hide folder and file names (recommended)", variable=self.hide_names,
                        command=self._show_report).grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.raw = ""
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.github_button = ttk.Button(buttons, text="Open GitHub to report", command=self.to_github)
        self.github_button.pack(side="left")
        ttk.Button(buttons, text="Copy", command=self.copy_all).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Save to file...", command=self.save).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        self.status = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.status, state="readonly").grid(row=8, column=0, columnspan=4, sticky="ew",
                                                                         pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.ready = False
        threading.Thread(target=self._gather, name="dugup-report-gather", daemon=True).start()
        self._poll_id = root.after(100, self._poll)

    def _gather(self) -> None:
        try:
            text = self.gather(self.data_dir, hide_names=False)
        except Exception as exc:
            text = f"The report could not be gathered: {type(exc).__name__}: {exc}"
        self.answers.put(text)

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            text = self.answers.get_nowait()
        except queue.Empty:
            self._poll_id = self.root.after(100, self._poll)
            return
        self.raw = text
        self._show_report()
        self.ready = True
        self.status.set("Ready. Describe what happened above, then choose how to send it.")

    def _show_report(self) -> None:
        """The report as it will be sent: names hidden unless the person chose otherwise. Changes made by hand are
        replaced when the choice changes (it is said so)."""
        if not self.raw:
            return
        shown = report.hide(self.raw, self.data_dir) if self.hide_names.get() else self.raw
        self.text.delete("1.0", "end")
        self.text.insert("1.0", shown)
        if self.ready:
            self.status.set("Folder and file names are hidden." if self.hide_names.get() else
                            "Folder and file names are shown as they are. Read the report before sending it.")

    def body(self) -> str:
        """The report as it will be sent: the description put in its place, plus whatever was changed by hand."""
        what = self.what.get("1.0", "end").strip()
        text = self.text.get("1.0", "end").rstrip() + "\n"
        marker = "(please describe what you were doing and what went wrong)"
        if what and marker in text:
            text = text.replace(marker, what, 1)
        elif what:
            text = "## What happened\n" + what + "\n\n" + text
        return report.redact(text)

    def _to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()                    # the clipboard keeps it after the window closes

    def _ask_save(self) -> str:
        from tkinter import filedialog
        return filedialog.asksaveasfilename(parent=self.root, title="Save the report", defaultextension=".md",
                                            initialfile="DugUp problem report.md",
                                            filetypes=(("Text", "*.md *.txt"), ("All files", "*.*")))

    def to_github(self) -> None:
        body = self.body()
        self.copy(body)                       # the whole of it, in case the address has to be cut short
        url, cut = report.issue_url(body)
        self.open_url(url)
        self.status.set("GitHub is opening with the report filled in; check it and choose Submit new issue."
                        + (" It was long: the whole report is on your clipboard to paste." if cut else ""))

    def copy_all(self) -> None:
        self.copy(self.body())
        self.status.set("The report is on your clipboard.")

    def save(self) -> None:
        path = self.ask_save()
        if not path:
            return
        try:
            Path(path).write_text(self.body(), encoding="utf-8")
        except OSError as exc:
            self.status.set(f"Not saved: {exc.strerror or exc}")
            return
        self.status.set(f"Saved to {path}.")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.root.after_cancel(self._poll_id)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
