"""What DugUp says about itself: the About box of the window, the search box and the tray."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import __version__

AUTHOR = "Ben Wade"
HOME = "https://github.com/benwade/DugUp"
LICENSE = "PolyForm Noncommercial License 1.0.0"
LICENSE_URL = "https://polyformproject.org/licenses/noncommercial/1.0.0"


def about_text(data_folder: Optional[Path | str] = None) -> str:
    lines = [f"DugUp {__version__}",
             "",
             "Local file search: by the words in files, the words in pictures and scans, what a passage means, and "
             "what a picture looks like. Everything runs on this machine and nothing you have leaves it.",
             "",
             f"Copyright (c) 2026 {AUTHOR}. {LICENSE}: use it, copy it, change it and share it for any noncommercial "
             f"purpose, with this notice kept; selling it, or using it in something sold, needs a separate agreement "
             f"with the author. {LICENSE_URL}",
             "",
             HOME]
    if data_folder:
        lines += ["", f"Data folder: {data_folder}"]
    return "\n".join(lines)
