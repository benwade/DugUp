"""Search commands: search and ask."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import List, TextIO
from ..service import LocalService
from ..shapes import Failure, FailureReason, SearchFilters, SearchPage, SearchRow, WHERE_BOTH, WHERE_LOOKS, WHERE_MEANING
from .common import _fail, _service
from .output import MISSING_NOTE, UNAVAILABLE_NOTE, format_date, one_line, safe_write


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
            from ..store import parse_query
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
