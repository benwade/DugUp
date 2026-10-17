"""The `alike` command: pictures by likeness."""

from __future__ import annotations

import os
import signal
import threading
from typing import Optional, TextIO
from ..shapes import Failure
from .changes_cmds import cmd_alike_duplicates
from .common import _fail, _service
from .output import ProgressPrinter, safe_write, format_seconds, print_compacted, format_bytes
from .search_cmds import print_results


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
