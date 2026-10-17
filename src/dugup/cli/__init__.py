"""Command line: a small client of the Service (guide 5c item 7).

`search`, `status`, `index` and `roots` go through the Service. `init` and `doctor` are the bootstrap
commands that must work before any index exists. Everything printed goes through `safe_write`, so a
console in a legacy code page shows an escape instead of crashing on a name it cannot display.

The commands live in one module per group; this package wires them to the parser. Public helpers are
re-exported here so `from dugup.cli import X` keeps working for the window and the tests.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, TextIO

from ..datadir import DataDirError
from ..doctor import Environment
from ..store import StoreError
from .alike_cmds import cmd_alike, print_looks_measurement, print_looks_report, print_looks_status
from .changes_cmds import cmd_alike_duplicates, cmd_duplicates, cmd_rename, print_name_plan
from .common import save_file_list, save_lines
from .data_cmds import cmd_data
from .desktop_cmds import cmd_popup, cmd_remote, cmd_tray, cmd_window
from .index_cmds import cmd_index, cmd_ocr, cmd_status, print_ocr_report, print_report
from .meaning_cmds import cmd_meaning, print_meaning_report, print_meaning_status, print_measurement
from .output import (MISSING_NOTE, UNAVAILABLE_NOTE, WARNING_TEXT, ProgressPrinter,
                     format_bytes, format_date, format_seconds, megabytes, one_line, print_compacted, progress_text,
                     quote, safe_write, shorten)
from .parser import build_parser
from .roots_cmds import cmd_roots, print_suggestions, print_survey
from .search_cmds import ALSO_AT_SHOWN, cmd_ask, cmd_search, print_results
from .setup_cmds import cmd_config, cmd_doctor, cmd_init, cmd_models, cmd_space, set_config_value, space_hint, suggested_limit_mb

__all__ = [
    "ALSO_AT_SHOWN", "COMMANDS", "MISSING_NOTE", "UNAVAILABLE_NOTE", "WARNING_TEXT", "ProgressPrinter",
    "build_parser", "cmd_alike", "cmd_alike_duplicates", "cmd_ask", "cmd_config", "cmd_data", "cmd_doctor", "cmd_duplicates",
    "cmd_index", "cmd_init", "cmd_meaning", "cmd_models", "cmd_ocr", "cmd_popup", "cmd_remote", "cmd_rename",
    "cmd_roots", "cmd_search", "cmd_space", "cmd_status", "cmd_tray", "cmd_window", "format_bytes", "format_date",
    "format_seconds", "main", "megabytes", "one_line", "print_compacted", "print_looks_measurement",
    "print_looks_report", "print_looks_status", "print_meaning_report", "print_meaning_status", "print_measurement",
    "print_name_plan", "print_ocr_report", "print_report", "print_results", "print_suggestions", "print_survey",
    "progress_text", "quote", "safe_write", "save_file_list", "save_lines", "set_config_value", "shorten",
    "space_hint", "suggested_limit_mb",
]


COMMANDS = {"doctor": cmd_doctor, "init": cmd_init, "roots": cmd_roots, "index": cmd_index, "search": cmd_search,
            "status": cmd_status, "duplicates": cmd_duplicates, "meaning": cmd_meaning, "alike": cmd_alike, "rename": cmd_rename, "ocr": cmd_ocr, "space": cmd_space, "window": cmd_window, "tray": cmd_tray, "remote": cmd_remote, "popup": cmd_popup, "models": cmd_models, "ask": cmd_ask, "config": cmd_config, "data": cmd_data}


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
