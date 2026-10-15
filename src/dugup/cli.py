"""Command line: a small client of the Service (guide 5c item 7).

`search`, `status`, `index` and `roots` go through the Service. `init` and `doctor` are the bootstrap
commands that must work before any index exists. Everything printed goes through `safe_write`, so a
console in a legacy code page shows an escape instead of crashing on a name it cannot display.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import datetime
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from typing import Callable, List, Optional, TextIO

from . import __version__
from . import catalogue
from .settings import Settings
from . import settings as settings_module
from .datadir import DataDirError, DataLayout, ensure_data_dir, never_write_from_env, resolve_data_dir
from .doctor import Environment, has_failure, render, run_checks
from .filesource import SUGGESTED_EXCLUDES
from .indexer import KEEP, REFUSE, REREAD, IndexReport, Progress
from .protocols import WARN_MASS_LOSS, WARN_ROOT_EMPTY
from .service import LocalService
from .shapes import LEVEL_WORDS, LEVELS, OPTION_OCR, POLICIES, POLICY_OPEN, Failure, FailureReason, Root, SearchFilters, SearchPage, SearchRow, WHERE_BOTH, WHERE_LOOKS, WHERE_MEANING
from .store import SORT_COLUMNS, StoreError
from .suggest import SAFE, Suggestion

MISSING_NOTE = ("Not at this path at the last scan. If this was on a temporarily unavailable disk, reconnect it and "
                "scan again and the file will be restored. Otherwise it will be removed from the index on {date}.")
UNAVAILABLE_NOTE = "Its folder is not available right now; the file is kept in the index."
WARNING_TEXT = {
    WARN_ROOT_EMPTY: "the folder is reachable but empty, and it was not before. Nothing was marked missing.",
    WARN_MASS_LOSS: "at least half of the folder's files disappeared at once. Nothing was marked missing.",
}


def safe_write(text: str, stream: TextIO, end: str = "\n") -> None:
    """Write text to a console that may not be able to show every character.

    A Windows console in a legacy code page raises on characters it cannot show
    (for example a Japanese folder name). Show an escape instead of crashing.
    """
    if stream is None:        # started without a console (the window's launcher): there is nowhere to write
        return
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(text.encode(encoding, "backslashreplace").decode(encoding) + end)
    stream.flush()


# ---- small formatting helpers ----------------------------------------------------------------------------

def format_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def format_date(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "never"
    try:
        return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def one_line(text: str) -> str:
    return " ".join(text.split())


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 3)] + "..."


def progress_text(p: Progress) -> str:
    """One line that says where a run is: phase, counts, percent, rate, time left, current file."""
    if p.phase == "scan":
        return f"[scan] looking through {p.root}"
    if p.phase == "record":
        return f"[scan] recording changes: {p.done:,} of {p.total:,}"
    if p.phase == "estimate":
        left = p.remaining_seconds
        left_text = "" if left is None else f" and take about {format_seconds(left)} more"
        return (f"[estimate] after {p.done:,} of {p.total:,} files: at this rate the index would end near "
                f"{p.projected_bytes / 1048576:,.0f} MB{left_text}. Ctrl+C stops; what is done is kept.")
    if p.phase == "download":
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        return (f"[download] {p.root}: {p.done / 1048576:,.1f} of {p.total / 1048576:,.1f} MB ({p.percent:.0f}%)  "
                f"{p.rate / 1048576:.1f} MB/s  {left_text}")
    if p.phase == "unpack":
        return f"[download] unpacking {p.root}"
    if p.phase == "mark":
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        line = (f"[same files] marking what was read before: {p.done:,} of {p.total:,} ({p.percent:.1f}%)  "
                f"{p.rate:.1f} files/s  {left_text}")
        return line + (f"  | {shorten(os.path.basename(p.current), 40)}" if p.current else "")
    if p.phase == "compare":
        return f"[alike] comparing: {p.done:,} of {p.total:,} pictures"
    if p.phase == "caption":
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        return (f"[rename] writing captions: {p.done:,} of {p.total:,} ({p.percent:.1f}%)  {left_text}"
                + (f"  | {shorten(os.path.basename(p.current), 40)}" if p.current else ""))
    if p.phase == "look":
        if p.root:
            return f"[alike] {p.root} on the {p.current}: {p.done:,} of {p.total:,}"
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        line = f"[alike] looking at pictures: {p.done:,} of {p.total:,} ({p.percent:.1f}%)  {p.rate:.1f} pictures/s  {left_text}  unread {p.failed:,}"
        return line + (f"  | {shorten(os.path.basename(p.current), 40)}" if p.current else "")
    if p.phase == "sketch":
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        line = f"[meaning] sketching: {p.done:,} of {p.total:,} file(s) ({p.percent:.1f}%)  {p.rate:.1f} files/s  {left_text}"
        return line + (f"  | {shorten(os.path.basename(p.current), 40)}" if p.current else "")
    if p.phase == "measure":
        if p.current:
            return f"[meaning] {p.root} on the {p.current}: {p.done:,} of {p.total:,} piece(s)"
        return f"[ocr] looking at the size of the pictures: {p.done:,} of {p.total:,}"
    left = p.remaining_seconds
    left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
    if p.phase == "ocr":
        line = (f"[ocr] {p.done:,} of {p.total:,} page(s) ({p.percent:.1f}%)  {p.rate * 60:.1f} pages/min  "
                f"{left_text}  failed {p.failed:,}")
    else:
        line = (f"[read] {p.done:,} of {p.total:,} ({p.percent:.1f}%)  {p.rate:.1f} files/s  "
                f"{left_text}  failed {p.failed:,}")
    if p.current:
        line += "  | " + shorten(os.path.basename(p.current), 40)
    return line


class ProgressPrinter:
    """Prints progress at most once a second: phase, counts, percent, rate, time left, current file."""

    def __init__(self, stream: TextIO, interval: float = 1.0, timer: Callable[[], float] = time.monotonic) -> None:
        self.stream = stream
        self.interval = interval
        self.timer = timer
        self.live = bool(getattr(stream, "isatty", lambda: False)())
        self._last_time: Optional[float] = None
        self._last_phase = ""
        self._width = 0

    def __call__(self, p: Progress) -> None:
        now = self.timer()
        fresh = p.phase != self._last_phase or p.done >= p.total > 0
        if not fresh and self._last_time is not None and now - self._last_time < self.interval:
            return
        if p.phase == "estimate":       # a line of its own that stays on the screen
            self.finish()
            safe_write(progress_text(p), self.stream)
            return
        self._last_time, self._last_phase = now, p.phase
        line = progress_text(p)
        if self.live:
            pad = " " * max(0, self._width - len(line))
            self._width = len(line)
            safe_write("\r" + line + pad, self.stream, end="")
        else:
            safe_write(line, self.stream)

    def finish(self) -> None:
        if self.live and self._width:
            safe_write("", self.stream)
            self._width = 0


# ---- the parser ------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", help="the program's data folder (default: environment, then per-user folder)")
    common.add_argument("--never-write", action="append", default=[], metavar="FOLDER",
                        help="a folder the program must never write to (repeatable)")
    common.add_argument("--allow-synced", action="store_true", help="allow a data folder that looks cloud-synced")

    parser = argparse.ArgumentParser(prog="dugup", description="DugUp: local file search.")
    parser.add_argument("--version", action="version", version=f"dugup {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", parents=[common], help="check that this machine is ready and report what is missing")
    sub.add_parser("init", parents=[common], help="create the data folder and an empty index")

    roots = sub.add_parser("roots", help="choose the folders to index")
    roots_sub = roots.add_subparsers(dest="roots_command", required=True)
    add = roots_sub.add_parser("add", parents=[common], help="add a folder, or change its policy and rules")
    add.add_argument("path")
    add.add_argument("--policy", choices=POLICIES, default=POLICY_OPEN,
                     help="open: names and text (default). names_only: file names only, no text is kept")
    add.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                     help="skip files and folders matching this pattern, for example .git or *.tmp (repeatable)")
    add.add_argument("--include", action="append", default=[], metavar="PATTERN",
                     help="index only what matches this pattern (repeatable)")
    add.add_argument("--suggested-excludes", action="store_true",
                     help="also skip: " + ", ".join(SUGGESTED_EXCLUDES))
    suggest = roots_sub.add_parser("suggest", parents=[common],
                                   help="look through a folder and list what may not be worth indexing (changes nothing)")
    suggest.add_argument("path")
    level = roots_sub.add_parser("level", parents=[common],
                                 help="how far indexing goes in one folder (a subfolder's level wins over the folder above it)")
    level.add_argument("path", help="an indexed folder, or any folder inside one")
    level.add_argument("level",
                       help="names: file names only. Or any of these joined by +: words (text of documents), ocr (words in "
                            "pictures and scans), meaning, alike (pictures by what they show); for example words+ocr. "
                            "meaning alone is short for everything, the default. inherit: follow the folder above again")
    look = roots_sub.add_parser("survey", parents=[common],
                                help="what a folder holds and what indexing it would cost, folder by folder (opens no file, changes nothing)")
    look.add_argument("path")
    look.add_argument("--rows", type=int, default=30, help="how many folders to list, largest first (default 30)")
    alike = roots_sub.add_parser("copies", parents=[common],
                                 help="folders that look like copies of other folders, from names and sizes (opens no file, changes nothing)")
    alike.add_argument("path")
    alike.add_argument("--rows", type=int, default=40, help="how many sets to list, most extra files first (default 40)")
    suggest.add_argument("--apply-safe", action="store_true",
                         help="skip everything in the 'safe to skip' list (downloaded requirements, tool data)")
    exclude = roots_sub.add_parser("exclude", parents=[common], help="skip more files or folders in an indexed folder")
    exclude.add_argument("path")
    exclude.add_argument("patterns", nargs="+", metavar="PATTERN",
                         help="a folder or file name (skipped wherever it appears), a path below the folder, or a pattern such as *.tmp")
    exclude.add_argument("--undo", action="store_true", help="stop skipping these patterns instead")
    roots_sub.add_parser("list", parents=[common], help="show the folders and when each was last scanned")
    remove = roots_sub.add_parser("remove", parents=[common], help="stop indexing a folder and remove its files from the index")
    remove.add_argument("path")
    remove.add_argument("--keep-index", action="store_true", help="forget the folder but keep what was indexed")

    index = sub.add_parser("index", parents=[common], help="bring the index up to date (Ctrl+C stops cleanly; run again to continue)")
    index.add_argument("--root", action="append", default=None, metavar="FOLDER", help="only this folder (repeatable)")
    index.add_argument("--retry-failed", action="store_true", help="try the files that failed last time again")
    index.add_argument("--no-ocr", action="store_true",
                       help="stop after the index is up to date; do not go on to read pictures and scans (dugup ocr does that later)")
    index.add_argument("--no-meaning", action="store_true",
                       help="do not go on to sketch meaning after reading (dugup meaning fill does that later)")
    index.add_argument("--no-looks", action="store_true",
                       help="do not go on to look at new pictures after reading (dugup alike fill does that later)")
    choice = index.add_mutually_exclusive_group()
    choice.add_argument("--reread", action="store_true", help="after a settings change: read every file again")
    choice.add_argument("--keep", action="store_true", help="after a settings change: keep what is stored as it is")

    ocr = sub.add_parser("ocr", parents=[common],
                         help="read the words in pictures and scanned pages (Ctrl+C stops cleanly; run again to continue)")
    ocr.add_argument("--root", action="append", default=None, metavar="FOLDER", help="only this folder (repeatable)")
    ocr.add_argument("--minutes", type=float, default=None, metavar="N", help="stop after about N minutes")
    ocr.add_argument("--retry-failed", action="store_true", help="try the pages OCR failed on again")
    ocr.add_argument("--folder", default=None, metavar="FOLDER",
                     help="only what lies in this folder: an indexed folder or any folder inside one")
    ocr.add_argument("--fast", action="store_true",
                     help="several readers side by side; the computer is busier while it runs")
    ocr.add_argument("--readers", type=int, default=0, metavar="N", help="fast, with exactly N readers")
    ocr.add_argument("--gentle", action="store_true",
                     help="one page at a time, even when many wait (the usual run goes fast from ocr_fast_pages pages)")
    ocr.add_argument("--gpu", action="store_true", default=None,
                     help="read on the graphics card, when the OCR runtime can use one (see dugup doctor)")
    ocr.add_argument("--no-gpu", dest="gpu", action="store_false", help="read on the processor even if config.toml says ocr_gpu = true")
    ocr.add_argument("--again", action="store_true",
                     help="with --folder: read its pictures and scans afresh, even where that was done before")
    ocr.add_argument("--reread-old", action="store_true",
                     help="read again the pages that an older version of the OCR engine read")

    space = sub.add_parser("space", parents=[common], help="how much disk the index uses, and its spending limit")
    space.add_argument("--compact", action="store_true",
                       help="give back the disk space that removed and re-read text left in the index")
    space.add_argument("--limit", type=int, default=None, metavar="MB",
                       help="stop indexing and OCR when the index is larger than this many megabytes (0: no limit)")

    window = sub.add_parser("window", parents=[common], help="open the search window")
    window.add_argument("--search", metavar="WORDS", help="search for these words as soon as the window opens")
    tray = sub.add_parser("tray", parents=[common], help="stay in the tray, with a key that brings up a search box anywhere")
    tray.add_argument("--startup", choices=("add", "remove"), help="make the tray start when you log in, or stop that")
    tray.add_argument("--quit", action="store_true", help="end the tray that is running")
    tray.add_argument("--no-icon", action="store_true", help="no icon in the tray (the key and `dugup popup` still work)")
    sub.add_parser("popup", parents=[common], help="bring up the running tray's search box")

    config = sub.add_parser("config", help="show and change settings (each with a line of guidance)")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    listing = config_sub.add_parser("list", parents=[common], help="the everyday settings and their values")
    listing.add_argument("--all", action="store_true", help="every setting, the advanced ones too")
    getting = config_sub.add_parser("get", parents=[common], help="one setting: its value, what it does, its choices")
    getting.add_argument("name")
    setting = config_sub.add_parser("set", parents=[common], help="change one setting (checked before it is saved)")
    setting.add_argument("name")
    setting.add_argument("value", nargs="+")
    resetting = config_sub.add_parser("reset", parents=[common], help="put one setting back to its default")
    resetting.add_argument("name")

    models_parser = sub.add_parser("models", help="the optional language model for plain-words search")
    models_sub = models_parser.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser("status", parents=[common], help="what is installed, and what an install would download")
    install = models_sub.add_parser("install", parents=[common],
                                    help="download the model and its runtime (Ctrl+C stops; run again to continue)")
    install.add_argument("--yes", action="store_true", help="go ahead; without this the plan is shown and nothing is downloaded")
    install.add_argument("--model", choices=["small", "larger"], default="small",
                         help="small: quick, 0.3 GB. larger: reads sentences much better, 2.2 GB, about 3 GB of memory, "
                              "several seconds a sentence without a graphics card")
    models_sub.add_parser("remove", parents=[common], help="delete the downloaded model and runtime")

    ask = sub.add_parser("ask", parents=[common],
                         help="show how a sentence in plain words would fill in the search form, and how long that took")
    ask.add_argument("sentence", nargs="+")
    ask.add_argument("--reader", choices=["rules", "model"], default=None,
                     help="try the built-in rules or the installed model, whatever config.toml says")

    search = sub.add_parser("search", parents=[common], help="find files by the words in them or in their names")
    search.add_argument("words", nargs="+")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--sort", choices=sorted(SORT_COLUMNS), default="rank")
    search.add_argument("--desc", action="store_true", help="largest (or last) first")
    search.add_argument("--exact", action="store_true",
                        help="only the very word forms typed: education then does not find educate")
    search.add_argument("--every-copy", action="store_true",
                        help="list every file; the usual is to show a file kept in several places once, with its other places")
    search.add_argument("--here", action="store_true",
                        help="only files that are here now: not ones gone since the last scan, nor ones on a drive not reachable now")
    search.add_argument("--meaning", action="store_true",
                        help="find passages by what they are about rather than by the words: a question in your own words "
                             "(needs sketches: dugup meaning fill)")
    search.add_argument("--both", action="store_true",
                        help="the words and the meaning together: a name in the question counts, and so does the sense")
    search.add_argument("--looks", action="store_true",
                        help="pictures that look like what the sentence says (needs sketches of pictures: dugup alike fill)")
    search.add_argument("--context", type=int, default=60, help="characters shown on each side of a match")

    dup = sub.add_parser("duplicates", parents=[common],
                         help="the same file kept in several places on one drive, with the copy to keep marked (changes nothing)")
    dup.add_argument("drive", help="the drive or folder to look in, for example G:\\ (copies on other drives are not duplicates)")
    dup.add_argument("--protect", action="append", default=[], metavar="FOLDER",
                     help="a folder never proposed for removal (as well as the setting cleanup_protect); may be repeated")
    dup.add_argument("--prefer", action="append", default=[], metavar="FOLDER",
                     help="a folder that is the proper home of its files: a copy there is the one to keep (as well as "
                          "the setting cleanup_prefer); may be repeated")
    dup.add_argument("--rows", type=int, default=50, help="how many sets to show, most space first (default 50)")
    dup.add_argument("--save", metavar="FILE", help="write the whole report to a text file (keep or extra, path, size, files in its folder)")
    dup.add_argument("--remove", action="store_true",
                     help="move every 'extra' to the trash, each after its keeper is found to hold the same bytes now; "
                          "the list is shown and a 'yes' asked for first; needs --record")
    dup.add_argument("--record", metavar="FILE", help="with --remove: a file that will list what was moved and what was left, and why")
    dup.add_argument("--yes", action="store_true", help="with --remove: do not ask; answer yes")
    dup.add_argument("--try-trash", metavar="FILE", dest="try_trash",
                     help="move this one file to the trash and say where it went, to see how the trash behaves here")
    meaning = sub.add_parser("meaning", parents=[common], help="searching by meaning: sketches of what each passage is about")
    meaning_sub = meaning.add_subparsers(dest="meaning_command", required=True)
    gauge = meaning_sub.add_parser("measure", parents=[common],
                                   help="make sketches of a sample of the index with the pinned models and report speed, "
                                        "disk, and what a few questions find (fetches the models; stores nothing)")
    gauge.add_argument("--pieces", type=int, default=2000, help="how many pieces of text to sample (default 2000)")
    gauge.add_argument("--model", choices=("small", "larger", "both"), default="both")
    gauge.add_argument("--gpu", dest="gpu", action="store_true", default=None, help="the graphics card only")
    gauge.add_argument("--no-gpu", dest="gpu", action="store_false", help="the processor only (default: both when the runtime can)")
    gauge.add_argument("--ask", action="append", default=[], metavar="SENTENCE",
                       help="a question to try on the sample, in plain words; may be repeated")
    filling = meaning_sub.add_parser("fill", parents=[common],
                                     help="make sketches of meaning for the files that have none yet, the most recently "
                                          "changed first (Ctrl+C stops; run again to continue)")
    filling.add_argument("--budget", type=float, default=None, metavar="SECONDS", help="stop after this long; run again to continue")
    filling.add_argument("--gpu", dest="gpu", action="store_true", default=None, help="sketch on the graphics card")
    filling.add_argument("--no-gpu", dest="gpu", action="store_false", help="sketch on the processor (default: as the setting meaning_gpu says)")
    meaning_sub.add_parser("status", parents=[common], help="how much of the index has sketches of meaning")
    alike = sub.add_parser("alike", parents=[common], help="pictures by likeness: like one another, or like a sentence (measured first)")
    alike_sub = alike.add_subparsers(dest="alike_command", required=True)
    gauge2 = alike_sub.add_parser("measure", parents=[common],
                                  help="make sketches of a sample of the index's pictures with the pinned picture model and report "
                                       "speed, disk, and what a few sentences or an example picture find (fetches the model; stores nothing)")
    gauge2.add_argument("--pictures", type=int, default=300, help="how many pictures to sample (default 300)")
    gauge2.add_argument("--gpu", dest="gpu", action="store_true", default=None, help="the graphics card only")
    gauge2.add_argument("--no-gpu", dest="gpu", action="store_false", help="the processor only (default: both when the runtime can)")
    gauge2.add_argument("--ask", action="append", default=[], metavar="SENTENCE",
                        help="a sentence to look for in the sample, such as \"a dog on a beach\"; may be repeated")
    gauge2.add_argument("--like", action="append", default=[], metavar="PICTURE",
                        help="a picture whose likes to find in the sample; may be repeated")
    filling2 = alike_sub.add_parser("fill", parents=[common],
                                    help="make sketches of the pictures that have none yet, the most recently changed first, "
                                         "several readers side by side (Ctrl+C stops; run again to continue)")
    filling2.add_argument("--budget", type=float, default=None, metavar="SECONDS", help="stop after this long; run again to continue")
    filling2.add_argument("--readers", type=int, default=None, help="readers side by side (default: the setting looks_readers)")
    filling2.add_argument("--gpu", dest="gpu", action="store_true", default=None, help="on the graphics card")
    filling2.add_argument("--no-gpu", dest="gpu", action="store_false", help="on the processor (default: as the setting looks_gpu says)")
    alike_sub.add_parser("status", parents=[common], help="how many pictures have sketches, and which could not be read")
    twins = alike_sub.add_parser("duplicates", parents=[common],
                                 help="sets of pictures that look nearly the same, with the one to keep; --remove moves the rest to the trash")
    twins.add_argument("--threshold", type=float, default=0.97, help="closeness at and above which pictures count as the same (default 0.97)")
    twins.add_argument("--protect", action="append", default=[], metavar="FOLDER", help="never propose pictures below this folder")
    twins.add_argument("--prefer", action="append", default=[], metavar="FOLDER", help="the copy below this folder is the one kept")
    twins.add_argument("--rows", type=int, default=50, help="how many sets to show")
    twins.add_argument("--remove", action="store_true", help="move the extras to the trash (asks first; needs --record)")
    twins.add_argument("--record", metavar="FILE", help="with --remove: a file that lists what was moved")
    twins.add_argument("--yes", action="store_true", help="with --remove: do not ask")
    liking = alike_sub.add_parser("like", parents=[common], help="pictures in the index that look like a picture")
    liking.add_argument("picture", help="a picture file, in the index or not")
    liking.add_argument("--limit", type=int, default=20)
    liking.add_argument("--offset", type=int, default=0)
    rename = sub.add_parser("rename", parents=[common],
                            help="name the pictures in a folder by what they show (a preview; --apply renames; --undo puts names back)")
    rename.add_argument("folder", nargs="?", help="an indexed folder, or a folder inside one")
    rename.add_argument("--pattern", help="the name pattern (default: the setting rename_pattern, \"{caption} {taken}\")")
    rename.add_argument("--all", dest="every", action="store_true",
                        help="every picture, not only those whose name is a camera's (IMG_..., 20220401_142335, Screenshot ...)")
    rename.add_argument("--no-caption", action="store_true", help="do not write captions; {caption} is then empty and such files are left alone")
    rename.add_argument("--longer", action="store_true", help="ask the model for a longer description (slower; more words to choose from)")
    rename.add_argument("--apply", action="store_true", help="rename as the preview says (needs --record)")
    rename.add_argument("--record", metavar="FILE", help="with --apply: a file that lists every rename, for --undo")
    rename.add_argument("--yes", action="store_true", help="with --apply: do not ask")
    rename.add_argument("--undo", metavar="RECORD", help="put the names in this record back, last first")
    rename.add_argument("--gpu", dest="gpu", action="store_true", default=None)
    rename.add_argument("--no-gpu", dest="gpu", action="store_false")
    status = sub.add_parser("status", parents=[common], help="what is indexed, what is waiting, what needs attention")
    status.add_argument("--unreadable", action="store_true",
                        help="list every file that cannot be read (damaged, or needs a password)")
    status.add_argument("--save", metavar="FILE",
                        help="write that list to a text file (path, a tab, the reason) instead of the screen")
    status.add_argument("--attention", nargs="?", type=int, const=50, default=None, metavar="N",
                        help="list up to N files that failed or were read only in part (default 50)")
    return parser


# ---- commands --------------------------------------------------------------------------------------------

def _layout(args, environ, create: bool) -> DataLayout:
    never_write = list(args.never_write) + never_write_from_env(environ)
    data_dir = resolve_data_dir(args.data_dir, environ=environ, never_write=never_write, allow_synced=args.allow_synced)
    if create:
        ensure_data_dir(data_dir, never_write, args.allow_synced)
        return DataLayout(data_dir).ensure()
    return DataLayout(data_dir)


def _service(args, environ, out: TextIO, create: bool) -> Optional[LocalService]:
    layout = _layout(args, environ, create)
    if not create and not layout.index.exists():
        safe_write(f"There is no index yet in {layout.root}. Start with: dugup roots add <folder>", out)
        return None
    settings, notes = settings_module.load(layout.config)
    for note in notes:
        safe_write(f"[config] {note}", out)
    return LocalService(layout, settings)


def _fail(failure: Failure, out: TextIO) -> int:
    safe_write(f"Could not do that ({failure.reason.value.replace('_', ' ')}): {failure.detail}", out)
    return 1


def cmd_doctor(args, env, out, environ) -> int:
    never_write = list(args.never_write) + never_write_from_env(environ)
    try:
        data_dir = resolve_data_dir(args.data_dir, environ=environ, never_write=never_write, allow_synced=args.allow_synced)
    except DataDirError as exc:
        from .doctor import FAIL, Check
        checks = run_checks(env)
        checks.append(Check("data folder", FAIL, exc.reason))
    else:
        checks = run_checks(env, data_dir, never_write, args.allow_synced)
    safe_write(render(checks), out)
    return 1 if has_failure(checks) else 0


def cmd_init(args, env, out, environ) -> int:
    service = _service(args, environ, out, create=True)
    with service:
        files = service.indexing_status()["files"]
    safe_write(f"Data folder ready: {service.layout.root}", out)
    safe_write(f"Index: {service.layout.index} ({files:,} files)", out)
    safe_write("Next: dugup roots add <folder>, then dugup index", out)
    return 0


def format_bytes(count: int) -> str:
    size = float(max(count, 0))
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"


def quote(text: str) -> str:
    """A pattern or path as it can be typed in PowerShell and in a Linux shell."""
    if text and all(ch.isalnum() or ch in "._-/\\:*?" for ch in text):
        return text
    return "'" + text.replace("'", "''") + "'"


def _find_root(service: LocalService, path: str) -> Optional[Root]:
    wanted = os.path.normcase(os.path.abspath(os.path.expanduser(path)))
    return next((r for r in service.list_roots() if os.path.normcase(r.path) == wanted), None)


def _change_excludes(service: LocalService, root: Root, patterns: List[str], undo: bool, out: TextIO) -> int:
    before = service.indexing_status()["files"]
    if undo:
        dropped = {p.casefold() for p in patterns}
        exclude = tuple(p for p in root.exclude if p.casefold() not in dropped)
    else:
        exclude = tuple(dict.fromkeys(list(root.exclude) + patterns))
    result = service.set_root(Root(root.path, root.policy, root.include, exclude))
    if isinstance(result, Failure):
        return _fail(result, out)
    left = before - service.indexing_status()["files"]
    safe_write(f"Folder: {result.path}", out)
    safe_write("  skipping: " + (", ".join(result.exclude) if result.exclude else "nothing"), out)
    if left:
        safe_write(f"  {left:,} file(s) that are now skipped left the index.", out)
    if undo:
        safe_write("Next: dugup index (to read what is no longer skipped)", out)
    return 0


def _size_text(count: float) -> str:
    """A size a person can compare at a glance: MB below 1,000 MB, GB above."""
    mb = count / 1048576
    if mb >= 1000:
        return f"{mb / 1024:,.1f} GB"
    return f"{mb:,.0f} MB" if mb >= 10 else f"{mb:,.1f} MB"


def print_survey(found, rows: int, out: TextIO) -> None:
    from .survey import CODE, DOCUMENTS, LEVELS, MEDIA, OTHER, PICTURES, TEXT
    shown = list(found.rows[:rows])
    rest = found.rows[rows:]
    name_width = min(38, max([10] + [len(r.folder) for r in shown]))

    def what(row, label):
        g = row.groups
        return (f"{shorten(label, name_width):<{name_width}} {row.files:>9,} {_size_text(row.bytes):>9} "
                f"{g[DOCUMENTS]:>8,} {g[TEXT]:>8,} {g[PICTURES]:>8,} {g[CODE]:>8,} {g[MEDIA]:>7,} {g[OTHER]:>8,} "
                f"{row.cloud_only:>8,} {row.not_worth:>9,}")

    def cost(row, label):
        return f"{shorten(label, name_width):<{name_width}} " + " ".join(f"{_size_text(c):>15}" for c in row.cost(found.rates))

    safe_write("", out)
    safe_write(f"What is in {found.root}", out)
    safe_write(f"{'folder':<{name_width}} {'files':>9} {'size':>9} {'docs':>8} {'text':>8} {'pictures':>8} {'code':>8} "
               f"{'media':>7} {'other':>8} {'cloud':>8} {'not worth':>9}", out)
    for row in shown:
        safe_write(what(row, row.folder), out)
    if rest:
        safe_write(f"  ... and {len(rest):,} smaller folder(s), included in the total. --rows {len(found.rows)} lists them all.", out)
    safe_write(what(found.total, "TOTAL"), out)
    safe_write("  docs: PDF and office files. cloud: files kept in the cloud, not on this machine; they are listed by "
               "name and never opened.", out)
    safe_write("  not worth: inside downloaded requirements and tool data (dugup roots suggest names them). "
               "Code and media are counted; DugUp does not read them yet.", out)
    safe_write("", out)
    safe_write("What the index would grow by, at each level (each includes the one before)", out)
    safe_write(f"{'folder':<{name_width}} " + " ".join(f"{level:>15}" for level in LEVELS), out)
    for row in shown:
        safe_write(cost(row, row.folder), out)
    safe_write(cost(found.total, "TOTAL"), out)
    basis = ("worked out from what your own index weighs for each file it has read" if found.rates.learned
             else "starting guesses (there is not enough of an index yet to learn from)")
    safe_write(f"  These are rough estimates, {basis}. Meaning search is not built yet; its column is for planning.", out)
    from .survey import HERE
    closer = next((r.folder for r in shown if r.folder != HERE), None)
    if closer is not None:
        safe_write(f"  To look closer at one folder: dugup roots survey {quote(os.path.join(found.root, closer))}", out)


def print_suggestions(found: List[Suggestion], path: str, is_root: bool, out: TextIO) -> None:
    if not found:
        safe_write("Nothing stands out as not worth indexing.", out)
        return
    titles = (("safe", "Safe to skip (downloaded requirements and tool data; nothing a person wrote):"),
              ("review", "Your call (often clutter, sometimes wanted):"))
    for group, title in titles:
        rows = [s for s in found if s.group == group]
        if not rows:
            continue
        safe_write(title, out)
        for s in rows:
            where = f"in {s.places:,} places, for example {s.example}" if s.places > 1 else (
                "" if s.example == s.pattern.lstrip("/") else f"at {s.example}")
            safe_write(f"  {quote(s.pattern)}", out)
            safe_write(f"      {s.reason}; {s.files:,} file(s), {format_bytes(s.bytes)}" + (f"; {where}" if where else ""), out)
            if s.readable and group != "safe":
                safe_write(f"      {s.readable:,} of these could be read and would be skipped too", out)
    total = sum(s.files for s in found if s.group == SAFE)
    safe_write("", out)
    if not is_root:
        safe_write(f"This folder is not indexed yet. Add it first: dugup roots add {quote(path)}", out)
    if any(s.group == SAFE for s in found):
        safe_write(f"To skip everything in the safe list ({total:,} files): dugup roots suggest {quote(path)} --apply-safe", out)
    safe_write(f"To skip anything else: dugup roots exclude {quote(path)} PATTERN [PATTERN ...]", out)
    safe_write("A bare name (Trash) is skipped wherever it appears. A path (a/b), or a name with a slash in front (/Trash), "
               "skips that one folder.", out)


def cmd_roots(args, env, out, environ) -> int:
    service = _service(args, environ, out, create=args.roots_command in ("add", "suggest", "survey", "copies"))
    if service is None:
        return 1
    with service:
        if args.roots_command == "add":
            exclude = list(args.exclude) + (list(SUGGESTED_EXCLUDES) if args.suggested_excludes else [])
            result = service.set_root(Root(args.path, args.policy, tuple(args.include), tuple(dict.fromkeys(exclude))))
            if isinstance(result, Failure):
                return _fail(result, out)
            safe_write(f"Folder: {result.path}", out)
            safe_write(f"  policy: {result.policy}", out)
            if result.include:
                safe_write(f"  only: {', '.join(result.include)}", out)
            if result.exclude:
                safe_write(f"  skipping: {', '.join(result.exclude)}", out)
            safe_write(f"Tip: dugup roots suggest {quote(result.path)} lists folders that may not be worth indexing "
                       "(downloaded requirements, trash, build output).", out)
            if len(service.list_roots()) == 1:
                space_hint(service.layout, service.settings, out)
            safe_write("Next: dugup index", out)
            return 0
        if args.roots_command == "level":
            result = service.set_level(args.path, None if args.level == "inherit" else args.level)
            if isinstance(result, Failure):
                return _fail(result, out)
            where = os.path.join(str(result["root"]), *str(result["folder"]).split("/")) if result["folder"] else result["root"]
            from .shapes import level_words, wants
            safe_write(f"{where}: {level_words(str(result['level']))}" + (" (follows the folder above)" if args.level == "inherit" else ""), out)
            if result["purged"]:
                safe_write(f"  The text kept for {result['purged']:,} file(s) was removed; their names stay searchable.", out)
            if result["queued"]:
                safe_write(f"  {result['queued']:,} file(s) now wait to be read. Next: dugup index", out)
            if wants(str(result["level"]), "words") and not wants(str(result["level"]), OPTION_OCR):
                safe_write("  Pictures and scanned pages here are no longer read (text already read from them is kept).", out)
            return 0
        if args.roots_command == "survey":
            safe_write(f"Looking through {args.path} (no file is opened, nothing is changed) ...", out)
            found = service.survey(args.path)
            if isinstance(found, Failure):
                return _fail(found, out)
            print_survey(found, max(1, args.rows), out)
            return 0
        if args.roots_command == "copies":
            from . import copies as finding
            safe_write(f"Looking through {args.path} (no file is opened, nothing is changed) ...", out)
            found = service.copies(args.path)
            if isinstance(found, Failure):
                return _fail(found, out)
            if not found:
                safe_write("No folders here look like copies of each other.", out)
                return 0
            groups = finding.families(found)
            extra = sum(g.files * (len(g.folders) - 1) for g in groups)
            capped = " or more" if len(found) >= finding.MOST_PAIRS else ""
            safe_write(f"{len(groups):,}{capped} set(s) of folders look like copies of each other (same file names and "
                       f"sizes; a guess, not proof). Roughly {extra:,} file(s) are extra copies.", out)
            for group in groups[:max(1, args.rows)]:
                backups = set(finding.named_backups(group.folders))
                if group.inside:
                    safe_write(f"  {len(group.folders)} places with {group.inside:,} folders inside that are copies of each other "
                               f"({group.files:,} file(s), each about {group.share:.0%} the same or better)", out)
                else:
                    safe_write(f"  {len(group.folders)} places, {group.files:,} file(s) or more in each, about {group.share:.0%} the same or better", out)
                for folder in group.folders:
                    mark = "   <- named like a backup: the picker would leave it out" if folder in backups else ""
                    safe_write(f"      {os.path.join(args.path, *folder.split('/'))}{mark}", out)
            if len(groups) > args.rows:
                safe_write(f"  ... and {len(groups) - max(1, args.rows):,} more set(s); --rows N shows more", out)
            return 0
        if args.roots_command == "suggest":
            safe_write(f"Looking through {args.path}" + ("" if args.apply_safe else " (nothing is changed)") + " ...", out)
            found = service.suggest_excludes(args.path)
            if isinstance(found, Failure):
                return _fail(found, out)
            root = _find_root(service, args.path)
            if args.apply_safe:
                if root is None:
                    safe_write(f"That folder is not indexed yet. Add it first: dugup roots add {quote(args.path)}", out)
                    return 1
                patterns = [s.pattern for s in found if s.group == SAFE]
                if not patterns:
                    safe_write("Nothing in the safe list to skip.", out)
                    return 0
                return _change_excludes(service, root, patterns, False, out)
            print_suggestions(found, root.path if root else args.path, root is not None, out)
            return 0
        if args.roots_command == "exclude":
            root = _find_root(service, args.path)
            if root is None:
                safe_write(f"Not one of the indexed folders: {args.path}. See: dugup roots list", out)
                return 1
            return _change_excludes(service, root, list(args.patterns), args.undo, out)
        if args.roots_command == "remove":
            result = service.remove_root(args.path, purge=not args.keep_index)
            if isinstance(result, Failure):
                return _fail(result, out)
            safe_write(f"Folder removed. {result:,} file(s) left the index.", out)
            return 0
        roots = service.list_roots()
        if not roots:
            safe_write("No folders yet. Add one with: dugup roots add <folder>", out)
            return 0
        for root in roots:
            _print_root(root, service.indexing_status(), out)
        return 0


def _print_root(root: Root, status, out: TextIO) -> None:
    counts = next((r["counts"] for r in status["roots"] if r["path"] == root.path), {})
    total = sum(counts.values())
    state = "" if root.reachable else "  [NOT AVAILABLE at the last scan; its files are kept]"
    safe_write(f"{root.path}{state}", out)
    safe_write(f"  policy {root.policy}; {total:,} file(s); last scan {format_date(root.last_scan)}", out)
    from .shapes import level_words
    for folder, level in root.levels:
        safe_write(f"  {folder or '(the whole folder)'}: {level_words(level)}", out)
    if root.exclude:
        safe_write(f"  skipping: {', '.join(root.exclude)}", out)
    if root.include:
        safe_write(f"  only: {', '.join(root.include)}", out)
    for code in root.warnings:
        safe_write(f"  WARNING: {WARNING_TEXT.get(code, code)}", out)


def print_report(report: IndexReport, out: TextIO, retention_days: float) -> None:
    for root in report.roots:
        safe_write(f"Folder: {root.path}", out)
        if not root.reachable:
            safe_write(f"  NOT AVAILABLE right now ({root.detail}). Nothing was changed; its files are kept.", out)
            continue
        safe_write(f"  new {root.added:,}, changed {root.changed:,}, moved {root.moved:,}, gone {root.missing:,}, "
                   f"back {root.restored:,}, removed after {retention_days:g} days missing {root.purged:,}", out)
        for code in root.warnings:
            safe_write(f"  WARNING: {WARNING_TEXT.get(code, code)} ({root.held_back:,} file(s) held back). "
                       "If the files are really gone, they will be marked missing once the folder looks normal again.", out)
        if root.skipped:
            safe_write(f"  {len(root.skipped):,} folder(s) or file(s) could not be read; what was known below them is kept:", out)
            for item in root.skipped[:5]:
                safe_write(f"    {item.path} ({item.reason})", out)
            if len(root.skipped) > 5:
                safe_write(f"    ... and {len(root.skipped) - 5:,} more", out)
    safe_write(f"Read {report.read:,} file(s) in {format_seconds(report.seconds)} ({report.rate:.1f} files/s): "
               f"{report.done:,} done, {report.partial:,} partial, {report.failed:,} failed, {report.skipped:,} skipped", out)
    if report.second_tries:
        safe_write(f"{report.second_tries:,} file(s) ran out of time and were read again, last, with more time.", out)
    if report.copied:
        safe_write(f"{report.copied:,} of the file(s) read hold the same content as a file read before: their text was taken "
                   "from that file, and they were not read again.", out)
    if report.marked:
        safe_write(f"{report.marked:,} file(s) read earlier were given a content mark, so the same file in several "
                   "places is now known for one file.", out)
    if report.vanished:
        safe_write(f"{report.vanished:,} file(s) disappeared before they could be read; the next run looks again.", out)
    if report.read >= 100 and report.skipped > report.read / 2:
        safe_write("More than half of the files read are of types that cannot be read. `dugup roots suggest <folder>` "
                   "shows which folders hold them.", out)
    if report.partial or report.failed:
        safe_write("See what failed, was read in part or waits for OCR: dugup status", out)
    if report.partial:
        safe_write("To read the words in pictures and scanned pages: dugup ocr", out)


def _file_log(layout: DataLayout) -> Optional[logging.Handler]:
    """Counts and reasons only go to the log; never document text, never file names."""
    try:
        layout.logs.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(layout.logs / "dugup.log", maxBytes=1_000_000,
                                                       backupCount=3, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger = logging.getLogger("dugup")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def cmd_index(args, env, out, environ, stop: Optional[threading.Event] = None, install_signals: bool = True) -> int:
    service = _service(args, environ, out, create=True)
    stop = stop or threading.Event()
    printer = ProgressPrinter(out)
    handler = _file_log(service.layout)
    previous = None

    def on_interrupt(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt   # the second Ctrl+C: stop at once
        stop.set()
        printer.finish()
        safe_write("Stopping after the current file. Press Ctrl+C again to stop at once.", out)

    if install_signals and threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, on_interrupt)
    try:
        with service:
            if not service.list_roots():
                safe_write("No folders to index yet. Add one with: dugup roots add <folder>", out)
                return 1
            roots = None
            if args.root:
                wanted = {os.path.normcase(os.path.abspath(r)) for r in args.root}
                roots = [r.path for r in service.list_roots() if os.path.normcase(r.path) in wanted]
                if len(roots) != len(wanted):
                    safe_write("One of the --root folders is not an indexed folder. See: dugup roots list", out)
                    return 1
            choice = REREAD if args.reread else KEEP if args.keep else REFUSE
            report = service.run_indexing(stop, printer, roots, choice, args.retry_failed)
            printer.finish()
            if isinstance(report, Failure):
                return _fail(report, out)
            if report.settings_changed:
                safe_write("The settings that shape the index have changed since it was built: "
                           + ", ".join(report.settings_changed), out)
                safe_write("Nothing was done. Choose one:", out)
                safe_write("  dugup index --reread   read every file again with the new settings", out)
                safe_write("  dugup index --keep     keep what is stored; only new and changed files use the new settings", out)
                return 1
            print_report(report, out, service.settings.missing_retention_days)
            print_compacted(report.compacted, out)
            if report.paused:
                safe_write(f"Indexing paused itself: {report.paused}.", out)
                safe_write("Run `dugup index` again to go on; `dugup index --retry-failed` also retries what failed.", out)
                return 1
            if report.stopped:
                safe_write("[Interrupted] Stopped cleanly. Run `dugup index` again to continue where it stopped.", out)
                return 130
            code = 0
            if not args.no_ocr and service.settings.ocr_after_index and service.ocr_waiting():
                code = _index_then_ocr(service, args, roots, stop, printer, out)
                if code == 130:
                    return code
            if args.no_meaning or not service.settings.meaning_after_index or not service.meaning_waiting():
                return _index_then_looks(service, args, stop, printer, out) or code
            ready = service.meaning_ready()
            if ready is not True:
                safe_write(f"Sketches of meaning were not made: {ready}.", out)
                return _index_then_looks(service, args, stop, printer, out) or code
            safe_write(f"Now sketching the meaning of {service.meaning_waiting():,} file(s), the most recently changed "
                       "first. Ctrl+C stops; what is done is kept and `dugup meaning fill` continues later. "
                       "(`dugup index --no-meaning` leaves this step out.)", out)
            report = service.run_meaning(stop, printer)
            printer.finish()
            if isinstance(report, Failure):
                return _fail(report, out)
            code = print_meaning_report(report, out) or code
            if code == 130:
                return code
            return _index_then_looks(service, args, stop, printer, out) or code
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if handler is not None:
            logging.getLogger("dugup").removeHandler(handler)
            handler.close()


def _index_then_looks(service, args, stop, printer, out) -> int:
    """After an update: look at new pictures, when the setting asks and the model is already there."""
    if getattr(args, "no_looks", False) or not service.settings.looks_after_index or not service.looks_waiting():
        return 0
    ready = service.looks_ready()
    if ready is not True:
        safe_write(f"Pictures were not sketched: {ready}.", out)
        return 0
    safe_write(f"Now looking at {service.looks_waiting():,} picture(s), the most recently changed first, "
               f"{service.looks_readers()} reader(s) side by side. Ctrl+C stops; what is done is kept and `dugup alike fill` "
               "continues later. (`dugup index --no-looks` leaves this step out.)", out)
    report = service.run_looks(stop, printer)
    printer.finish()
    if isinstance(report, Failure):
        return _fail(report, out)
    return print_looks_report(report, out)


def _index_then_ocr(service, args, roots, stop, printer, out) -> int:
    waiting = service.ocr_waiting()
    # the same run goes on to the pictures and scans the folders ask for; Ctrl+C stops it as before
    threshold = service.settings.ocr_fast_pages
    way = (f" Fast, with several readers, since {waiting:,} pages is a lot: the computer will be busier until this ends."
           if threshold > 0 and waiting >= threshold else "")
    safe_write(f"Now reading {waiting:,} picture(s) and scanned page(s) (OCR).{way} Ctrl+C stops; what is done is kept "
               "and `dugup ocr` continues later. (`dugup index --no-ocr` leaves this step out.)", out)
    pages = service.run_ocr(stop, printer, roots)
    printer.finish()
    if isinstance(pages, Failure):
        return _fail(pages, out)
    print_ocr_report(pages, out)
    print_compacted(pages.compacted, out)
    if pages.stopped:
        safe_write("[Interrupted] Stopped cleanly. The index is up to date; run `dugup ocr` to read the rest.", out)
        return 130
    return 1 if pages.paused else 0


def _chosen_roots(service: LocalService, asked: Optional[List[str]], out: TextIO) -> "Optional[List[str]] | int":
    if not asked:
        return None
    wanted = {os.path.normcase(os.path.abspath(r)) for r in asked}
    roots = [r.path for r in service.list_roots() if os.path.normcase(r.path) in wanted]
    if len(roots) != len(wanted):
        safe_write("One of the --root folders is not an indexed folder. See: dugup roots list", out)
        return 1
    return roots


def print_compacted(result, out: TextIO) -> None:
    if result:
        safe_write(f"The index was compacted: {result['before'] / 1048576:,.0f} MB before, "
                   f"{result['after'] / 1048576:,.0f} MB after.", out)


def print_ocr_report(report, out: TextIO) -> None:
    for path in report.away:
        safe_write(f"Folder NOT AVAILABLE right now, left alone: {path}", out)
    if report.requeued:
        safe_write(f"{report.requeued:,} file(s) had pages read by an older OCR version; those pages were queued again.", out)
    if report.total == 0 and report.not_ready is None and not report.paused:
        safe_write("Nothing waits for OCR.", out)
        return
    safe_write(f"OCR read {report.read:,} of {report.total:,} waiting page(s) in {format_seconds(report.seconds)} "
               f"({report.rate * 60:.1f} pages/min): {report.with_words:,} with words, "
               f"{report.read - report.with_words:,} with none, {report.failed:,} failed"
               + (f" [{report.readers} readers side by side]" if getattr(report, "readers", 1) > 1 else ""), out)
    if getattr(report, "chose_fast", 0):
        safe_write(f"Several readers were used because {report.chose_fast:,} page(s) waited (the setting ocr_fast_pages); "
                   "`dugup ocr --gentle` reads one page at a time.", out)
    if getattr(report, "copied", 0):
        safe_write(f"{report.copied:,} of those page(s) belong to files that hold the same content as another file: "
                   "each such page was read once and the result used for every copy.", out)
    if report.second_tries:
        safe_write(f"{report.second_tries:,} page(s) ran out of time and were read again, last, with more time.", out)
    device = getattr(report, "device", "")
    if getattr(report, "gpu_asked", False) and device and not device.startswith("graphics card"):
        safe_write("A graphics card was asked for, but the OCR runtime installed here cannot use one, so the processor "
                   "was used. `dugup doctor` says what to install.", out)
    elif device.startswith("graphics card"):
        safe_write(f"Read on the {device}.", out)
    if report.left:
        safe_write(f"{report.left:,} page(s) belong to files that could not be reached; they stay queued.", out)
    if report.held_back:
        safe_write(f"{report.held_back:,} page(s) are in folders whose level stops at words; they were left alone "
                   "(dugup roots level <folder> ocr changes that).", out)
    if report.failed:
        safe_write("See what failed: dugup status --attention    Try those again: dugup ocr --retry-failed", out)
    if report.not_ready is not None:
        safe_write(f"OCR cannot run ({report.not_ready.reason.value.replace('_', ' ')}): {report.not_ready.detail}", out)
        safe_write("Nothing was marked as failed; the pages stay queued.", out)
    if report.paused:
        safe_write(f"OCR paused itself: {report.paused}.", out)
        safe_write("The rest stays queued. Run `dugup ocr` to go on, or `dugup ocr --retry-failed` to also retry the failed pages.", out)
    if report.out_of_time:
        safe_write(f"The time given is used up; {report.remaining:,} page(s) still wait. Run `dugup ocr` again to continue.", out)


def cmd_ocr(args, env, out, environ, stop: Optional[threading.Event] = None, install_signals: bool = True) -> int:
    service = _service(args, environ, out, create=False)
    if service is None:
        return 1
    stop = stop or threading.Event()
    printer = ProgressPrinter(out)
    handler = _file_log(service.layout)
    previous = None

    def on_interrupt(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt   # the second Ctrl+C: stop at once
        stop.set()
        printer.finish()
        safe_write("Stopping. The page being read is not lost: it stays queued. Press Ctrl+C again to stop at once.", out)

    if install_signals and threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, on_interrupt)
    try:
        with service:
            roots = _chosen_roots(service, args.root, out)
            if isinstance(roots, int):
                return roots
            if args.minutes is not None and args.minutes <= 0:
                safe_write("--minutes must be above 0", out)
                return 1
            budget = None if args.minutes is None else args.minutes * 60.0
            if args.again and not args.folder:
                safe_write("--again needs --folder: say which folder to read afresh", out)
                return 1
            if args.fast or args.readers:
                on_card = service.settings.ocr_gpu if args.gpu is None else bool(args.gpu)
                if args.readers:        # the number asked for applies to whichever kind of reader this run uses
                    service.settings = replace(service.settings, ocr_fast_readers=max(1, args.readers),
                                               ocr_gpu_readers=max(1, args.readers))
                count = service.card_readers() if on_card else service.fast_readers()
                safe_write(f"Fast: {count} reader(s) side by side" + (", sharing the graphics card" if on_card else "")
                           + ". The computer will be busier until this ends.", out)
            report = service.run_ocr(stop, printer, roots, budget, args.retry_failed, args.reread_old,
                                     folder=args.folder, fast=False if args.gentle else (True if (args.fast or args.readers) else None),
                                     again=args.again,
                                     gpu=args.gpu)
            printer.finish()
            if isinstance(report, Failure):
                return _fail(report, out)
            print_ocr_report(report, out)
            print_compacted(report.compacted, out)
            if report.stopped:
                safe_write("[Interrupted] Stopped cleanly. Run `dugup ocr` again to continue where it stopped.", out)
                return 130
            return 1 if report.not_ready is not None or report.paused else 0
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if handler is not None:
            logging.getLogger("dugup").removeHandler(handler)
            handler.close()


def _flags(row: SearchRow, retention_days: float) -> List[str]:
    notes = []
    if row.missing_since is not None:
        notes.append(MISSING_NOTE.format(date=format_date(row.missing_since + retention_days * 86400.0)[:10]))
    if row.unavailable:
        notes.append(UNAVAILABLE_NOTE)
    return notes


ALSO_AT_SHOWN = 8


def print_results(page: SearchPage, query: str, service: LocalService, context: int, out: TextIO,
                  by_meaning: bool = False, both: bool = False, looks: bool = False) -> None:
    if page.total == 0:
        safe_write(f"No files found for: {query}", out)
        return
    first = page.offset + 1 if page.rows else 0
    last = page.offset + len(page.rows)
    way = ("that look most like" if looks else "closest in words and meaning to" if both
           else "closest in meaning to" if by_meaning else "for")
    safe_write(f"Showing {first:,} to {last:,} of {page.total:,} file(s) {way}: {query}", out)
    for number, row in enumerate(page.rows, first):
        state = "" if row.state == "done" else f"  [{row.state}]"
        score = f"  ({row.rank:.2f})" if by_meaning or looks else ""
        safe_write(f"{number:>3}. {row.name}{state}{score}", out)
        safe_write(f"     {row.folder}", out)
        for note in _flags(row, service.settings.missing_retention_days):
            safe_write(f"     ! {note}", out)
        if row.also_at:
            safe_write(f"     the same file is also at ({len(row.also_at):,} more place(s)):", out)
            for place in row.also_at[:ALSO_AT_SHOWN]:
                safe_write(f"       {place}", out)
            if len(row.also_at) > ALSO_AT_SHOWN:
                safe_write(f"       ... and {len(row.also_at) - ALSO_AT_SHOWN:,} more (--every-copy lists every one)", out)
        shown_lines: List[str] = []
        for evidence in row.evidence:
            passage = service.get_evidence(evidence, context)
            if isinstance(passage, Failure):
                continue
            if passage.kind.value == "name":
                where = "name"
            elif passage.kind.value == "ocr":     # words read from pixels: say so, they can be misread
                where = "in the picture" if row.kind == "image" else f"p.{passage.page}, read by OCR"
            else:
                where = f"p.{passage.page}" if passage.page else "text"
            before, match, after = passage.text[:passage.start], passage.text[passage.start:passage.end], passage.text[passage.end:]
            if passage.start == passage.end:       # nothing in particular to mark (a passage found by meaning)
                shown = one_line(passage.text)
            else:
                shown = one_line(before) + (" " if before[-1:].isspace() else "") + "[" + one_line(match) + "]" \
                    + (" " if after[:1].isspace() else "") + one_line(after)
            line = f"{where}: {shown.strip()}"
            # overlapping pieces of text can hold the same passage twice: show it once, in its longer form
            core = line.split(": ", 1)[1]
            twin = next((i for i, old in enumerate(shown_lines)
                         if core in old or old.split(": ", 1)[1] in core), None)
            if twin is None:
                shown_lines.append(line)
            elif len(line) > len(shown_lines[twin]):
                shown_lines[twin] = line
        for line in shown_lines:
            safe_write(f"     {line}", out)
    if page.truncated:
        safe_write(f"More: add --offset {last} (showing {len(page.rows):,} of {page.total:,})", out)


def cmd_search(args, env, out, environ) -> int:
    service = _service(args, environ, out, create=False)
    if service is None:
        return 1
    query = " ".join(args.words)
    with service:
        filters = (SearchFilters(exact=args.exact, every_copy=args.every_copy, present_only=args.here)
                   if (args.exact or args.every_copy or args.here) else None)
        if args.meaning or args.both or args.looks:
            filters = replace(filters or SearchFilters(), where=WHERE_LOOKS if args.looks else WHERE_BOTH if args.both else WHERE_MEANING)
        page = service.search(query, max(0, args.limit), max(0, args.offset), args.sort, args.desc, filters)
        if isinstance(page, Failure):
            return _fail(page, out)
        print_results(page, query, service, max(0, args.context), out, by_meaning=args.meaning or args.both, both=args.both,
                      looks=args.looks)
        if args.looks:
            done, could, _ = service.looks_coverage()
            if could > done:
                safe_write(f"(Sketches cover {done:,} of {could:,} picture(s) so far; `dugup alike fill` continues.)", out)
        if args.both:
            from .store import parse_query
            telling = list(service.store.telling_words(query, service.settings.meaning_word_everywhere))
            aside = [t for t in parse_query(query) if t not in telling]
            if aside:
                safe_write("(Words found nearly everywhere in the index were not looked for: " + ", ".join(aside)
                           + (". Looked for: " + ", ".join(telling) if telling else ". The meaning alone was used") + ".)", out)
        if args.meaning or args.both:
            done, could = service.meaning_coverage()
            if could > done:
                safe_write(f"(Sketches of meaning cover {done:,} of {could:,} file(s) with text so far; "
                           "`dugup meaning fill` continues.)", out)
    return 0


def save_lines(target: str, lines, args, environ) -> str:
    """Write lines as UTF-8 (with a mark, so spreadsheets read it). Returns "" when done, else why not. Never into
    a folder the program must not write to or one that looks cloud-synced, and never over a file that is there."""
    from .datadir import check_allowed
    path = os.path.abspath(os.path.expanduser(target))
    try:
        check_allowed(path, list(args.never_write) + never_write_from_env(environ), args.allow_synced)
    except DataDirError as exc:
        return f"The list was not written: {path} is {exc}."
    if os.path.exists(path):
        return f"The list was not written: {path} is there already. Give another name."
    try:
        with open(path, "x", encoding="utf-8-sig", newline="\r\n" if os.name == "nt" else "\n") as handle:
            for line in lines:
                handle.write(line + "\n")
    except OSError as exc:
        return f"The list was not written: {type(exc).__name__}: {exc}"
    return ""


def save_file_list(target: str, records, args, environ) -> str:
    """Write "path<TAB>reason" lines as UTF-8. Returns "" when done, else why not. Never into a folder the
    program must not write to or one that looks cloud-synced, and never over a file that is there already."""
    from .datadir import check_allowed
    path = os.path.abspath(os.path.expanduser(target))
    try:
        check_allowed(path, list(args.never_write) + never_write_from_env(environ), args.allow_synced)
    except DataDirError as exc:
        return f"The list was not written: {path} is {exc}."
    if os.path.exists(path):
        return f"The list was not written: {path} is there already. Give another name."
    try:
        with open(path, "x", encoding="utf-8-sig", newline="\r\n" if os.name == "nt" else "\n") as handle:
            for record in records:
                handle.write(f"{record.path}\t{' '.join(str(record.error).split())}\n")
    except OSError as exc:
        return f"The list was not written: {type(exc).__name__}: {exc}"
    return ""


def cmd_status(args, env, out, environ) -> int:
    service = _service(args, environ, out, create=False)
    if service is None:
        return 1
    with service:
        status = service.indexing_status()
        counts = status["counts"]
        safe_write(f"Data folder: {service.layout.root}", out)
        safe_write(f"{status['files']:,} file(s), {status['segments']:,} piece(s) of text", out)
        order = ["done", "partial", "pending", "skipped", "failed", "missing"]
        parts = [f"{name} {counts[name]:,}" for name in order if counts.get(name)]
        parts += [f"{name} {count:,}" for name, count in sorted(counts.items()) if name not in order]
        safe_write("States: " + (", ".join(parts) if parts else "nothing indexed yet"), out)
        if counts.get("pending"):
            safe_write(f"{counts['pending']:,} file(s) wait to be read. Run: dugup index", out)
        for root in service.list_roots():
            _print_root(root, status, out)
        if not status["roots"]:
            safe_write("No folders yet. Add one with: dugup roots add <folder>", out)
        tally = service.attention_counts()
        if tally["waiting_for_ocr"]:
            safe_write(f"{tally['waiting_for_ocr']:,} file(s) wait for OCR (pictures and scanned pages). "
                       "Their names are searchable already. To read the words in them: dugup ocr", out)
        for engine, count in sorted(service.old_ocr_pages().items()):
            safe_write(f"{count:,} page(s) were read by another OCR version ({engine or 'unknown'}). "
                       "To read them again with the one installed now: dugup ocr --reread-old", out)
        marked, sets, extra = service.store.copy_counts()
        if extra:
            safe_write(f"{extra:,} file(s) are an extra copy of another ({sets:,} file(s) kept in two or more places). "
                       "A search shows each once, with its other places.", out)
        waiting_marks = len(service.store.unmarked())
        if waiting_marks and service.settings.content_marks:
            safe_write(f"{waiting_marks:,} file(s) read earlier have no content mark yet, so copies among them are not "
                       "known. The next `dugup index` marks them (each is read once more; Ctrl+C stops and it goes on later).", out)
        if args.unreadable or args.save:
            records = service.unreadable_files()
            if args.save:
                problem = save_file_list(args.save, records, args, environ)
                if problem:
                    safe_write(problem, out)
                    return 1
                safe_write(f"{len(records):,} file(s) that cannot be read are listed in {args.save} (path, a tab, the reason).", out)
            else:
                safe_write(f"Cannot be read (damaged, or needs a password): {len(records):,} file(s). Their names are "
                           "searchable. DugUp never deletes or changes a file.", out)
                for record in records:
                    safe_write(f"  {record.path}", out)
                    safe_write(f"      {record.error}", out)
        if args.attention is not None:
            records = service.needs_attention(limit=max(0, args.attention))
            safe_write(f"Worth a look: showing {len(records):,} of {tally['attention']:,}", out)
            for record in records:
                safe_write(f"  [{record.state.value}] {record.path}", out)
                safe_write(f"      {record.error}", out)
            if tally["unreadable"] and not (args.unreadable or args.save):
                shown = service.unreadable_files(limit=max(0, args.attention))
                safe_write(f"Cannot be read (damaged, or needs a password; nothing to do here): showing {len(shown):,} of "
                           f"{tally['unreadable']:,}. All of them: dugup status --unreadable", out)
                for record in shown:
                    safe_write(f"  [{record.state.value}] {record.path}", out)
                    safe_write(f"      {record.error}", out)
        elif tally["attention"]:
            safe_write(f"{tally['attention']:,} file(s) are worth a look (a limit, a timeout or a crash). List them: dugup status --attention", out)
        if tally["unreadable"] and not (args.unreadable or args.save) and args.attention is None:
            safe_write(f"{tally['unreadable']:,} file(s) cannot be read (damaged, or need a password); nothing to do here. "
                       "List them: dugup status --unreadable    Save the list: dugup status --save FILE", out)
    return 0


def megabytes(count: Optional[int]) -> str:
    return "unknown" if count is None else f"{count / (1024 * 1024):,.0f} MB"


def suggested_limit_mb(free_bytes: Optional[int]) -> Optional[int]:
    """A starting point for the spending limit: a tenth of what is free now, and at least 500 MB."""
    if free_bytes is None:
        return None
    return max(500, int(free_bytes / (1024 * 1024) / 10))


def space_hint(layout: DataLayout, settings, out: TextIO) -> None:
    """Said once a folder is added: what is free, what the index may use, and how to cap it."""
    free = layout.free_bytes()
    if settings.max_index_mb:
        safe_write(f"Disk: {megabytes(free)} free; the index may use up to {settings.max_index_mb:,} MB (dugup space).", out)
        return
    suggestion = suggested_limit_mb(free)
    line = f"Disk: {megabytes(free)} free where the index is kept. No spending limit is set"
    if settings.min_free_mb:
        line += f" (a run stops by itself when less than {settings.min_free_mb:,} MB is free)"
    safe_write(line + ".", out)
    if suggestion is not None:
        safe_write(f"To cap what the index may use, for example at a tenth of the free space: dugup space --limit {suggestion}", out)
        safe_write("(Tidying the index now and then needs as much space again for a moment, so leave twice the limit free.)", out)


def set_config_value(config, key: str, value) -> None:
    """Write one setting into config.toml, leaving everything else in the file as it is."""
    catalogue.write(config, {key: value})


def cmd_config(args, env, out, environ) -> int:
    """Show and change settings. Every setting is described once, in catalogue.py; this only prints that."""
    layout = _layout(args, environ, create=args.config_command in ("set", "reset"))
    settings, notes = settings_module.load(layout.config)
    for note in notes:
        safe_write(f"[config] {note}", out)
    command = args.config_command
    if command == "list":
        safe_write(f"Settings are kept in {layout.config}" + ("" if layout.config.exists() else " (not there yet: every setting is at its default)"), out)
        for group in catalogue.GROUPS:
            entries = catalogue.in_group(group.key, everyday_only=not args.all)
            if not entries:
                continue
            safe_write("", out)
            safe_write(group.title, out)
            safe_write("  " + group.about, out)
            for entry in entries:
                value = getattr(settings, entry.name)
                mark = "" if value == catalogue.default(entry.name) else "   [changed]"
                safe_write(f"  {entry.name} = {catalogue.show(entry.name, value)}{mark}", out)
                safe_write(f"      {entry.label}. {entry.help}", out)
        safe_write("", out)
        if not args.all:
            hidden = len(catalogue.ENTRIES) - sum(1 for e in catalogue.ENTRIES.values() if e.everyday)
            safe_write(f"These are the everyday settings. {hidden} more: dugup config list --all", out)
        safe_write("To change one: dugup config set NAME VALUE     To go back to the default: dugup config reset NAME", out)
        return 0
    entry = catalogue.ENTRIES.get(args.name)
    if entry is None:
        return _fail(Failure(FailureReason.UNSUPPORTED,
                             f"there is no setting called {args.name!r}. `dugup config list --all` shows them all"), out)
    if command == "get":
        value = getattr(settings, entry.name)
        safe_write(f"{entry.name} = {catalogue.show(entry.name, value)}"
                   + ("" if value == catalogue.default(entry.name)
                      else f"   (the default is {catalogue.show(entry.name, catalogue.default(entry.name))})"), out)
        safe_write(f"  {entry.label}. {entry.help}", out)
        if entry.why:
            safe_write(f"  Why it matters: {entry.why}", out)
        if entry.choices:
            safe_write("  Choices: " + "; ".join(
                f"{'yes' if v is True else 'no' if v is False else (v if v != '' else chr(34) * 2)} ({words})"
                for v, words in entry.choices), out)
        return 0
    if command == "reset":
        catalogue.write(layout.config, {entry.name: None})
        safe_write(f"{entry.name} is back at its default: {catalogue.show(entry.name, catalogue.default(entry.name))}", out)
        return 0
    changed = catalogue.apply(settings, {entry.name: " ".join(args.value)})
    if isinstance(changed, Failure):
        return _fail(changed, out)
    value = getattr(changed, entry.name)
    catalogue.write(layout.config, {entry.name: value})
    safe_write(f"{entry.name} = {catalogue.show(entry.name, value)}   (saved in {layout.config})", out)
    if entry.name in Settings().fingerprint():
        safe_write("  This setting shapes what is stored. The next `dugup index` will ask whether to read every file again.", out)
    return 0


def cmd_space(args, env, out, environ) -> int:
    service = _service(args, environ, out, create=args.limit is not None)
    if service is None:
        return 1
    with service:
        layout = service.layout
        limit = service.settings.max_index_mb
        if args.limit is not None:
            if args.limit < 0:
                safe_write("--limit must be 0 (no limit) or a number of megabytes", out)
                return 1
            set_config_value(layout.config, "max_index_mb", args.limit)
            limit = args.limit
            safe_write(f"Saved in {layout.config}", out)
        if args.compact:
            safe_write("Compacting the index (this rewrites the whole file; it can take a minute) ...", out)
            result = service.compact()
            if isinstance(result, Failure):
                return _fail(result, out)
            saved = result["before"] - result["after"]
            safe_write(f"Compacted: {megabytes(result['before'])} before, {megabytes(result['after'])} after"
                       + (f" ({megabytes(saved)} given back)." if saved > 0 else " (there was nothing to give back)."), out)
        used = layout.index_bytes()
        free = layout.free_bytes()
        safe_write(f"Data folder: {layout.root}", out)
        safe_write(f"The index uses {megabytes(used)}. Free on that drive: {megabytes(free)}.", out)
        if limit:
            share = 100.0 * used / (limit * 1024 * 1024)
            safe_write(f"Spending limit: {limit:,} MB ({share:.0f}% used). Indexing and OCR stop when it is reached; "
                       "searching goes on working.", out)
        else:
            safe_write("Spending limit: none.", out)
            suggestion = suggested_limit_mb(free)
            if suggestion is not None:
                safe_write(f"To set one, for example a tenth of the free space: dugup space --limit {suggestion}", out)
        safe_write(f"Compacting writes a full copy first, so for a moment the index needs twice its size "
                   f"(now about {megabytes(2 * used)}). When there is no room for that, compacting is skipped; nothing else is affected.", out)
        if service.settings.min_free_mb:
            safe_write(f"Whatever the limit, a run stops when less than {service.settings.min_free_mb:,} MB is free on the drive.", out)
        files = service.indexing_status()["files"]
        if files and used:
            safe_write(f"So far that is about {used / files / 1024:,.1f} KB of index for each of the {files:,} file(s).", out)
    return 0


def cmd_models(args, env, out, environ, stop: Optional[threading.Event] = None, install_signals: bool = True) -> int:
    service = _service(args, environ, out, create=args.models_command == "install" and args.yes)
    if service is None:
        return 1
    with service:
        installed = service.models_installed()
        if args.models_command == "remove":
            freed = service.remove_models()
            safe_write(f"Removed. {megabytes(freed)} given back." if freed else "Nothing was installed.", out)
            return 0
        plan = service.models_plan(getattr(args, "model", "small"))
        if args.models_command == "status":
            if installed:
                safe_write(f"Installed: runtime {installed.get('runtime')}, model {installed.get('model')}", out)
                safe_write(f"  in {service.models_folder}", out)
                reader = service.settings.intent_reader
                safe_write(f'Sentences are read by: {"the model" if reader == "model" else "the built-in rules"} '
                           f'(intent_reader = "{reader}" in config.toml). To compare: dugup ask --reader model "..."', out)
            else:
                safe_write("Plain-words search is not installed.", out)
            if service.settings.intent_url or service.settings.intent_server:
                safe_write("config.toml names its own model server, which is used instead of an installed one.", out)
            if not installed:
                safe_write(plan.detail if isinstance(plan, Failure) else plan.describe(), out)
                if not isinstance(plan, Failure):
                    safe_write("To install: dugup models install --yes", out)
                    safe_write("There is also a larger model that reads sentences better and is slower (2.2 GB): "
                               "dugup models install --model larger", out)
            return 0
        if isinstance(plan, Failure):
            return _fail(plan, out)
        safe_write(plan.describe(), out)
        if not args.yes:
            more = " --model larger" if getattr(args, "model", "small") == "larger" else ""
            safe_write(f"Nothing was downloaded. To go ahead: dugup models install{more} --yes", out)
            return 1
        stop = stop or threading.Event()
        printer = ProgressPrinter(out)
        previous = None

        def on_interrupt(signum, frame):
            if stop.is_set():
                raise KeyboardInterrupt
            stop.set()
            printer.finish()
            safe_write("Stopping. What was downloaded is kept. Press Ctrl+C again to stop at once.", out)

        if install_signals and threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGINT, on_interrupt)
        try:
            result = service.install_models(stop, printer, choice=getattr(args, "model", "small"))
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
        printer.finish()
        if isinstance(result, Failure):
            if result.reason.value == "stopped":
                safe_write("[Interrupted] Stopped. Run `dugup models install --yes` again to continue.", out)
                return 130
            return _fail(result, out)
        safe_write("Installed. Try it: dugup ask --reader model \"pdfs about budgets from last month\"", out)
        if service.settings.intent_reader != "model":
            safe_write('The window keeps using the built-in rules until config.toml says intent_reader = "model".', out)
        return 0


def cmd_ask(args, env, out, environ) -> int:
    service = _service(args, environ, out, create=False)
    if service is None:
        return 1
    sentence = " ".join(args.sentence)
    with service:
        own = args.reader is not None
        reader = service.reader_named(args.reader) if own else service.intent
        if reader is None:
            return _fail(Failure(FailureReason.NOT_INSTALLED,
                                 "there is no model to read with. To fetch one: dugup models install"), out)
        try:
            folders = [root.path for root in service.list_roots()]
            started = time.monotonic()
            answer = reader.understand(sentence, folders)
            took = time.monotonic() - started
        finally:
            if own:
                reader.close()
        if isinstance(answer, Failure):
            return _fail(answer, out)
        safe_write(answer.describe(), out)
        for note in answer.notes:
            safe_write(f"  note: {note}", out)
        safe_write(f"Read in {took:.1f} s (the first sentence after a start includes loading the model).", out)
    return 0


def cmd_window(args, env, out, environ) -> int:
    try:
        import tkinter
    except ImportError:
        safe_write("The window needs tkinter, which this Python does not have. On Windows it comes with Python "
                   "(tick 'tcl/tk' in the installer); on Linux install your system's python3-tk package.", out)
        return 1
    from .window import App
    try:
        try:
            root = tkinter.Tk()
        except tkinter.TclError:
            root = tkinter.Tk()      # starting tkinter fails now and then on Windows while it reads its own files
    except tkinter.TclError as exc:
        first_line = (str(exc).splitlines() or ["no reason given"])[0]
        safe_write(f"A window cannot be opened here ({first_line}). The other commands work without one.", out)
        return 1
    try:
        service = _service(args, environ, out, create=True)
    except BaseException:
        root.destroy()
        raise
    app = App(root, service)
    if getattr(args, "search", None):
        app.words.set(args.search)
        root.after(0, app.search)
    root.mainloop()
    return 0


def cmd_tray(args, env, out, environ) -> int:
    from . import summons, tray as tray_module
    layout = _layout(args, environ, create=True)
    if args.startup == "add":
        path = tray_module.install_startup(layout)
        safe_write(f"The tray will start when you log in (the entry is {path}). Start it now with: dugup tray", out)
        return 0
    if args.startup == "remove":
        path = tray_module.remove_startup()
        safe_write("The tray will no longer start when you log in." if path else "There was no start-at-login entry.", out)
        return 0
    if args.quit:
        answer = summons.summon(layout.root, "quit")
        safe_write("The tray was told to quit." if answer else "No tray is running.", out)
        return 0
    if summons.summon(layout.root, "ping") == "pong":
        summons.summon(layout.root, "popup")
        safe_write("DugUp is already in the tray; its search box was brought up.", out)
        return 0
    try:
        import tkinter
    except ImportError:
        safe_write("The tray needs tkinter, which this Python does not have.", out)
        return 1
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        safe_write(f"A window cannot be opened here ({(str(exc).splitlines() or ['no reason'])[0]}), so there is no tray.", out)
        return 1
    try:
        service = _service(args, environ, out, create=True)
    except BaseException:
        root.destroy()
        raise
    tray = tray_module.Tray(root, service, service.settings, make_icon=(lambda t: None) if args.no_icon else None,
                            say=lambda text: safe_write(text, out))
    for line in tray.status_lines():
        safe_write(line, out)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        tray.quit()
        try:
            root.destroy()
        except Exception:
            pass
    return 0


def cmd_popup(args, env, out, environ) -> int:
    from . import summons
    layout = _layout(args, environ, create=False)
    if summons.summon(layout.root, "popup"):
        return 0
    safe_write("No tray is running, so there is no search box to bring up. Start it with: dugup tray", out)
    return 1


def cmd_meaning(args, env, out, environ, stop: Optional[threading.Event] = None, install_signals: bool = True) -> int:
    service = _service(args, environ, out, create=False)
    if service is None:
        return 1
    stop = stop or threading.Event()
    printer = ProgressPrinter(out)
    previous = None

    def on_interrupt(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt
        stop.set()
        printer.finish()
        safe_write("Stopping.", out)

    if install_signals and threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, on_interrupt)
    try:
        with service:
            if args.meaning_command == "status":
                print_meaning_status(service, out)
                return 0
            if args.meaning_command == "fill":
                waiting = service.meaning_waiting()
                safe_write(f"Sketching the meaning of {waiting:,} file(s) with the {service.settings.meaning_model} model "
                           f"({service.meaning_model()}). The model is fetched on first use. Ctrl+C stops; what is done "
                           "is kept and the next run continues.", out)
                report = service.run_meaning(stop, printer, args.budget, args.gpu)
                printer.finish()
                if isinstance(report, Failure):
                    return _fail(report, out)
                return print_meaning_report(report, out)
            choices = ("small", "larger") if args.model == "both" else (args.model,)
            safe_write(f"Measuring searching by meaning on {args.pieces:,} piece(s) of this index's text, with the "
                       f"{' and '.join(choices)} model(s). The models are fetched on first use (90 MB and 547 MB). "
                       "Nothing is stored in the index. Ctrl+C stops.", out)
            found = service.measure_meaning(args.pieces, choices, args.gpu, args.ask, printer, stop)
            printer.finish()
            if isinstance(found, Failure):
                return _fail(found, out)
            print_measurement(found, out)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    return 0


def print_meaning_status(service, out: TextIO) -> None:
    done, could = service.meaning_coverage()
    share = f" ({100.0 * done / could:.0f}%)" if could else ""
    safe_write(f"Sketches of meaning: {done:,} of {could:,} file(s) with text{share}, with {service.meaning_model()}.", out)
    if could > done:
        safe_write(f"{could - done:,} file(s) wait (those in folders that stop short of meaning are passed over). "
                   "`dugup meaning fill` continues.", out)


def print_meaning_report(report, out: TextIO) -> int:
    """Prints a FillReport; returns the exit code."""
    if report.failure is not None:
        safe_write(f"Stopped early: {report.failure.detail}", out)
    device = f" on the {report.device}" if report.device else ""
    safe_write(f"Sketched {report.files:,} file(s), {report.pieces:,} piece(s) of text{device} in "
               f"{format_seconds(report.seconds)} ({report.per_second:,.1f} pieces/s; the model took "
               f"{report.load_seconds:.1f} s to load).", out)
    if report.left_alone:
        safe_write(f"{report.left_alone:,} file(s) were passed over: their folder stops short of meaning.", out)
    share = f" ({100.0 * report.done / report.could:.0f}%)" if report.could else ""
    safe_write(f"Now {report.done:,} of {report.could:,} file(s) with text have sketches{share}.", out)
    print_compacted(report.compacted, out)
    if report.stopped:
        safe_write("[Interrupted] Stopped cleanly. Run `dugup meaning fill` again to continue.", out)
        return 130
    if report.out_of_time:
        safe_write("Out of time for this run. Run `dugup meaning fill` again to continue.", out)
    if report.paused:
        safe_write(f"It paused itself: {report.paused}. Make room (or raise the limit) and run `dugup meaning fill` again.", out)
        return 1
    return 1 if report.failure is not None else 0


def cmd_alike(args, env, out, environ, stop: Optional[threading.Event] = None, install_signals: bool = True) -> int:
    service = _service(args, environ, out, create=False)
    if service is None:
        return 1
    stop = stop or threading.Event()
    printer = ProgressPrinter(out)
    previous = None

    def on_interrupt(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt
        stop.set()
        printer.finish()
        safe_write("Stopping.", out)

    if install_signals and threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, on_interrupt)
    try:
        with service:
            if args.alike_command == "status":
                print_looks_status(service, out)
                return 0
            if args.alike_command == "duplicates":
                return cmd_alike_duplicates(args, service, out, environ, stop)
            if args.alike_command == "like":
                page = service.search_like_file(args.picture, max(0, args.limit), max(0, args.offset))
                if isinstance(page, Failure):
                    return _fail(page, out)
                print_results(page, os.path.basename(args.picture), service, 0, out, looks=True)
                return 0
            if args.alike_command == "fill":
                waiting = service.looks_waiting()
                readers = max(1, args.readers or service.looks_readers())
                safe_write(f"Looking at {waiting:,} picture(s) with {readers} reader(s) side by side. The picture model is "
                           "fetched on first use (352 MB). Ctrl+C stops; what is done is kept and the next run continues.", out)
                report = service.run_looks(stop, printer, args.budget, args.gpu, args.readers)
                printer.finish()
                if isinstance(report, Failure):
                    return _fail(report, out)
                return print_looks_report(report, out)
            safe_write(f"Measuring pictures by likeness on {args.pictures:,} of this index's pictures. The picture model is "
                       "fetched on first use (352 MB, and 254 MB more for sentences). Nothing is stored in the index. Ctrl+C stops.", out)
            found = service.measure_looks(args.pictures, args.gpu, args.ask, args.like, printer, stop)
            printer.finish()
            if isinstance(found, Failure):
                return _fail(found, out)
            print_looks_measurement(found, out)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    return 0


def cmd_rename(args, env, out, environ, stop: Optional[threading.Event] = None, install_signals: bool = True,
               ask: Callable[[str], str] = input) -> int:
    service = _service(args, environ, out, create=False)
    if service is None:
        return 1
    stop = stop or threading.Event()
    with service:
        if args.undo:
            try:
                with open(args.undo, "r", encoding="utf-8-sig") as handle:
                    text = handle.read()
            except OSError as exc:
                safe_write(f"The record could not be read ({type(exc).__name__}: {exc}).", out)
                return 1
            done = service.undo_names(text)
            safe_write(f"Put back {len(done.renamed):,} name(s); left {len(done.skipped):,}"
                       + (" (stopped)" if done.stopped else "") + (f". The record of this undo is in {done.record}." if done.record else "."), out)
            if done.record_problem:
                safe_write(f"Stopped: the record could not be written ({done.record_problem}). What was put back:", out)
                for new_path, old_path in done.renamed:
                    safe_write(f"  {new_path} -> {old_path}", out)
            if done.index_problems:
                safe_write(f"{len(done.index_problems)} put-back file(s) could not be updated in the index; the next index update sees the names.", out)
            for path, why in done.skipped[:20]:
                safe_write(f"  left: {path}: {why}", out)
            return 0 if not done.record_problem else 1
        if not args.folder:
            safe_write("Name a folder to preview, or --undo RECORD.", out)
            return 1
        if args.apply and not args.record:
            safe_write("--apply needs --record FILE: a file that will list every rename, so it can be undone.", out)
            return 1
        if args.record:
            problem = save_lines(args.record, [], args, environ)
            if problem:
                safe_write(problem, out)
                return 1
        printer = ProgressPrinter(out)
        previous = None

        def on_interrupt(signum, frame):
            if stop.is_set():
                raise KeyboardInterrupt
            stop.set()
            printer.finish()
            safe_write("Stopping.", out)

        if install_signals and threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGINT, on_interrupt)
        if not args.no_caption and service.captions_ready() is not True and "not fetched" in str(service.captions_ready()):
            safe_write("The captioning model is fetched now, once (about 1.1 GB, checked against known checksums). Ctrl+C stops.", out)
        try:
            found = service.plan_names(args.folder, args.pattern, not args.no_caption, args.longer, printer, stop, args.gpu, args.every)
            printer.finish()
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
        if isinstance(found, Failure):
            return _fail(found, out)
        if found.stopped:
            safe_write("Stopped before every picture was captioned; nothing was renamed.", out)
            return 130
        print_name_plan(found, out)
        if not args.apply or not found.to_rename:
            if found.to_rename and not args.apply:
                safe_write("Nothing was renamed. Add --apply --record FILE to rename as shown.", out)
            return 0
        if not args.yes:
            try:
                reply = ask(f"Rename {len(found.to_rename):,} picture(s) as shown? Type yes to go on: ")
            except (EOFError, KeyboardInterrupt):
                reply = ""
            if reply.strip().lower() != "yes":
                safe_write("Nothing was renamed.", out)
                return 0
        from . import records
        try:      # created exclusively above (save_lines), empty; filled as the renames happen
            handle = open(args.record, "a", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n")
        except OSError as exc:
            safe_write(f"The record could not be written ({type(exc).__name__}: {exc}); nothing was renamed.", out)
            return 1
        journal = records.Journal(handle)
        try:
            journal.outcome(f"pattern\t{found.pattern}")
            done = service.apply_names(found, journal=journal, stop=stop)
        except records.RecordError as exc:
            journal.close()
            safe_write(f"The record could not be written ({exc}); nothing was renamed.", out)
            return 1
        finally:
            journal.close()
        safe_write(f"Renamed {len(done.renamed):,} picture(s); left {len(done.skipped):,} alone"
                   + (" (stopped)" if done.stopped else "") + f". The record is in {args.record}; `dugup rename --undo {args.record}` puts the names back.", out)
        if done.record_problem:
            safe_write(f"Stopped: the record could not be written ({done.record_problem}). What was renamed:", out)
            for old_path, new_path in done.renamed:
                safe_write(f"  {old_path} -> {new_path}", out)
        if done.index_problems:
            safe_write(f"{len(done.index_problems)} renamed file(s) could not be updated in the index; the next index update sees the new names.", out)
        for path, why in done.skipped[:20]:
            safe_write(f"  left: {path}: {why}", out)
    return 0


def print_name_plan(found, out: TextIO) -> None:
    device = f" on the {found.device}" if found.device else ""
    if found.captioned:
        safe_write(f"Captioned {found.captioned:,} picture(s){device} in {format_seconds(found.seconds)}.", out)
    safe_write(f"Pattern: {found.pattern}", out)
    for change in found.to_rename:
        safe_write(f"  {os.path.basename(change.old)}", out)
        safe_write(f"    -> {os.path.basename(change.new)}" + (f"    ({change.caption})" if change.caption else ""), out)
    if found.left_alone:
        safe_write(f"Left alone: {len(found.left_alone):,}", out)
        for change in found.left_alone[:20]:
            safe_write(f"  {os.path.basename(change.old)}: {change.why}", out)
        if len(found.left_alone) > 20:
            safe_write(f"  ... and {len(found.left_alone) - 20:,} more", out)
    safe_write(f"{len(found.to_rename):,} picture(s) would be renamed.", out)


def cmd_alike_duplicates(args, service, out, environ, stop, ask: Callable[[str], str] = input) -> int:
    if args.remove and not args.record:
        safe_write("--remove needs --record FILE: a file that will list what was moved and what was left alone, and why.", out)
        return 1
    if args.record:
        problem = save_lines(args.record, [], args, environ)
        if problem:
            safe_write(problem, out)
            return 1
    width = [0]

    def tell(done: int, total: int) -> None:
        line = f"[alike] comparing: {done:,} of {total:,} pictures"
        pad = " " * max(0, width[0] - len(line))
        width[0] = len(line)
        safe_write("\r" + line + pad, out, end="")

    found = service.alike_pictures(args.threshold, args.protect, args.prefer, stop, tell)
    safe_write("", out)
    if found.stopped:
        safe_write("Stopped.", out)
        return 130
    safe_write(f"Compared {found.pictures:,} picture(s) in {format_seconds(found.seconds)} at {found.threshold:.2f} and above: "
               f"{len(found.sets):,} set(s), {found.extra_pictures:,} extra picture(s), about {format_bytes(found.bytes_extra)}.", out)
    for number, same in enumerate(found.sets[:max(0, args.rows)], 1):
        safe_write(f"{number:>4}. keep   {same.keep.record.path}   ({same.keep.why})", out)
        for item in same.extra:
            safe_write(f"      extra  {item.record.path}   ({item.closeness:.3f})", out)
        for item in same.aside:
            safe_write(f"      kept aside (protected)  {item.record.path}   ({item.closeness:.3f})", out)
        for item in same.left:
            safe_write(f"      near, but below the threshold against the keeper: {item.record.path}   ({item.closeness:.3f})", out)
    if len(found.sets) > args.rows:
        safe_write(f"... and {len(found.sets) - args.rows:,} more set(s) (--rows N shows more)", out)
    if not args.remove or not found.sets:
        if found.sets and not args.remove:
            safe_write("Nothing was moved. Add --remove --record FILE to move the extras to the trash.", out)
        return 0
    if not args.yes:
        try:
            reply = ask(f"Move all {found.extra_pictures:,} extra picture(s) to the trash? Type yes to go on: ")
        except (EOFError, KeyboardInterrupt):
            reply = ""
        if reply.strip().lower() != "yes":
            safe_write("Nothing was moved.", out)
            return 0
    try:      # the record was created exclusively above (save_lines), empty; it is filled as the moves happen
        handle = open(args.record, "a", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n")
    except OSError as exc:
        safe_write(f"The record could not be written ({type(exc).__name__}: {exc}); nothing was moved.", out)
        return 1
    from .records import Journal
    journal = Journal(handle)
    try:
        done = service.trash_alike_extras(found.sets, journal=journal)
    finally:
        journal.close()
    safe_write(f"Moved {len(done.moved):,} picture(s) to the trash; left {len(done.skipped):,} alone"
               + (" (stopped)" if done.stopped else "") + f". The record is in {args.record}.", out)
    if done.record_problem:
        safe_write(f"Stopped: the record could not be written ({done.record_problem}). What was moved:", out)
        for path, _ in done.moved:
            safe_write(f"  {path}", out)
    for path, why in done.skipped[:10]:
        safe_write(f"  left: {path}: {why}", out)
    return 0


def print_looks_status(service, out: TextIO) -> None:
    done, could, unread = service.looks_coverage()
    share = f" ({100.0 * done / could:.0f}%)" if could else ""
    safe_write(f"Sketches of pictures: {done:,} of {could:,}{share}, with {service.looks_model()}.", out)
    if unread:
        safe_write(f"{unread:,} of those could not be read as pictures:", out)
        for path, why in service.store.unread_pictures(service.looks_model(), 20):
            safe_write(f"  {path}\n      {why}", out)
        if unread > 20:
            safe_write(f"  ... and {unread - 20:,} more", out)
    if could > done:
        safe_write(f"{could - done:,} picture(s) wait (those in folders without \"Pictures alike\" are passed over). "
                   "`dugup alike fill` continues.", out)


def print_looks_report(report, out: TextIO) -> int:
    if report.failure is not None:
        safe_write(f"Stopped early: {report.failure.detail}", out)
    device = f" on the {report.device}" if report.device else ""
    safe_write(f"Looked at {report.pictures + report.unread:,} picture(s){device}, {report.readers} reader(s) side by side, in "
               f"{format_seconds(report.seconds)} ({report.per_second:,.1f} pictures/s; the model took {report.load_seconds:.1f} s to load).", out)
    if report.unread:
        safe_write(f"{report.unread:,} could not be read as pictures; `dugup alike status` lists them. They are not tried again "
                   "unless they change.", out)
    if report.left_alone:
        safe_write(f"{report.left_alone:,} picture(s) were passed over: their folder leaves out \"Pictures alike\".", out)
    share = f" ({100.0 * report.done / report.could:.0f}%)" if report.could else ""
    safe_write(f"Now {report.done:,} of {report.could:,} picture(s) have sketches{share}.", out)
    print_compacted(report.compacted, out)
    if report.stopped:
        safe_write("[Interrupted] Stopped cleanly. Run `dugup alike fill` again to continue.", out)
        return 130
    if report.out_of_time:
        safe_write("Out of time for this run. Run `dugup alike fill` again to continue.", out)
    if report.paused:
        safe_write(f"It paused itself: {report.paused}. Make room (or raise the limit) and run `dugup alike fill` again.", out)
        return 1
    return 1 if report.failure is not None else 0


def print_looks_measurement(found, out: TextIO) -> None:
    safe_write(f"Sample: {found.pictures:,} picture(s); the index lists {found.in_index:,}.", out)
    for note in found.notes:
        safe_write(f"  note: {note}", out)
    safe_write("Speed (the model already loaded; the first load is given apart):", out)
    for t in found.timings:
        whole = found.in_index / t.per_second if t.per_second > 0 else 0.0
        failed = f"   {t.failed} could not be read" if t.failed else ""
        safe_write(f"  {t.device:<24} {t.per_second:8,.1f} pictures/s   load {t.load_seconds:5.1f} s   "
                   f"all the pictures: about {format_seconds(whole)}{failed}", out)
    if found.unread:
        safe_write("Could not be read:", out)
        for why, paths in sorted(found.unread.items(), key=lambda kv: -len(kv[1])):
            safe_write(f"  {len(paths):,}: {why}", out)
            for path in paths[:3]:
                safe_write(f"       {path}", out)
    safe_write(f"Disk for sketches of every picture: {format_bytes(found.disk_for(1))} as bytes "
               f"({format_bytes(found.disk_for(4))} as full numbers).", out)
    if found.answers:
        safe_write("What was found in the sample (the closest 5; with this kind of model 0.30 is close and 0.20 is far):", out)
        for answer in found.answers:
            safe_write(f"  \"{answer.asked}\"", out)
            for hit in answer.hits:
                safe_write(f"     {hit.score:.3f}  {hit.path}", out)


def print_measurement(found, out: TextIO) -> None:
    from .embedding import SHAPES, SIZES
    safe_write(f"Sample: {found.pieces:,} piece(s); the whole index holds {found.in_index:,}.", out)
    for note in found.notes:
        safe_write(f"  note: {note}", out)
    safe_write("Speed (the model already loaded; the first load is given apart):", out)
    for t in found.timings:
        whole = found.in_index / t.per_second if t.per_second > 0 else 0.0
        safe_write(f"  {t.choice:<7} {t.device:<24} {t.per_second:8,.1f} pieces/s   load {t.load_seconds:5.1f} s   "
                   f"the whole index: about {format_seconds(whole)}", out)
    safe_write("Disk for sketches of the whole index (4 bytes a number; a byte a number is also possible, at some cost in precision):", out)
    for choice in sorted({t.choice for t in found.timings}):
        full = SHAPES[choice].dims
        sizes = [full] + ([d for d in sorted(SIZES.values(), reverse=True) if d < full] if SHAPES[choice].cuttable else [])
        for dims in sizes:
            safe_write(f"  {choice:<7} {dims:>4} numbers: {format_bytes(found.disk_for(dims))} (or {format_bytes(found.disk_for(dims, 1))} as bytes)", out)
    if found.answers:
        safe_write("What the questions found in the sample (the closest 5; a score near 1 is close):", out)
        for answer in found.answers:
            agree = found.agreement.get((answer.question, answer.dims))
            tail = "" if agree is None else f"   (keeps {agree:.0%} of the top 10 at full size)"
            safe_write(f"  \"{answer.question}\"  with {answer.choice}, {answer.dims} numbers{tail}", out)
            for hit in answer.hits:
                safe_write(f"     {hit.score:.2f}  {os.path.basename(hit.path)}" + (f" p.{hit.page}" if hit.page else "") + f"  | {hit.snippet}", out)


def cmd_duplicates(args, env, out, environ, stop: Optional[threading.Event] = None, install_signals: bool = True,
                   answer: Optional[Callable[[str], str]] = None) -> int:
    from . import trash
    if args.try_trash:
        path = os.path.abspath(os.path.expanduser(args.try_trash))
        safe_write(f"Here a file is {trash.how()}.", out)
        problem = trash.to_trash(path)
        if problem:
            safe_write(f"{path}: {problem}", out)
            return 1
        safe_write(f"{path} was moved to the trash. Look there (the drive's own trash for a cloud drive, else the "
                   "Recycle Bin) to see that it arrived, and put it back from there.", out)
        return 0
    if args.remove and not args.record:
        safe_write("--remove needs --record FILE: a file that will list what was moved and what was left alone, and why.", out)
        return 1
    service = _service(args, environ, out, create=False)
    if service is None:
        return 1
    with service:
        found = service.duplicates(args.drive, args.protect, args.prefer)
        if args.remove:
            return _remove_duplicates(found, service, args, out, environ, stop, install_signals, answer)
        safe_write(f"The same file in several places below {found.drive} (from the index; nothing is changed, DugUp never deletes).", out)
        for folder in found.protected:
            gone = folder in found.protected_empty
            safe_write(f"  never proposed: {folder}" + ("   <- NO INDEXED FILE LIES BELOW THIS: is the name right?" if gone else ""), out)
        for folder in found.preferred:
            safe_write(f"  the proper home, kept when there is a choice: {folder}", out)
        if not found.sets:
            safe_write("No file below there is kept in two or more places (among the files whose content is known).", out)
            return 0
        safe_write(f"{found.extra_files:,} extra copy(ies) of {len(found.sets):,} file(s), taking about {format_bytes(found.wasted_bytes)}. "
                   "'keep' is the copy in a proper-home folder, else in the larger set of files, then the one higher up; every other copy is 'extra'.", out)
        if args.save:
            lines = []
            for same in found.sets:
                for word, item in [("keep", same.keep)] + [("extra", c) for c in same.extra] + [("protected", c) for c in same.aside]:
                    lines.append(f"{word}\t{item.record.path}\t{item.record.size}\t{item.folder_files}")
                lines.append("")
            problem = save_lines(args.save, lines, args, environ)
            if problem:
                safe_write(problem, out)
                return 1
            safe_write(f"The whole report is in {args.save} (keep or extra, a tab, the path, the size, the files in its folder).", out)
            return 0
        for same in found.sets[:max(1, args.rows)]:
            safe_write(f"  {format_bytes(same.size)} each, {len(same.extra)} extra: {format_bytes(same.wasted)}", out)
            safe_write(f"      keep   {same.keep.record.path}   ({same.keep.folder_files:,} files in its folder)", out)
            for item in same.extra:
                safe_write(f"      extra  {item.record.path}   ({item.folder_files:,} files in its folder)", out)
            for item in same.aside:
                safe_write(f"      kept aside (protected)  {item.record.path}", out)
        if len(found.sets) > args.rows:
            safe_write(f"  ... and {len(found.sets) - max(1, args.rows):,} more set(s); --rows N shows more, --save FILE writes all", out)
    return 0


def _remove_duplicates(found, service, args, out, environ, stop, install_signals, answer) -> int:
    from . import trash
    record_path = os.path.abspath(os.path.expanduser(args.record))
    problem = save_lines(record_path, [], args, environ)      # claimed now, so the run cannot end with nowhere to write
    if problem:
        safe_write(problem, out)
        return 1
    safe_write(f"Below {found.drive}: {found.extra_files:,} extra copy(ies) of {len(found.sets):,} file(s), about "
               f"{format_bytes(found.wasted_bytes)}.", out)
    for folder in found.protected:
        gone = folder in found.protected_empty
        safe_write(f"  never touched: {folder}" + ("   <- NO INDEXED FILE LIES BELOW THIS: is the name right?" if gone else ""), out)
    for folder in found.preferred:
        safe_write(f"  the proper home, kept when there is a choice: {folder}", out)
    if not found.sets:
        safe_write("Nothing to move.", out)
        return 0
    safe_write(f"Each extra would be {trash.how()}, one by one, only after the copy kept is found to hold the same "
               "bytes at that moment. Nothing is deleted outright. Ctrl+C stops between files; what was moved stays moved.", out)
    safe_write(f"What was moved, and what was left alone and why, will be written to {record_path}.", out)
    shown = found.sets[:max(1, args.rows)]
    for same in shown:
        safe_write(f"  keep   {same.keep.record.path}", out)
        for item in same.extra:
            safe_write(f"  move   {item.record.path}", out)
    if len(found.sets) > len(shown):
        safe_write(f"  ... and {len(found.sets) - len(shown):,} more set(s) (--rows N shows more; --save FILE writes all)", out)
    if not args.yes:
        ask = answer or (lambda prompt: input(prompt))
        try:
            reply = ask(f"Move all {found.extra_files:,} extra copy(ies) to the trash? Type yes to go on: ")
        except (EOFError, KeyboardInterrupt):
            reply = ""
        if reply.strip().lower() != "yes":
            safe_write("Nothing was moved.", out)
            return 0
    stop = stop or threading.Event()
    previous = None

    def on_interrupt(signum, frame):
        if stop.is_set():
            raise KeyboardInterrupt
        stop.set()
        safe_write("\nStopping after the file in hand. What was moved stays moved; the rest stays where it is.", out)

    if install_signals and threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, on_interrupt)
    width = [0]

    def tell(at: int, total: int, path: str) -> None:
        line = f"[move] {at:,} of {total:,}  | {shorten(os.path.basename(path), 50)}"
        pad = " " * max(0, width[0] - len(line))
        width[0] = len(line)
        safe_write("\r" + line + pad, out, end="")

    # the record is written as it goes: the intent on disk before each move, the outcome after; a record that
    # cannot be written stops the run (records.Journal), so a file is never moved without one
    from .records import Journal
    try:
        handle = open(record_path, "a", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n")
    except OSError as exc:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        safe_write(f"The record could not be written ({type(exc).__name__}: {exc}); nothing was moved.", out)
        return 1
    journal = Journal(handle)
    try:
        done = service.remove_duplicates(found, stop, tell, journal=journal)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        journal.close()
    safe_write("", out)
    if done.record_problem:
        safe_write(f"Stopped: the record could not be written ({done.record_problem}). What was moved before that:", out)
        for path, kept in done.moved:
            safe_write(f"  moved\t{path}\tkept\t{kept}", out)
    safe_write(f"Moved {len(done.moved):,} file(s) to the trash, about {format_bytes(done.bytes_freed)}; left {len(done.skipped):,} alone"
               + (" (stopped)" if done.stopped else "") + f". The record is in {record_path}.", out)
    for path, why in done.skipped[:10]:
        safe_write(f"  left: {path}: {why}", out)
    if len(done.skipped) > 10:
        safe_write(f"  ... and {len(done.skipped) - 10:,} more in the record", out)
    return 0


COMMANDS = {"doctor": cmd_doctor, "init": cmd_init, "roots": cmd_roots, "index": cmd_index, "search": cmd_search,
            "status": cmd_status, "duplicates": cmd_duplicates, "meaning": cmd_meaning, "alike": cmd_alike, "rename": cmd_rename, "ocr": cmd_ocr, "space": cmd_space, "window": cmd_window, "tray": cmd_tray, "popup": cmd_popup, "models": cmd_models, "ask": cmd_ask, "config": cmd_config}


def main(argv: Optional[List[str]] = None, env: Optional[Environment] = None,
         stdout: Optional[TextIO] = None, environ: Optional[dict] = None) -> int:
    stdout = stdout or sys.stdout
    environ = os.environ if environ is None else environ
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args, env, stdout, environ)
    except KeyboardInterrupt:
        safe_write("\n[Interrupted] Stopped. Anything not finished stays queued; run the command again to continue.", stdout)
        return 130
    except DataDirError as exc:
        safe_write(f"The data folder cannot be used: {exc.reason}", stdout)
        return 1
    except StoreError as exc:
        safe_write(f"The index cannot be used: {exc}", stdout)
        return 1
