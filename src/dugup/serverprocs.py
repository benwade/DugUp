"""DugUp servers running for an assistant (`dugup mcp serve`), and which of them the Claude app's settings do
not name.

The Claude desktop app starts its DugUp server when it opens and keeps it until it closes. When its settings
change while it runs, or a server outlives the app, the server that answers is not the one the settings name,
and the settings file alone cannot show that: the file says "this DugUp" while an older copy answers. This
looks at the running processes themselves. Read-only: it never ends a process, it only says which one to end.
"""

from __future__ import annotations

import json
import ntpath
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

LIST_SECONDS = 20.0          # listing processes takes about a second; give up long before anyone waits on it

# Windows: one PowerShell call. Single quotes only (doubled inside), so no double quote has to survive the trip
# through the command line; UTF-8 out, since paths may hold any letter.
def windows_query(like: str) -> str:
    """The PowerShell listing of the processes whose command line is `like` (a WQL pattern, % for any text)."""
    return ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -Filter 'CommandLine like ''" + like + "''' | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress")


SERVER_LIKE = "%mcp%serve%"
WINDOWS_QUERY = windows_query(SERVER_LIKE)


@dataclass(frozen=True)
class Server:
    """One running `dugup mcp serve`. `program` is the program its command line starts (for a virtual
    environment, the environment's python.exe, even when the process itself is the Python it hands over to)."""

    pid: int
    program: str
    args: Tuple[str, ...]
    command_line: str


def split_command_line(text: str) -> List[str]:
    """The parts of a command line: double quotes group, backslashes are kept as they are (Windows paths).
    Good enough to compare command lines, not to rebuild one."""
    parts: List[str] = []
    current: List[str] = []
    quoted = False
    started = False
    for char in text or "":
        if char == '"':
            quoted = not quoted
            started = True
        elif char.isspace() and not quoted:
            if started:
                parts.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
    if started:
        parts.append("".join(current))
    return parts


def is_server(parts: Sequence[str]) -> bool:
    """True for `... dugup mcp serve ...` (as `-m dugup` or a `dugup` program), and nothing else."""
    lowered = [p.lower() for p in parts]     # ntpath: a Windows command line splits on \ and / alike, on any system
    for i in range(len(lowered) - 1):
        if lowered[i] == "mcp" and lowered[i + 1] == "serve":
            before = lowered[:i]
            return any(p == "dugup" or ntpath.basename(p) in ("dugup", "dugup.exe") for p in before)
    return False


def _server(pid: int, command_line: str) -> Optional[Server]:
    parts = split_command_line(command_line)
    if not parts or not is_server(parts):
        return None
    return Server(int(pid), parts[0], tuple(parts[1:]), command_line)


def command_lines(like: str, run: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
                  platform: Optional[str] = None) -> Optional[List[Tuple[int, str]]]:
    """(process id, command line) of the processes running now (on Windows only those whose command line is
    `like`), or None when the processes could not be listed: then nothing is known, which is not none."""
    platform = platform or sys.platform
    try:
        if platform.startswith("win"):
            done = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", windows_query(like)],
                       capture_output=True, encoding="utf-8", errors="replace", timeout=LIST_SECONDS,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            done = run(["ps", "-axo", "pid=,args="], capture_output=True, encoding="utf-8", errors="replace",
                       timeout=LIST_SECONDS)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    return windows_rows(done.stdout) if platform.startswith("win") else ps_rows(done.stdout)


def windows_rows(text: str) -> Optional[List[Tuple[int, str]]]:
    """PowerShell's answer: nothing (no match), one object, or a list of them. None when it cannot be read."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        found = json.loads(text)
    except ValueError:
        return None
    rows: List[Tuple[int, str]] = []
    for row in found if isinstance(found, list) else [found]:
        if not isinstance(row, dict):
            continue
        try:
            rows.append((int(row.get("ProcessId")), str(row.get("CommandLine") or "")))
        except (TypeError, ValueError):
            continue
    return rows


def ps_rows(text: str) -> List[Tuple[int, str]]:
    """`ps -axo pid=,args=`: a number, then the command line (unquoted, so a path with spaces splits apart)."""
    rows: List[Tuple[int, str]] = []
    for line in (text or "").splitlines():
        pid, _, command_line = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), command_line.strip()))
    return rows


def _servers(rows: Optional[Sequence[Tuple[int, str]]]) -> Optional[List[Server]]:
    if rows is None:
        return None
    return [s for s in (_server(pid, line) for pid, line in rows) if s is not None]


def find(run: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
         platform: Optional[str] = None) -> Optional[List[Server]]:
    """The DugUp servers running now, or None when the processes could not be listed (then nothing is known,
    which is not the same as none running)."""
    return _servers(command_lines(SERVER_LIKE, run, platform))


def parse_windows(text: str) -> Optional[List[Server]]:
    """PowerShell's answer, read as servers."""
    return _servers(windows_rows(text))


def parse_ps(text: str) -> List[Server]:
    """`ps`'s answer, read as servers. A path with spaces splits apart; it is still found, and still told apart
    from the settings, which is all that is needed here."""
    return _servers(ps_rows(text)) or []


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def matches(server: Server, entry: Mapping[str, object]) -> bool:
    """Whether a running server is the one this settings entry ({"command": ..., "args": [...]}) starts."""
    command = str(entry.get("command") or "")
    args = [str(a) for a in (entry.get("args") or [])]
    if not command or not _same_path(server.program, command) or len(server.args) != len(args):
        return False
    return all(os.path.normcase(a) == os.path.normcase(b) for a, b in zip(server.args, args))


def not_named(servers: Sequence[Server], entry: Optional[Mapping[str, object]]) -> List[Server]:
    """The running servers the settings entry does not start: the ones answering in its place."""
    if not entry:
        return []
    return [s for s in servers if not matches(s, entry)]


def advice(stale: Sequence[Server], platform: Optional[str] = None) -> str:
    """What to say about servers the settings do not name, and how to end them. Empty when there are none."""
    if not stale:
        return ""
    platform = platform or sys.platform
    pids = [s.pid for s in stale]
    lines = ["A DugUp server is still running that the Claude app's settings do not name. The app keeps using "
             "it until it is gone, so it may search the wrong index or none:"]
    lines += [f"  process {s.pid}: {s.command_line}" for s in stale]
    stop = (f"Stop-Process -Id {', '.join(map(str, pids))}" if platform.startswith("win")
            else f"kill {' '.join(map(str, pids))}")
    lines.append("Quit the Claude app from its tray icon. If it is still listed after that, end it (" + stop +
                 "), then open the Claude app again.")
    return "\n".join(lines)


def short_advice(stale: Sequence[Server]) -> str:
    """The same, on one line, for a window's status line."""
    if not stale:
        return ""
    pids = ", ".join(str(s.pid) for s in stale)
    noun = "process" if len(stale) == 1 else "processes"
    return (f"Another DugUp server is still running ({noun} {pids}) and the Claude app uses it instead: quit the "
            "Claude app from its tray icon; if it is still running, end it, then reopen the app.")
