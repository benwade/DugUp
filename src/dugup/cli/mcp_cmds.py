"""The `mcp` command: read-only search for an assistant such as Claude (assistant.py, mcpserver.py).

    dugup mcp enable [--folders "C:\\Docs;D:\\Notes"]   turn it on, after saying what it means
    dugup mcp disable                                  turn it off (at once, even while one is connected)
    dugup mcp status                                   on or off, which folders, how to connect
    dugup mcp connect [--write]                        the entry for the Claude desktop app; --write adds it
    dugup mcp serve                                    what the assistant app starts (standard input and output)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Mapping, Optional

from .. import assistantsetup as setup
from ..assistant import folders_allowed
from ..shapes import Failure
from .common import _layout, _service
from .output import safe_write

# kept here too, for anything that imported them from this module
PRIVACY = setup.PRIVACY
SERVER_NAME = setup.SERVER_NAME
console_python = setup.console_python


def claude_config_path(environ: Mapping[str, str], platform: Optional[str] = None, home: Optional[Path] = None) -> Path:
    return setup.claude_config_path(environ, platform, home)


def server_entry(args, environ: Mapping[str, str], python: Optional[str] = None):
    return setup.server_entry(args.data_dir or environ.get("DUGUP_DATA_DIR"), python)


def _change(args, environ, out, enabled=None, folders=None):
    layout = _layout(args, environ, create=True)
    new = setup.change(layout, enabled=enabled, folders=folders)
    if isinstance(new, Failure):
        safe_write(f"Not changed: {new.detail}", out)
        return None
    return new


def _enable(args, environ, out, ask: Callable[[str], str]) -> int:
    safe_write(PRIVACY, out)
    folders = None
    if args.folders is not None:
        folders = folders_allowed(args.folders) or []
        missing = [f for f in folders if not Path(f).is_dir()]
        if missing:
            safe_write("Not changed: these folders do not exist: " + "; ".join(missing), out)
            return 1
    if not args.yes:
        if ask("Turn it on? [y/N] ").strip().lower() not in ("y", "yes"):
            safe_write("Nothing was changed.", out)
            return 0
    new = _change(args, environ, out, enabled=True, folders=folders)
    if new is None:
        return 1
    _say_folders(new, out)
    safe_write("Turned on. To connect the Claude desktop app: dugup mcp connect --write (then restart the app).", out)
    return 0


def _say_folders(settings, out) -> None:
    folders = setup.folders_of(settings)
    if folders is None:
        safe_write("Assistants may search every indexed folder. To limit them: "
                   'dugup mcp enable --folders "FOLDER;FOLDER", or Choose folders... in DugUp Remote', out)
    else:
        safe_write("Assistants may search only: " + "; ".join(folders), out)


def _status(args, environ, out) -> int:
    layout = _layout(args, environ, create=False)
    current = setup.load(layout)
    safe_write(f"Assistant search is {'ON' if current.mcp_enabled else 'off'}.", out)
    if current.mcp_enabled:
        _say_folders(current, out)
    path = claude_config_path(environ)
    connected = setup.is_connected(path)
    safe_write(f"The Claude desktop app {'has' if connected else 'does not have'} the DugUp entry ({path}).", out)
    return 0


def _connect(args, environ, out) -> int:
    entry = server_entry(args, environ)
    path = Path(args.config) if args.config else claude_config_path(environ)
    if not args.write:
        safe_write(f"Add this to the Claude desktop app's settings file ({path}), or run: dugup mcp connect --write", out)
        safe_write(setup.snippet(entry), out)
        return 0
    done, said = setup.connect(path, entry)
    safe_write(said, out)
    if not done:
        return 1
    if not setup.load(_layout(args, environ, create=False)).mcp_enabled:
        safe_write("Assistant search is still off: the tools will say so until you run dugup mcp enable.", out)
    return 0


def _folders(args, environ, out) -> int:
    try:
        import tkinter
    except ImportError:
        safe_write("Choosing folders in a window needs tkinter. Use: dugup mcp enable --folders \"FOLDER;FOLDER\"", out)
        return 1
    from ..assistantwindow import FolderChooser
    layout = _layout(args, environ, create=True)
    root = tkinter.Tk()
    root.withdraw()
    chooser = FolderChooser(root, layout)
    root.wait_window(chooser.top)
    root.destroy()
    if chooser.saved is not None:
        safe_write(setup.describe_folders(chooser.saved), out)
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
        return setup.load(layout)

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
        new = _change(args, environ, out, enabled=False)
        if new is None:
            return 1
        safe_write("Turned off. A connected assistant is told so on its next request.", out)
        return 0
    if what == "status":
        return _status(args, environ, out)
    if what == "folders":
        return _folders(args, environ, out)
    return _connect(args, environ, out)
