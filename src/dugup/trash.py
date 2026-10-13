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
from typing import Callable, Optional

FALLBACK_FOLDER = "DugUp removed"


def _windows_trash(path: str) -> str:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0\0"       # a list of names ends with two NULs
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
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


def _fallback_move(path: str) -> str:
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    top = (drive + os.sep) if drive else os.sep
    folder = os.path.join(top, FALLBACK_FOLDER)
    target = os.path.join(folder, rest.lstrip("/\\"))
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
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
    """Move one file to the trash. "" when done, else why not. Never deletes outright."""
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
