"""Commands that change files on disk: rename, duplicates, picture duplicates."""

from __future__ import annotations

import os
import signal
import threading
from typing import Callable, Optional, TextIO
from ..shapes import Failure
from .common import _fail, _service, save_lines
from .output import ProgressPrinter, safe_write, format_seconds, format_bytes, shorten


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
        from .. import records
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
        for item in same.stale:
            safe_write(f"      changed since it was indexed, not weighed until a scan renews it: {item.record.path}   ({item.stale})", out)
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
    from ..records import Journal
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


def cmd_duplicates(args, env, out, environ, stop: Optional[threading.Event] = None, install_signals: bool = True,
                   answer: Optional[Callable[[str], str]] = None) -> int:
    from .. import trash
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
    from .. import trash
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
    from ..records import Journal
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
