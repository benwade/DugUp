"""`doctor`: is this machine ready, and if not, what is missing (guide 5c, 5d).

Every probe goes through an Environment object, so tests can describe a machine
with or without any tool and never depend on what is installed.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from .datadir import DataDirError, check_allowed
from .store import fts5_available

OK, WARN, FAIL, INFO = "ok", "warn", "FAIL", "info"
MIN_PYTHON = (3, 10)


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def _program_dirs() -> List[str]:
    found = []
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(key)
        if value and value not in found:
            found.append(value)
    return found


def _probe_fts5() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        return fts5_available(conn)
    finally:
        conn.close()


def find_ocr_card() -> str:
    """The graphics card route the OCR runtime here has, in words, or "". Asked in a short-lived process of
    its own, so looking does not load the runtime into this one; any trouble counts as "none"."""
    import subprocess
    import sys
    code = "from dugup.jobs import GPU_NAMES, gpu_provider; print(GPU_NAMES.get(gpu_provider(), ''))"
    try:
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        return done.stdout.strip().splitlines()[-1] if done.returncode == 0 and done.stdout.strip() else ""
    except Exception:
        return ""


@dataclass
class Environment:
    which: Callable[[str], Optional[str]] = shutil.which
    can_import: Callable[[str], bool] = lambda name: importlib.util.find_spec(name) is not None
    ocr_card: Callable[[], str] = lambda: find_ocr_card()
    exists: Callable[[str], bool] = os.path.exists
    platform: str = sys.platform
    python_version: Tuple[int, int] = field(default_factory=lambda: sys.version_info[:2])
    program_dirs: Sequence[str] = field(default_factory=_program_dirs)
    sqlite_version: str = sqlite3.sqlite_version
    has_fts5: Callable[[], bool] = lambda: _probe_fts5()

    @property
    def is_windows(self) -> bool:
        return self.platform.startswith("win")


def _find_under_program_dirs(env: Environment, relative: Iterable[str]) -> Optional[str]:
    for base in env.program_dirs:
        for rel in relative:
            candidate = os.path.join(base, rel)
            if env.exists(candidate):
                return candidate
    return None


def find_tesseract(env: Environment) -> Optional[str]:
    return env.which("tesseract") or _find_under_program_dirs(env, [os.path.join("Tesseract-OCR", "tesseract.exe")])


def find_libreoffice(env: Environment) -> Optional[str]:
    found = env.which("soffice") or env.which("libreoffice")
    return found or _find_under_program_dirs(env, [os.path.join("LibreOffice", "program", "soffice.exe")])


def find_ms_office(env: Environment) -> Optional[str]:
    if not env.is_windows:
        return None
    names = [os.path.join("Microsoft Office", "root", "Office16", "WINWORD.EXE"),
             os.path.join("Microsoft Office", "Office16", "WINWORD.EXE")]
    return _find_under_program_dirs(env, names)


def run_checks(env: Optional[Environment] = None, data_dir: Optional[os.PathLike | str] = None,
               never_write: Iterable[os.PathLike | str] = (), allow_synced: bool = False) -> List[Check]:
    env = env or Environment()
    checks: List[Check] = []
    add = lambda name, status, detail="": checks.append(Check(name, status, detail))

    py = ".".join(str(p) for p in env.python_version)
    if tuple(env.python_version) >= MIN_PYTHON:
        add("python", OK, py)
    else:
        add("python", FAIL, f"{py} is too old; need {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")

    if env.has_fts5():
        add("sqlite fts5", OK, f"SQLite {env.sqlite_version}")
    else:
        add("sqlite fts5", FAIL, f"SQLite {env.sqlite_version} lacks FTS5, which the index needs")

    add("tkinter", OK if env.can_import("tkinter") else WARN,
        "" if env.can_import("tkinter") else "window interface unavailable (Linux: install python3-tk); the command line still works")

    if data_dir is None:
        add("data folder", INFO, "not checked (none given)")
    else:
        try:
            check_allowed(data_dir, never_write, allow_synced)
            add("data folder", OK, str(data_dir))
        except DataDirError as exc:
            add("data folder", FAIL, exc.reason)

    rapid = env.can_import("rapidocr")
    tess = find_tesseract(env)
    add("ocr: rapidocr", OK if rapid else WARN, "" if rapid else "not installed (pip install rapidocr onnxruntime)")
    if rapid:
        card = env.ocr_card()
        add("ocr: graphics card", INFO, f"can be used: {card} (turn on with ocr_gpu = true, or dugup ocr --gpu)" if card else
            "not usable with the OCR runtime installed (optional; on Windows: pip uninstall onnxruntime, "
            "then pip install onnxruntime-directml)")
    add("ocr: tesseract", OK if tess else WARN, tess or "not found")
    if not rapid and not tess:
        add("ocr engine", FAIL, "no OCR engine available; scans and photos cannot be read")
    elif not rapid or not tess:
        add("ocr engine", WARN, "only one OCR engine; full power mode needs both")
    else:
        add("ocr engine", OK, "both engines available")

    soffice = find_libreoffice(env)
    add("libreoffice", OK if soffice else WARN,
        soffice or "not found; Office files fall back to native text readers (no page images)")
    office = find_ms_office(env)
    add("microsoft office", OK if office else INFO, office or ("not found" if env.is_windows else "not applicable on this system"))

    pdf_lib = env.can_import("pypdfium2")
    add("pdf reader", OK if pdf_lib else FAIL,
        "pypdfium2" if pdf_lib else "pypdfium2 is not installed (pip install pypdfium2); PDF files cannot be read")
    add("watchdog", OK if env.can_import("watchdog") else WARN,
        "" if env.can_import("watchdog") else "not installed; live updates off, scans still work")
    add("rapidfuzz", OK if env.can_import("rapidfuzz") else WARN,
        "" if env.can_import("rapidfuzz") else "not installed; fuzzy matching off")
    return checks


def has_failure(checks: Iterable[Check]) -> bool:
    return any(c.status == FAIL for c in checks)


def render(checks: Iterable[Check]) -> str:
    lines = []
    for c in checks:
        label = f"[{c.status:>4}]"
        lines.append(f"{label} {c.name}" + (f": {c.detail}" if c.detail else ""))
    return "\n".join(lines)
