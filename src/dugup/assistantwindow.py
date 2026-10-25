"""The windows for assistant search: choosing the folders Claude may see, and asking before access is allowed.

Opened from DugUp Remote (Choose folders...) and by `dugup mcp folders`. The choices are saved in config.toml
(mcp_folders) and take effect at once, even while Claude is connected. The folder dialog starts where the last
folder was chosen, so adding several folders from one place is quick.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional

from . import assistantsetup as setup
from .shapes import Failure

LAST_FOLDER = "assistant-last-folder.txt"     # in the data folder: where the folder dialog opens next time
INTRO = ("Claude sees results only from the folders listed here. Results, passages and other copies of a file "
         "outside them are never shown. Leave out folders you would not attach to a conversation, such as "
         "medical or money records.")


def ask_allow(parent, title: str, text: str, allow: str = "Allow", cancel: str = "Cancel") -> bool:
    """A plain question in a small window of its own, Cancel by default. True when `allow` is chosen."""
    import tkinter as tk
    from tkinter import ttk

    answer = {"value": False}
    win = tk.Toplevel(parent)
    win.title(title)
    win.resizable(False, False)
    ttk.Label(win, text=text, wraplength=460, justify="left").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                   padx=16, pady=(16, 12))

    def choose(value: bool) -> None:
        answer["value"] = value
        win.destroy()

    no = ttk.Button(win, text=cancel, command=lambda: choose(False))
    yes = ttk.Button(win, text=allow, command=lambda: choose(True))
    no.grid(row=1, column=0, sticky="e", padx=(16, 4), pady=(0, 14))
    yes.grid(row=1, column=1, sticky="w", padx=(4, 16), pady=(0, 14))
    win.columnconfigure(0, weight=1)
    win.protocol("WM_DELETE_WINDOW", lambda: choose(False))
    try:
        win.attributes("-topmost", True)       # over the search box, which stays on top itself
    except Exception:
        pass
    win.bind("<Escape>", lambda event: choose(False))
    no.focus_set()
    try:
        win.transient(parent)
        win.grab_set()
    except Exception:
        pass
    parent.wait_window(win)
    return answer["value"]


class FolderChooser:
    """The folders Claude may search. `choose_folder(initialdir, title)` returns a folder or ""; a seam for tests."""

    def __init__(self, parent, layout, *, choose_folder: Optional[Callable[..., str]] = None,
                 on_saved: Optional[Callable[[object], None]] = None) -> None:
        import tkinter as tk
        from tkinter import filedialog, ttk

        self.layout = layout
        self.choose_folder = choose_folder or (lambda **kw: filedialog.askdirectory(parent=self.top, mustexist=True, **kw))
        self.on_saved = on_saved
        self.saved = None
        current = setup.folders_of(setup.load(layout))

        self.top = top = tk.Toplevel(parent)
        top.title("Folders Claude may search")
        frame = ttk.Frame(top, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=INTRO, wraplength=480, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.mode = tk.StringVar(value="all" if current is None else "these")
        ttk.Radiobutton(frame, text="Every indexed folder", value="all", variable=self.mode,
                        command=self._mode_changed).grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(frame, text="Only these folders:", value="these", variable=self.mode,
                        command=self._mode_changed).grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 4))
        self.list = tk.Listbox(frame, height=8, width=64, selectmode="extended", exportselection=False)
        self.list.grid(row=3, column=0, columnspan=3, sticky="nsew")
        for folder in current or []:
            self.list.insert("end", folder)
        self.add_button = ttk.Button(frame, text="Add folder...", command=self.add)
        self.add_button.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.remove_button = ttk.Button(frame, text="Remove", command=self.remove)
        self.remove_button.grid(row=4, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        self.message = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.message, state="readonly", width=64).grid(row=5, column=0, columnspan=3,
                                                                                    sticky="ew", pady=(10, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right", padx=(0, 6))
        frame.columnconfigure(2, weight=1)
        frame.rowconfigure(3, weight=1)
        top.protocol("WM_DELETE_WINDOW", self.cancel)
        top.bind("<Escape>", lambda event: self.cancel())
        self._mode_changed()

    def folders(self) -> List[str]:
        return list(self.list.get(0, "end"))

    def _mode_changed(self) -> None:
        state = "normal" if self.mode.get() == "these" else "disabled"
        self.list.configure(state=state)
        self.remove_button.state(["!disabled"] if state == "normal" else ["disabled"])

    def _last_place(self) -> str:
        try:
            last = (Path(self.layout.root) / LAST_FOLDER).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            last = ""
        if last and os.path.isdir(last):
            return last
        folders = self.folders()
        return folders[-1] if folders else str(Path.home())

    def add(self) -> None:
        chosen = self.choose_folder(initialdir=self._last_place(), title="A folder Claude may search")
        if not chosen:
            return
        chosen = os.path.abspath(chosen)
        self.mode.set("these")
        self._mode_changed()
        if os.path.normcase(chosen) not in {os.path.normcase(f) for f in self.folders()}:
            self.list.insert("end", chosen)
        self.message.set(f"Added {chosen}. Save to use it.")
        try:
            (Path(self.layout.root) / LAST_FOLDER).write_text(os.path.dirname(chosen) or chosen, encoding="utf-8")
        except OSError:
            pass

    def remove(self) -> None:
        for index in reversed(self.list.curselection()):
            self.list.delete(index)
        self.message.set("Removed. Save to use the list as it is now.")

    def save(self) -> None:
        only = self.mode.get() == "these"
        folders = self.folders() if only else []
        if only and not folders:
            self.message.set("Add at least one folder, or choose Every indexed folder.")
            return
        result = setup.change(self.layout, folders=folders)
        if isinstance(result, Failure):
            self.message.set(f"Not saved: {result.detail}")
            return
        self.saved = result
        if self.on_saved is not None:
            self.on_saved(result)
        self.top.destroy()

    def cancel(self) -> None:
        self.top.destroy()


class CloudSwitch:
    """"Allow cloud search access", with what it costs said beside it, and Choose folders. The same control in
    the search box (compact: one line) and in DugUp Remote. Turning it on asks first (Allow / Cancel) and offers
    to connect the Claude app; turning it off happens at once. `tell(text)` shows what happened.

    `confirm(title, text, allow, cancel)`, `choose_folder(**kw)`, `claude_config` and `find_servers` (the
    running DugUp servers, serverprocs.find) are seams for tests."""

    def __init__(self, parent, layout, *, tell: Callable[[str], None], confirm: Optional[Callable[..., bool]] = None,
                 choose_folder: Optional[Callable[..., str]] = None, claude_config=None, compact: bool = False,
                 dialog_parent=None, find_servers: Optional[Callable[[], object]] = None) -> None:
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk

        self.layout, self.tell = layout, tell
        self.dialog_parent = dialog_parent or parent
        self.confirm = confirm or (lambda title, text, allow, cancel: ask_allow(self.dialog_parent, title, text, allow, cancel))
        self.choose_folder = choose_folder
        self.claude_config = claude_config if claude_config is not None else setup.claude_config_path()
        self.find_servers = find_servers
        self.on = tk.BooleanVar(value=False)
        self.folders_text = tk.StringVar(value="")
        self.frame = frame = ttk.Frame(parent)
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self._bold = bold
        line = ttk.Frame(frame)
        line.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(line, variable=self.on, command=self.toggle).pack(side="left")
        for text, font in (("Allow ", None), ("cloud", bold), (" search access", None)):
            word = ttk.Label(line, text=text, font=font) if font else ttk.Label(line, text=text)
            word.pack(side="left")
            word.bind("<Button-1>", lambda event: self.click())
        warning = ttk.Label(frame, text=setup.TOGGLE_NOTE if not compact else "(what Claude is shown leaves this computer)",
                            foreground="#9a3412", wraplength=0 if compact else 540, justify="left")
        if compact:
            # the warning wraps under itself in a narrow box rather than being cut off: it must always be read
            warning.grid(row=0, column=1, sticky="ew", padx=(6, 6))
            warning.bind("<Configure>", lambda event: warning.configure(wraplength=max(120, event.width - 2)))
            ttk.Button(frame, text="Folders...", command=self.choose_folders).grid(row=0, column=2, sticky="e")
            frame.columnconfigure(1, weight=1)
        else:
            warning.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 4))
            ttk.Button(frame, text="Choose folders...", command=self.choose_folders).grid(row=2, column=0, sticky="w")
            ttk.Entry(frame, textvariable=self.folders_text, state="readonly", width=34).grid(row=2, column=1, sticky="ew",
                                                                                             padx=(6, 0))
            frame.columnconfigure(1, weight=1)
        self.sync()

    def sync(self) -> None:
        """Show the saved state (it may have been changed elsewhere: dugup mcp, the other window)."""
        try:
            settings = setup.load(self.layout)
        except Exception:
            return
        self.on.set(bool(settings.mcp_enabled))
        self.folders_text.set(setup.describe_folders(settings))

    def click(self) -> None:
        self.on.set(not self.on.get())
        self.toggle()

    def toggle(self) -> None:
        wanted = self.on.get()
        if wanted and not self.confirm(setup.CONFIRM_TITLE, setup.CONFIRM_TEXT, "Allow", "Cancel"):
            self.on.set(False)
            self.tell("Cloud search access stays off.")
            return
        result = setup.change(self.layout, enabled=wanted)
        if isinstance(result, Failure):
            self.on.set(not wanted)
            self.tell(f"Not changed: {result.detail}")
            return
        if not wanted:
            self.tell("Cloud search access is off. Claude can no longer search, from its next request.")
            return
        self.tell("Cloud search access is on.")
        if self.connection() != "this":
            if not self.connect_claude(asked_from_switch=True):
                self.tell("On. To connect the Claude app later: File, Connect the Claude app to this DugUp in the "
                          "main window, or run: dugup mcp connect --write")

    def connection(self) -> str:
        """"this", "other" (the Claude app starts another copy of DugUp) or "none"."""
        return setup.connection(self.claude_config, setup.server_entry(chosen_data_dir(self.layout)))

    def connect_claude(self, asked_from_switch: bool = False) -> bool:
        """Point the Claude desktop app at this DugUp, after asking. True when it was connected."""
        state = self.connection()
        if state == "this":
            # the settings are right; an older server still answering is the one thing they cannot show
            from .serverprocs import short_advice
            stale = setup.stale_servers(self.claude_config, self.find_servers)
            if stale:
                self.tell("The Claude app's settings start this DugUp. " + short_advice(stale))
            else:
                self.tell("The Claude app already starts this DugUp. If it says DugUp could not start, restart the "
                          "Claude app.")
            return True
        if state == "other":
            title, text = ("The Claude app starts another DugUp",
                           "The Claude desktop app's DugUp entry starts a different copy of DugUp:\n\n"
                           f"{setup.connected_command(self.claude_config)}\n\n"
                           "Point it at this one? It then searches the index you use here. Everything else in "
                           "the app's settings is kept, with a copy of the old file.")
        else:
            title, text = ("Connect the Claude app?",
                           "To search, the Claude desktop app needs DugUp in its settings. Add it now? "
                           "Everything else in those settings is kept, with a copy of the old file.")
        if not self.confirm(title, text, "Connect", "Not now"):
            if not asked_from_switch:
                self.tell("The Claude app was not changed.")
            return False
        done, said = setup.connect(self.claude_config, setup.server_entry(chosen_data_dir(self.layout)))
        self.tell("Connected: restart the Claude app (quit it from its tray icon) to finish." if done else said.splitlines()[0])
        return done

    def choose_folders(self) -> "FolderChooser":
        def saved(settings) -> None:
            self.sync()
            self.tell("Saved: " + self.folders_text.get())

        return FolderChooser(self.dialog_parent, self.layout, choose_folder=self.choose_folder, on_saved=saved)


def chosen_data_dir(layout) -> Optional[str]:
    """The data folder for the Claude app's entry, only when DugUp would not find it by itself."""
    from .datadir import resolve_data_dir
    try:
        found = resolve_data_dir(allow_synced=True)
    except Exception:
        return str(layout.root)
    same = os.path.normcase(os.path.abspath(str(found))) == os.path.normcase(os.path.abspath(str(layout.root)))
    return None if same else str(layout.root)
