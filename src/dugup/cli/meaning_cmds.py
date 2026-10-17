"""The `meaning` command: measuring and filling search by meaning."""

from __future__ import annotations

import os
import signal
import threading
from typing import Optional, TextIO
from ..shapes import Failure
from .common import _fail, _service
from .output import ProgressPrinter, safe_write, format_seconds, print_compacted, format_bytes


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


def print_measurement(found, out: TextIO) -> None:
    from ..embedding import SHAPES, SIZES
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
