"""Moving a file to the trash (the Recycle Bin, or a cloud drive's own trash), never deleting outright.

  to_trash(path)   -> "" when done, else why not
  how()            -> what to_trash would do on this machine, in words

On Windows the shell is asked to do it with "allow undo", which is what Explorer does: a file on a cloud
drive then goes to that drive's trash, and any other to the Recycle Bin. Elsewhere, the `send2trash`
package is used when it is installed. When neither way exists, the file is MOVED to a folder named
"DugUp removed" at the top of its drive (or beside it), which is not a deletion either, and said as such.
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Callable, Optional, Tuple

FALLBACK_FOLDER = "DugUp removed"


def _windows_trash(path: str) -> str:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    FOF_WANTNUKEWARNING = 0x4000     # where the shell cannot recycle and would delete outright, it asks first (overrides NOCONFIRMATION for that)
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0\0"       # a list of names ends with two NULs
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0:
        return f"the shell refused to move it to the trash (code {result})"
    if op.fAnyOperationsAborted:
        return "the shell stopped part way"
    if os.path.exists(path):
        return "the shell said it was done, but the file is still there"
    return ""


def _send2trash(path: str) -> str:
    try:
        from send2trash import send2trash
    except ImportError:
        return "no way to reach the trash here (the send2trash package is not installed)"
    try:
        send2trash(path)
    except Exception as exc:
        return f"the trash refused it ({type(exc).__name__}: {exc})"
    return ""


_run_started = time.strftime("%Y-%m-%d %H-%M-%S")     # this program's run: its own folder, so runs never overwrite each other


def _top_of(path: str) -> Tuple[str, str]:
    """(the top of the drive the path is on, the rest of the path below it)."""
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    return ((drive + os.sep) if drive else os.sep), rest.lstrip("/\\")


def _fallback_move(path: str) -> str:
    top, rest = _top_of(path)
    folder = os.path.join(top, FALLBACK_FOLDER, _run_started)
    target = os.path.join(folder, rest)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.lexists(target):            # the same path twice in one run: keep both
            stem, ext = os.path.splitext(target)
            n = 1
            while os.path.lexists(f"{stem} ({n}){ext}"):
                n += 1
            target = f"{stem} ({n}){ext}"
        shutil.move(path, target)
    except OSError as exc:
        return f"could not be moved aside ({type(exc).__name__}: {exc})"
    return ""


def how() -> str:
    if os.name == "nt":
        return "moved to the trash by the Windows shell (a cloud drive's own trash, or the Recycle Bin), as Explorer does"
    try:
        import send2trash  # noqa: F401
        return "moved to the trash by the send2trash package"
    except ImportError:
        return f"MOVED to a folder named \"{FALLBACK_FOLDER}\" at the top of the drive (no trash can be reached here)"


def to_trash(path: str, mover: Optional[Callable[[str], str]] = None) -> str:
    """Move one file to the trash. "" when done, else why not. Nothing is deleted outright by this program;
    on Windows the shell is told to ask the person first where it could only delete (a drive with no
    recycle bin), rather than do it silently."""
    if not os.path.isfile(path):
        return "it is not there (or it is a folder)"
    if mover is not None:
        return mover(path)
    if os.name == "nt":
        return _windows_trash(path)
    try:
        import send2trash  # noqa: F401
        return _send2trash(path)
    except ImportError:
        return _fallback_move(path)
