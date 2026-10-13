"""The choices of the setup wizard and the Settings window, without the window (it is drawn by setupwindow.py).

Everything shown comes from the catalogue: the groups are the pages, the everyday entries are the rows.
Nothing in this file names a particular setting, so a new setting, or a whole new group, appears in the
wizard by being described in catalogue.py.

  SetupPlan      the values chosen so far, starting from the settings in use and, on a first run, from what
                 suits this computer; each suggestion keeps its reason
  pages()        the pages to show: the groups that have something to ask
  summary()      every choice in plain words, with the reason where there is one
  changes()      what would be saved
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

from . import catalogue
from .catalogue import Entry, Group, Machine
from .settings import Settings
from .shapes import Failure

WARNING = ("Setting up means reading your files once. That uses a lot of the computer for a while: the processor, "
           "the disk and, if you choose it, the graphics card. This is temporary. Once the index is built, DugUp only "
           "reads what is new or changed, and searching itself is light.")
WHY_QUESTIONS = ("DugUp has many settings because computers and collections of files differ a great deal. You do not "
                 "have to answer any of them: it can look at this computer and choose for you, and tell you what it "
                 "chose and why. Every choice can be changed later under Settings.")


class SetupPlan:
    def __init__(self, settings: Settings, machine: Optional[Machine] = None, first_run: bool = True,
                 advanced: bool = False) -> None:
        self.settings = settings
        self.machine = machine or Machine()
        self.first_run = first_run
        self.advanced = advanced          # show every setting, not only the everyday ones
        self.values: Dict[str, object] = {name: getattr(settings, name) for name in catalogue.ENTRIES}
        self.reasons: Dict[str, str] = {}
        self.suggested: Dict[str, Tuple[object, str]] = {}
        self.use_machine(self.machine)

    def use_machine(self, machine: Machine) -> None:
        """Take in what was found on the computer. On a first run the suggestions become the values; later
        they are only shown beside the value in use, which a person chose and keeps."""
        self.machine = machine
        self.suggested = catalogue.suggestions(machine)
        untouched = {name for name in self.suggested
                     if self.values[name] == getattr(self.settings, name) or name in self.reasons}
        self.reasons = {}
        for name, (value, reason) in self.suggested.items():
            if self.first_run and name in untouched and getattr(self.settings, name) == catalogue.default(name):
                self.values[name] = value
                self.reasons[name] = reason

    # -- what to show

    def pages(self) -> List[Group]:
        return [group for group in catalogue.GROUPS if self.rows(group.key)]

    def rows(self, group_key: str) -> List[Entry]:
        return catalogue.in_group(group_key, everyday_only=not self.advanced)

    def shown(self, name: str) -> str:
        return catalogue.show(name, self.values[name])

    def hint(self, name: str) -> str:
        """The line under a setting: why this value, or what DugUp would suggest instead."""
        if name in self.reasons:
            return f"Chosen for this computer: {self.reasons[name]}."
        if name in self.suggested and self.suggested[name][0] != self.values[name]:
            value, reason = self.suggested[name]
            return f"DugUp would suggest {catalogue.show(name, value)}: {reason}."
        return ""

    # -- changing

    def set(self, name: str, typed: object) -> str:
        """Take a value a person gave. "" when it was taken, else why it will not do (nothing changes then)."""
        value = catalogue.parse(name, typed)
        if isinstance(value, Failure):
            return value.detail
        together = catalogue.apply(self.settings, {**self.changes(), name: value})
        if isinstance(together, Failure):
            return together.detail
        self.values[name] = value
        self.reasons.pop(name, None)       # a person's own choice: the computer's reason no longer applies
        return ""

    def use_suggestion(self, name: str) -> None:
        if name in self.suggested:
            self.values[name], self.reasons[name] = self.suggested[name]

    # -- the result

    def changes(self) -> Dict[str, object]:
        """What differs from the settings in use."""
        return {name: value for name, value in self.values.items() if value != getattr(self.settings, name)}

    def summary(self) -> List[Tuple[str, List[str]]]:
        """(page title, lines) for every page: each setting shown, its value in words, and the reason if any."""
        out = []
        for group in self.pages():
            lines = []
            for entry in self.rows(group.key):
                line = f"{entry.label}: {self.shown(entry.name)}"
                if entry.name in self.reasons:
                    line += f"  ({self.reasons[entry.name]})"
                elif entry.name in self.suggested and self.suggested[entry.name][0] != self.values[entry.name]:
                    line += f"  (your choice; DugUp would suggest {catalogue.show(entry.name, self.suggested[entry.name][0])})"
                elif self.values[entry.name] != getattr(self.settings, entry.name):
                    line += "  (your choice)"
                lines.append(line)
            out.append((group.title, lines))
        return out

    def computer_lines(self) -> List[str]:
        m = self.machine
        gb = lambda count: "not known" if count is None else f"{count / 1024 ** 3:,.0f} GB"
        lines = [f"Processor cores: {m.cores}", f"Memory free now: {gb(m.free_memory_bytes)}",
                 f"Disk space free where the index is kept: {gb(m.free_disk_bytes)}"]
        if not m.card:
            lines.append("Graphics card for reading pictures: none that the reader can use (the processor is used)")
        elif m.card_speedup is None:
            lines.append(f"Graphics card for reading pictures: {m.card}, not tested yet")
        else:
            lines.append(f"Graphics card for reading pictures: {m.card}, {m.card_speedup:.1f} times the processor's "
                         "speed on a sample page")
        return lines
