"""A note when DugUp runs with administrator (or root) rights.

Nothing goes wrong because of it, so this is a note, never an error, and it never stops anything. It is
there because DugUp opens many kinds of files while it indexes, and a program that does that is best run
with ordinary rights: if a damaged or hostile file ever found a flaw in one of the readers, it would reach
only what the user can reach, not the whole machine.
"""

from __future__ import annotations

import os

TITLE = "A note from DugUp"

TEXT = ("DugUp is running with administrator rights. Nothing is wrong, and nothing has gone wrong: "
        "this is only a precaution.\n\n"
        "DugUp opens many kinds of files while it indexes. With administrator rights, a damaged file "
        "would have more reach than it needs, so DugUp is best started the ordinary way. Everything "
        "works the same either way.")

LINE = ("Note from DugUp: it is running with administrator rights. Nothing is wrong; as a precaution, "
        "it is best started the ordinary way. Everything works the same either way.")


def is_elevated() -> bool:
    """True when this process has administrator rights (Windows) or is root (elsewhere). False when it
    cannot be told: a note that cannot be sure of itself is better not shown."""
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    try:
        return geteuid is not None and geteuid() == 0
    except OSError:
        return False
