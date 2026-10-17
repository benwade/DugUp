"""Bootstrap and housekeeping commands: doctor, init, config, space, models."""

from __future__ import annotations

import signal
import threading
from typing import Optional, TextIO
from .. import catalogue, settings as settings_module
from ..datadir import DataDirError, DataLayout, never_write_from_env, resolve_data_dir
from ..doctor import has_failure, render, run_checks
from ..settings import Settings
from ..shapes import Failure, FailureReason
from .common import _service, _fail, _layout
from .output import safe_write, megabytes, ProgressPrinter


def cmd_doctor(args, env, out, environ) -> int:
    never_write = list(args.never_write) + never_write_from_env(environ)
    try:
        data_dir = resolve_data_dir(args.data_dir, environ=environ, never_write=never_write, allow_synced=args.allow_synced)
    except DataDirError as exc:
        from ..doctor import FAIL, Check
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
