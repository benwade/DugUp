"""Small things the window remembers between runs (the folder it was set to, say), kept as one JSON file in
the data folder. Settings are what the user decides; this is what they were last doing. Nothing here is
ever needed: a missing or damaged file is an empty memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

FILE = "window.json"


def read(root: Path | str) -> Dict[str, Any]:
    try:
        data = json.loads((Path(root) / FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write(root: Path | str, **values: Any) -> None:
    """Change these values, keep the rest."""
    kept = read(root)
    kept.update(values)
    try:
        path = Path(root) / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(kept, indent=1), encoding="utf-8")
    except OSError:
        pass
