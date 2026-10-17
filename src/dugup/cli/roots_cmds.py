"""The `roots` command: choosing, surveying and excluding the folders to index."""

from __future__ import annotations

import os
from typing import List, Optional, TextIO
from ..filesource import SUGGESTED_EXCLUDES
from ..service import LocalService
from ..shapes import OPTION_OCR, Failure, Root
from ..suggest import SAFE, Suggestion
from .common import _fail, _service
from .output import safe_write, _size_text, quote, shorten, format_bytes, WARNING_TEXT, format_date
from .setup_cmds import space_hint


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


def print_survey(found, rows: int, out: TextIO) -> None:
    from ..survey import CODE, DOCUMENTS, LEVELS, MEDIA, OTHER, PICTURES, TEXT
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
    from ..survey import HERE
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
            from ..shapes import level_words, wants
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
            from .. import copies as finding
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
    from ..shapes import level_words
    for folder, level in root.levels:
        safe_write(f"  {folder or '(the whole folder)'}: {level_words(level)}", out)
    if root.exclude:
        safe_write(f"  skipping: {', '.join(root.exclude)}", out)
    if root.include:
        safe_write(f"  only: {', '.join(root.include)}", out)
    for code in root.warnings:
        safe_write(f"  WARNING: {WARNING_TEXT.get(code, code)}", out)
