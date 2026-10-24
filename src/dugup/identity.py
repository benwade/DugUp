"""Who DugUp is, to Windows: its app ID and its icon.

Without these, Windows names DugUp's windows after the program that runs them (Python): in the taskbar,
when pinned, and in lists of recent programs. `dress(root)` is called on every root window DugUp makes;
it never fails (an identity is a nicety, never a reason not to open).

The installer's shortcuts carry the same app ID (installer/dugup.iss), so a pinned DugUp and a running one
are one taskbar button.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

APP_ID = "DugUp.DugUp"
ICON = Path(__file__).with_name("dugup.ico")


def claim_app_id(platform: Optional[str] = None) -> bool:
    """Tell Windows that this process is DugUp. True when it was told."""
    if not (platform or sys.platform).startswith("win"):
        return False
    try:
        import ctypes
        return ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID) == 0
    except Exception:
        return False


def icon_image(size: int = 64):
    """The icon as a Pillow image of about this size (the nearest size the icon file holds), or None."""
    try:
        from PIL import Image
        with Image.open(ICON) as found:
            sizes = sorted(found.info.get("sizes") or [found.size])
            found.size = min(sizes, key=lambda s: (abs(s[0] - size), -s[0]))
            return found.convert("RGBA")
    except Exception:
        return None


def dress(root, platform: Optional[str] = None) -> None:
    """Give a root window (and every window it opens) DugUp's identity and icon."""
    platform = platform or sys.platform
    claim_app_id(platform)
    try:
        if platform.startswith("win"):
            root.iconbitmap(default=str(ICON))           # default=: every window of this root, dialogs too
            return
        image = icon_image(64)
        if image is not None:
            from PIL import ImageTk
            photo = ImageTk.PhotoImage(image, master=root)
            root.iconphoto(True, photo)
            root._dugup_icon = photo                     # kept, or Tk shows an empty icon once it is collected
    except Exception:
        pass
