"""Indexing commands: index, ocr, status, and their reports."""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import threading
from dataclasses import replace
from typing import Optional, TextIO
from ..indexer import KEEP, REFUSE, REREAD, IndexReport
from ..shapes import Failure
from .alike_cmds import print_looks_report
from .common import _fail, _file_log, _service, _chosen_roots, save_file_list
from .meaning_cmds import print_meaning_report
from .output import WARNING_TEXT, format_seconds, safe_write, ProgressPrinter, print_compacted
from .roots_cmds import _print_root


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
