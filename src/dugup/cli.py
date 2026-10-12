"""Command line: a small client of the Service (guide 5c item 7).

`search`, `status`, `index` and `roots` go through the Service. `init` and `doctor` are the bootstrap
commands that must work before any index exists. Everything printed goes through `safe_write`, so a
console in a legacy code page shows an escape instead of crashing on a name it cannot display.
"""

from __future__ import annotations

import argparse
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
from . import settings as settings_module
from .datadir import DataDirError, DataLayout, ensure_data_dir, never_write_from_env, resolve_data_dir
from .doctor import Environment, has_failure, render, run_checks
from .filesource import SUGGESTED_EXCLUDES
from .indexer import KEEP, REFUSE, REREAD, IndexReport, Progress
from .protocols import WARN_MASS_LOSS, WARN_ROOT_EMPTY
from .service import LocalService
from .shapes import POLICIES, POLICY_OPEN, Failure, Root, SearchFilters, SearchPage, SearchRow
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
    if p.phase == "download":
        left = p.remaining_seconds
        left_text = "time left unknown" if left is None else f"about {format_seconds(left)} left"
        return (f"[download] {p.root}: {p.done / 1048576:,.1f} of {p.total / 1048576:,.1f} MB ({p.percent:.0f}%)  "
                f"{p.rate / 1048576:.1f} MB/s  {left_text}")
    if p.phase == "unpack":
        return f"[download] unpacking {p.root}"
    if p.phase == "measure":
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
    choice = index.add_mutually_exclusive_group()
    choice.add_argument("--reread", action="store_true", help="after a settings change: read every file again")
    choice.add_argument("--keep", action="store_true", help="after a settings change: keep what is stored as it is")

    ocr = sub.add_parser("ocr", parents=[common],
                         help="read the words in pictures and scanned pages (Ctrl+C stops cleanly; run again to continue)")
    ocr.add_argument("--root", action="append", default=None, metavar="FOLDER", help="only this folder (repeatable)")
    ocr.add_argument("--minutes", type=float, default=None, metavar="N", help="stop after about N minutes")
    ocr.add_argument("--retry-failed", action="store_true", help="try the pages OCR failed on again")
    ocr.add_argument("--reread-old", action="store_true",
                     help="read again the pages that an older version of the OCR engine read")

    space = sub.add_parser("space", parents=[common], help="how much disk the index uses, and its spending limit")
    space.add_argument("--compact", action="store_true",
                       help="give back the disk space that removed and re-read text left in the index")
    space.add_argument("--limit", type=int, default=None, metavar="MB",
                       help="stop indexing and OCR when the index is larger than this many megabytes (0: no limit)")

    sub.add_parser("window", parents=[common], help="open the search window")

    models_parser = sub.add_parser("models", help="the optional language model for plain-words search")
    models_sub = models_parser.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser("status", parents=[common], help="what is installed, and what an install would download")
    install = models_sub.add_parser("install", parents=[common],
                                    help="download the model and its runtime (Ctrl+C stops; run again to continue)")
    install.add_argument("--yes", action="store_true", help="go ahead; without this the plan is shown and nothing is downloaded")
    models_sub.add_parser("remove", parents=[common], help="delete the downloaded model and runtime")

    search = sub.add_parser("search", parents=[common], help="find files by the words in them or in their names")
    search.add_argument("words", nargs="+")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--sort", choices=sorted(SORT_COLUMNS), default="rank")
    search.add_argument("--desc", action="store_true", help="largest (or last) first")
    search.add_argument("--exact", action="store_true",
                        help="only the very word forms typed: education then does not find educate")
    search.add_argument("--context", type=int, default=60, help="characters shown on each side of a match")

    status = sub.add_parser("status", parents=[common], help="what is indexed, what is waiting, what needs attention")
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
    service = _service(args, environ, out, create=args.roots_command in ("add", "suggest"))
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
            return 0
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if handler is not None:
            logging.getLogger("dugup").removeHandler(handler)
            handler.close()


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
               f"{report.read - report.with_words:,} with none, {report.failed:,} failed", out)
    if report.second_tries:
        safe_write(f"{report.second_tries:,} page(s) ran out of time and were read again, last, with more time.", out)
    if report.left:
        safe_write(f"{report.left:,} page(s) belong to files that could not be reached; they stay queued.", out)
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
            report = service.run_ocr(stop, printer, roots, budget, args.retry_failed, args.reread_old)
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


def print_results(page: SearchPage, query: str, service: LocalService, context: int, out: TextIO) -> None:
    if page.total == 0:
        safe_write(f"No files found for: {query}", out)
        return
    first = page.offset + 1 if page.rows else 0
    last = page.offset + len(page.rows)
    safe_write(f"Showing {first:,} to {last:,} of {page.total:,} file(s) for: {query}", out)
    for number, row in enumerate(page.rows, first):
        state = "" if row.state == "done" else f"  [{row.state}]"
        safe_write(f"{number:>3}. {row.name}{state}", out)
        safe_write(f"     {row.folder}", out)
        for note in _flags(row, service.settings.missing_retention_days):
            safe_write(f"     ! {note}", out)
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
        filters = SearchFilters(exact=True) if args.exact else None
        page = service.search(query, max(0, args.limit), max(0, args.offset), args.sort, args.desc, filters)
        if isinstance(page, Failure):
            return _fail(page, out)
        print_results(page, query, service, max(0, args.context), out)
    return 0


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
        if args.attention is not None:
            records = service.needs_attention(limit=max(0, args.attention))
            safe_write(f"Needs attention: showing {len(records):,} of {tally['attention']:,}", out)
            for record in records:
                safe_write(f"  [{record.state.value}] {record.path}", out)
                safe_write(f"      {record.error}", out)
        elif tally["attention"]:
            safe_write(f"{tally['attention']:,} file(s) failed or were read only in part. List them: dugup status --attention", out)
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


def set_config_value(config, key: str, value: int) -> None:
    """Write one `key = number` line into config.toml, replacing an earlier line for the same key and
    leaving everything else in the file as it is."""
    try:
        lines = config.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    kept = []
    section = False
    done = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if not section and not done:      # top-level keys must stand before the first table
                kept.append(f"{key} = {value}")
                done = True
            section = True
        if not section and stripped.split("=")[0].strip() == key:
            if not done:
                kept.append(f"{key} = {value}")
                done = True
            continue
        kept.append(line)
    if not done:
        kept.append(f"{key} = {value}")
    temporary = config.with_name(config.name + ".new")
    temporary.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.replace(temporary, config)


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
        plan = service.models_plan()
        if args.models_command == "status":
            if installed:
                safe_write(f"Installed: runtime {installed.get('runtime')}, model {installed.get('model')}", out)
                safe_write(f"  in {service.models_folder}", out)
            else:
                safe_write("Plain-words search is not installed.", out)
            if service.settings.intent_url or service.settings.intent_server:
                safe_write("config.toml names its own model server, which is used instead of an installed one.", out)
            if not installed:
                safe_write(plan.detail if isinstance(plan, Failure) else plan.describe(), out)
                if not isinstance(plan, Failure):
                    safe_write("To install: dugup models install --yes", out)
            return 0
        if isinstance(plan, Failure):
            return _fail(plan, out)
        safe_write(plan.describe(), out)
        if not args.yes:
            safe_write("Nothing was downloaded. To go ahead: dugup models install --yes", out)
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
            result = service.install_models(stop, printer)
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
        printer.finish()
        if isinstance(result, Failure):
            if result.reason.value == "stopped":
                safe_write("[Interrupted] Stopped. Run `dugup models install --yes` again to continue.", out)
                return 130
            return _fail(result, out)
        safe_write("Installed. The window now has an \"In plain words\" box: dugup window", out)
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
    App(root, service)
    root.mainloop()
    return 0


COMMANDS = {"doctor": cmd_doctor, "init": cmd_init, "roots": cmd_roots, "index": cmd_index, "search": cmd_search,
            "status": cmd_status, "ocr": cmd_ocr, "space": cmd_space, "window": cmd_window, "models": cmd_models}


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
