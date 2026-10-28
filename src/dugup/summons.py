"""Reaching the one running tray process from another: "show the search box", "open the window", "quit".

The tray listens on a port of the loopback interface chosen by the system, and writes the port and a secret
to a small file in the data folder; only a program that can read that file (this user) can send a command.
A command is one line: the secret, a space, the word. The answer is one line. Nothing else is sent, and
nothing about the user's files passes this way.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

FILE = "tray.json"
COMMANDS = ("ping", "version", "popup", "window", "update", "pause", "resume", "stop", "status", "state", "models", "free", "load", "about", "quit")


def address_file(root: Path | str) -> Path:
    return Path(root) / FILE


class Listener:
    """Accepts commands on a thread of its own and hands each to `handle`, which returns the answer."""

    def __init__(self, root: Path | str, handle: Callable[[str], str]) -> None:
        self.file = address_file(root)
        self.handle = handle
        self.token = secrets.token_hex(16)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.closed = False
        self.thread = threading.Thread(target=self._serve, name="dugup-summons", daemon=True)

    def start(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file, "w", encoding="utf-8") as handle:
            json.dump({"port": self.port, "token": self.token, "pid": os.getpid()}, handle)
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass
        self.thread.start()

    def _serve(self) -> None:
        while not self.closed:
            try:
                link, _ = self.sock.accept()
            except OSError:
                return
            with link:
                try:
                    link.settimeout(2.0)
                    line = _read_line(link)
                    answer = "no"
                    token, _, word = line.partition(" ")
                    if token == self.token and word in COMMANDS:
                        answer = self.handle(word) or "ok"
                    link.sendall((answer + "\n").encode("utf-8"))
                except OSError:
                    pass

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)   # wakes the thread waiting in accept; close alone does not
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if self.thread.is_alive():
            self.thread.join(2.0)
        try:
            if self.file.exists() and json.loads(self.file.read_text(encoding="utf-8")).get("port") == self.port:
                self.file.unlink()
        except (OSError, ValueError):
            pass


def _read_line(link: socket.socket) -> str:
    data = b""
    while not data.endswith(b"\n") and len(data) < 4096:
        piece = link.recv(256)
        if not piece:
            break
        data += piece
    return data.decode("utf-8", "replace").strip()


def summon(root: Path | str, command: str, timeout: float = 3.0) -> Optional[str]:
    """Send one command to the running tray. The answer, or None when no tray is running (a stale address
    file from a tray that died is removed)."""
    path = address_file(root)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as link:
            link.sendall(f"{token} {command}\n".encode("utf-8"))
            return _read_line(link) or "ok"
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        return None
