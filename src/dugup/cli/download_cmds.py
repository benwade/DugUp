"""The `download` command: every optional download at once, with sizes and free space shown first."""

from __future__ import annotations

import signal
import threading
from typing import Callable, List, Optional

from .. import downloads, settings as settings_module
from ..models import megabytes
from ..shapes import Failure
from .common import _layout, _notes
from .output import safe_write

KEYS = ("meaning", "looks", "captions", "words")


def _chosen(args, every: List[downloads.Download]) -> List[downloads.Download]:
    if args.only:
        return [d for d in every if d.key in args.only]
    chosen = downloads.everything(every)
    if args.plain_words:
        chosen += [d for d in every if d.key == "words"]
    return chosen


def look(layout, settings):
    """This computer, quickly: cores, memory, disk, and whether a graphics card is there (no speed test)."""
    from ..service import LocalService
    with LocalService(layout, settings) as service:
        return service.look_at_machine(test_card=False)


def summary(got, chosen: List[downloads.Download]) -> str:
    """What happened, in a few lines, for the console or the window."""
    if isinstance(got, Failure):
        return f"Nothing was downloaded: {got.detail}."
    titles = {d.key: d.title for d in chosen}
    lines = []
    if got.done:
        lines.append("Ready: " + ", ".join(titles[k] for k in got.done) + ".")
    for key, why in got.failed:
        lines.append(f"Not downloaded: {titles[key]} ({why}). It will be fetched on first use, or run this again.")
    if got.stopped:
        lines.append("Stopped. What came so far is kept; run it again to continue where it left off.")
    return "\n".join(lines) or "Everything was already here."


def cmd_download(args, env, out, environ, stop: Optional[threading.Event] = None, install_signals: bool = True,
                 ask: Callable[[str], str] = input) -> int:
    layout = _layout(args, environ, create=True)
    _notes(args, environ, out)
    settings, notes = settings_module.load(layout.config)
    for note in notes:
        safe_write(f"[config] {note}", out)
    chose = ""
    if not args.list:
        settings, chose = downloads.settle_meaning_model(layout.config, settings, lambda: look(layout, settings))
        if chose and not args.window:
            safe_write(chose, out)
    every = downloads.available(layout, settings, plain_words=True)
    chosen = _chosen(args, every)
    keep_free = settings.min_free_mb * 1024 * 1024
    room = downloads.room_for(chosen, layout.free_bytes(), keep_free)
    if args.window:
        from ..downloadwindow import run_in_window
        return run_in_window(chosen, layout, keep_free, notice=chose)
    safe_write("DugUp's optional downloads (each is otherwise fetched the first time it is used):", out)
    safe_write(downloads.describe(chosen, room), out)
    left_out = [d for d in every if d not in chosen]
    if left_out:
        safe_write("Not included: " + "; ".join(f"{d.title} ({downloads.size_text(d.size)})" for d in left_out) +
                   (". Add --plain-words for the experimental model." if any(d.key == "words" for d in left_out) else "."), out)
    if args.list:
        return 0
    if not room.needed:
        safe_write("Everything chosen is already here.", out)
        return 0
    if not room.enough:
        return 1
    if not args.yes:
        if ask(f"Download {downloads.size_text(room.needed)} now? [y/N] ").strip().lower() not in ("y", "yes"):
            safe_write("Nothing was downloaded.", out)
            return 0
    stop = stop or threading.Event()
    previous = signal.getsignal(signal.SIGINT) if install_signals else None
    if install_signals:
        signal.signal(signal.SIGINT, lambda *a: stop.set())
    shown = {"percent": -5, "title": ""}

    def tell(done: int, total: int, title: str) -> None:
        percent = int(100 * done / total) if total else 100
        if title != shown["title"] or percent >= shown["percent"] + 5:
            shown.update(percent=percent, title=title)
            safe_write(f"  {percent:3d}%  {title}  ({megabytes(done)} of {megabytes(total)})", out)

    try:
        got = downloads.fetch(chosen, tell, stop, layout.free_bytes, keep_free)
    finally:
        if install_signals:
            signal.signal(signal.SIGINT, previous)
    safe_write(summary(got, chosen), out)
    return 1 if isinstance(got, Failure) or got.failed else 0
