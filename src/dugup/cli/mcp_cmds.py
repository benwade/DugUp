"""The `mcp` command: read-only search for an assistant such as Claude (assistant.py, mcpserver.py).

    dugup mcp enable [--folders "C:\\Docs;D:\\Notes"]   turn it on, after saying what it means
    dugup mcp disable                                  turn it off (at once, even while one is connected)
    dugup mcp status                                   on or off, which folders, how to connect
    dugup mcp connect [--write]                        the entry for the Claude desktop app; --write adds it
    dugup mcp serve                                    what the assistant app starts (standard input and output)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from .. import catalogue
from .. import settings as settings_module
from ..assistant import folders_allowed
from ..shapes import Failure
from .common import _layout, _service
from .output import safe_write

PRIVACY = ("Turning this on lets an AI assistant on this computer (such as the Claude desktop app) search your index, "
           "read-only: it can never change, move or delete anything. What it is shown, file names, paths and the "
           "passages that match, is sent to the assistant's service, as a file you attach to a conversation would be. "
           "Your files themselves are not sent; the assistant sees only what a search returns.")
SERVER_NAME = "dugup"


def claude_config_path(environ: Mapping[str, str], platform: Optional[str] = None,
                       home: Optional[Path] = None) -> Path:
    """Where the Claude desktop app keeps its list of MCP servers."""
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


def server_entry(args, environ: Mapping[str, str], python: Optional[str] = None) -> Dict[str, object]:
    command = ["-m", "dugup", "mcp", "serve"]
    data_dir = args.data_dir or environ.get("DUGUP_DATA_DIR")
    if data_dir:
        command += ["--data-dir", os.path.abspath(data_dir)]
    return {"command": console_python(python), "args": command}


def _change(args, environ, out, changes: Dict[str, str]) -> "settings_module.Settings | None":
    layout = _layout(args, environ, create=True)
    current, _ = settings_module.load(layout.config)
    new = catalogue.apply(current, changes)
    if isinstance(new, Failure):
        safe_write(f"Not changed: {new.detail}", out)
        return None
    catalogue.write(layout.config, {name: getattr(new, name) for name in changes})
    return new


def _enable(args, environ, out, ask: Callable[[str], str]) -> int:
    safe_write(PRIVACY, out)
    changes = {"mcp_enabled": "true"}
    if args.folders is not None:
        folders = folders_allowed(args.folders) or []
        missing = [f for f in folders if not os.path.isdir(f)]
        if missing:
            safe_write("Not changed: these folders do not exist: " + "; ".join(missing), out)
            return 1
        changes["mcp_folders"] = ";".join(folders)
    if not args.yes:
        if ask("Turn it on? [y/N] ").strip().lower() not in ("y", "yes"):
            safe_write("Nothing was changed.", out)
            return 0
    new = _change(args, environ, out, changes)
    if new is None:
        return 1
    _say_folders(new, out)
    safe_write("Turned on. To connect the Claude desktop app: dugup mcp connect --write (then restart the app).", out)
    return 0


def _say_folders(settings, out) -> None:
    folders = folders_allowed(settings.mcp_folders)
    if folders is None:
        safe_write("Assistants may search every indexed folder. To limit them: "
                   'dugup mcp enable --folders "FOLDER;FOLDER"', out)
    else:
        safe_write("Assistants may search only: " + "; ".join(folders), out)


def _status(args, environ, out) -> int:
    layout = _layout(args, environ, create=False)
    current, _ = settings_module.load(layout.config)
    safe_write(f"Assistant search is {'ON' if current.mcp_enabled else 'off'}.", out)
    if current.mcp_enabled:
        _say_folders(current, out)
    path = claude_config_path(environ)
    connected = False
    try:
        connected = SERVER_NAME in json.loads(path.read_text(encoding="utf-8-sig")).get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        pass
    safe_write(f"The Claude desktop app {'has' if connected else 'does not have'} the DugUp entry ({path}).", out)
    return 0


def _connect(args, environ, out) -> int:
    entry = server_entry(args, environ)
    snippet = json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2)
    path = Path(args.config) if args.config else claude_config_path(environ)
    if not args.write:
        safe_write(f"Add this to the Claude desktop app's settings file ({path}), or run: dugup mcp connect --write", out)
        safe_write(snippet, out)
        return 0
    data: Dict[str, object] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except ValueError:
            safe_write(f"{path} is not valid JSON, so it was not changed. Add this to it by hand:", out)
            safe_write(snippet, out)
            return 1
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            safe_write(f"{path} is not laid out as expected, so it was not changed. Add this to it by hand:", out)
            safe_write(snippet, out)
            return 1
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    servers = data.setdefault("mcpServers", {})
    servers[SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    safe_write(f"Added DugUp to {path}" + (" (the old file is kept beside it as .bak)." if path.with_name(path.name + ".bak").exists() else "."), out)
    safe_write("Restart the Claude desktop app (quit it from its tray icon, then open it) to connect.", out)
    layout = _layout(args, environ, create=False)
    current, _ = settings_module.load(layout.config)
    if not current.mcp_enabled:
        safe_write("Assistant search is still off: the tools will say so until you run dugup mcp enable.", out)
    return 0


def _serve(args, environ) -> int:
    from ..mcpserver import serve
    import importlib.util
    if importlib.util.find_spec("mcp") is None:
        safe_write("The MCP library is not installed here: pip install mcp", sys.stderr)
        return 1
    service = _service(args, environ, sys.stderr, create=False)
    if service is None:
        return 1
    layout = service.layout

    def load_settings():
        return settings_module.load(layout.config)[0]

    with service:
        serve(service, load_settings)
    return 0


def cmd_mcp(args, env, out, environ, ask: Callable[[str], str] = input) -> int:
    what = args.mcp_command
    if what == "serve":
        return _serve(args, environ)
    if what == "enable":
        return _enable(args, environ, out, ask)
    if what == "disable":
        new = _change(args, environ, out, {"mcp_enabled": "false"})
        if new is None:
            return 1
        safe_write("Turned off. A connected assistant is told so on its next request.", out)
        return 0
    if what == "status":
        return _status(args, environ, out)
    return _connect(args, environ, out)
