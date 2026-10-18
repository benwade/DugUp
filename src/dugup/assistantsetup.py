"""Turning assistant search on and off, choosing its folders, and connecting the Claude desktop app.

Shared by `dugup mcp` (cli/mcp_cmds.py) and DugUp Remote (remote.py, assistantwindow.py), so both say and do
the same thing. The rules themselves (what an assistant may see) live in assistant.py.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from . import catalogue
from . import settings as settings_module
from .assistant import folders_allowed
from .shapes import Failure, FailureReason

SERVER_NAME = "dugup"

# What switching it on means, said the same way everywhere.
PRIVACY = ("Turning this on lets an AI assistant on this computer (such as the Claude desktop app) search your index, "
           "read-only: it can never change, move or delete anything. What it is shown, file names, paths and the "
           "passages that match, is sent to the assistant's service, as a file you attach to a conversation would be. "
           "Your files themselves are not sent; the assistant sees only what a search returns.")
TOGGLE_NOTE = "On: Claude can search your files. What it is shown leaves this computer."
CONFIRM_TITLE = "Allow cloud search access?"
CONFIRM_TEXT = ("Claude runs in the cloud. When it searches your files, what it is shown leaves this computer and goes "
                "to Anthropic, as a file you attach to a conversation would:\n\n"
                "  - file names and full paths\n"
                "  - the passages that matched (a few hundred characters each)\n\n"
                "DugUp's promise that your files stay on your computer does not cover what Claude is shown. Claude can "
                "only search, never change, move or delete anything, and only in the folders you allow.\n\n"
                "Allow cloud search access?")


def load(layout) -> "settings_module.Settings":
    return settings_module.load(layout.config)[0]


def change(layout, enabled: Optional[bool] = None, folders: Optional[Sequence[str]] = None) -> "settings_module.Settings | Failure":
    """Save the switch and/or the folder list in config.toml. Takes effect at once, even for a connected assistant."""
    changes: Dict[str, str] = {}
    if enabled is not None:
        changes["mcp_enabled"] = "true" if enabled else "false"
    if folders is not None:
        missing = [f for f in folders if not os.path.isdir(f)]
        if missing:
            return Failure(FailureReason.UNSUPPORTED, "these folders do not exist: " + "; ".join(missing))
        changes["mcp_folders"] = ";".join(os.path.abspath(f) for f in folders)
    current = load(layout)
    new = catalogue.apply(current, changes)
    if isinstance(new, Failure):
        return new
    Path(layout.config).parent.mkdir(parents=True, exist_ok=True)
    catalogue.write(layout.config, {name: getattr(new, name) for name in changes})
    return new


def folders_of(settings) -> Optional[List[str]]:
    return folders_allowed(getattr(settings, "mcp_folders", ""))


def describe_folders(settings) -> str:
    folders = folders_of(settings)
    if folders is None:
        return "Every indexed folder"
    return f"{len(folders)} folder" + ("" if len(folders) == 1 else "s") + ": " + "; ".join(folders)


# ---- the Claude desktop app -------------------------------------------------------------------------------------

def claude_config_path(environ: Optional[Mapping[str, str]] = None, platform: Optional[str] = None,
                       home: Optional[Path] = None) -> Path:
    """Where the Claude desktop app keeps its list of MCP servers."""
    environ = os.environ if environ is None else environ
    platform = platform or sys.platform
    home = home or Path.home()
    if platform.startswith("win"):
        base = Path(environ.get("APPDATA") or home / "AppData" / "Roaming")
        return base / "Claude" / "claude_desktop_config.json"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "Claude" / "claude_desktop_config.json"


def console_python(executable: Optional[str] = None) -> str:
    """The Python that runs the server: the console one, since pythonw has no standard input and output."""
    executable = executable or sys.executable
    folder, name = os.path.split(executable)
    if name.lower() == "pythonw.exe":
        candidate = os.path.join(folder, "python.exe")
        if os.path.exists(candidate):
            return candidate
    return executable


def server_entry(data_dir: Optional[str] = None, python: Optional[str] = None) -> Dict[str, object]:
    """The settings entry that starts `dugup mcp serve`. `data_dir` only when the data folder was chosen by hand:
    otherwise the server finds it as every other DugUp command does, and keeps finding it after a move."""
    command = ["-m", "dugup", "mcp", "serve"]
    if data_dir:
        command += ["--data-dir", os.path.abspath(data_dir)]
    return {"command": console_python(python), "args": command}


def is_connected(path: Path) -> bool:
    try:
        servers = json.loads(Path(path).read_text(encoding="utf-8-sig")).get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(servers, dict) and SERVER_NAME in servers


def connect(path: Path, entry: Dict[str, object]) -> Tuple[bool, str]:
    """Add DugUp to the app's settings file, keeping everything else and a .bak copy. (done, what to say)."""
    path = Path(path)
    snippet = json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2)
    data: Dict[str, object] = {}
    backup = path.with_name(path.name + ".bak")
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except ValueError:
            return False, f"{path} is not valid JSON, so it was not changed. Add this to it by hand:\n{snippet}"
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            return False, f"{path} is not laid out as expected, so it was not changed. Add this to it by hand:\n{snippet}"
        shutil.copy2(path, backup)
    data.setdefault("mcpServers", {})[SERVER_NAME] = entry      # type: ignore[index]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    kept = " (the old file is kept beside it as .bak)" if backup.exists() else ""
    return True, f"Added DugUp to {path}{kept}. Restart the Claude desktop app (quit it from its tray icon, then open it) to connect."


def snippet(entry: Dict[str, object]) -> str:
    return json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2)
