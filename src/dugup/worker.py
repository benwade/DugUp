"""The worker: a separate process that does risky work for the Sandbox (CONTRACTS.md part 7).

Started as `python -m dugup.worker`. It reads one request a line (JSON) from its standard input, runs
the named job, and writes one answer a line. It never opens the index. A job is named "module:function";
the function takes the payload (a mapping) and returns something JSON can carry.

Before the first request it:
- keeps its real output for the answers and points ordinary printing at the error stream, so a
  library that prints cannot corrupt the conversation;
- cuts itself off from the network: always at the Python level (opening a socket raises), and on Linux
  also in the kernel when the system allows an unprivileged network namespace;
- limits its own memory when asked (systems without a Job Object);
- arranges to end if the program that started it goes away.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import traceback
from typing import Any, BinaryIO, Callable, Dict

NETWORK_REFUSED = "this worker has no network access"


class NoNetwork(OSError):
    pass


def block_python_sockets() -> None:
    import socket

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise NoNetwork(NETWORK_REFUSED)

    class Refusing(socket.socket):   # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            refuse()

    socket.socket = Refusing          # type: ignore[misc,assignment]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse       # type: ignore[assignment]


def leave_the_network() -> str:
    """Returns "namespace" when the kernel now refuses the network for this process, else "python"."""
    level = "python"
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            uid, gid = os.getuid(), os.getgid()
            libc = ctypes.CDLL(None, use_errno=True)
            new_user, new_net = 0x10000000, 0x40000000
            if libc.unshare(new_user | new_net) == 0:
                level = "namespace"
                try:   # keep our own user id inside the new namespace, so files are still ours to read and write
                    with open("/proc/self/setgroups", "w") as handle:
                        handle.write("deny")
                    with open("/proc/self/uid_map", "w") as handle:
                        handle.write(f"{uid} {uid} 1")
                    with open("/proc/self/gid_map", "w") as handle:
                        handle.write(f"{gid} {gid} 1")
                except OSError:
                    pass
        except Exception:
            pass
    block_python_sockets()
    return level


def limit_own_memory(megabytes: int) -> None:
    try:
        import resource
        amount = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (amount, amount))
    except (ImportError, OSError, ValueError):
        pass


def end_with_parent() -> None:
    """A worker must not outlive the program that started it. Windows does this with the Job Object."""
    if os.name == "nt":
        return
    parent = os.getppid()
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            ctypes.CDLL(None).prctl(1, 9)   # PR_SET_PDEATHSIG, SIGKILL
        except Exception:
            pass

    def watch() -> None:
        gone = threading.Event()
        while not gone.wait(1.0):
            if os.getppid() != parent:
                os._exit(3)

    threading.Thread(target=watch, name="dugup-parent-watch", daemon=True).start()


def resolve(name: str) -> Callable[[Dict[str, Any]], Any]:
    module, _, function = name.partition(":")
    if not module or not function:
        raise ValueError(f"a job is named module:function, not {name!r}")
    return getattr(importlib.import_module(module), function)


def answer(out: BinaryIO, message: Dict[str, Any]) -> None:
    out.write(json.dumps(message).encode("ascii") + b"\n")   # ASCII with escapes: no encoding to get wrong
    out.flush()


def serve(inp: BinaryIO, out: BinaryIO) -> int:
    for line in inp:
        if not line.strip():
            continue
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            answer(out, {"ok": False, "kind": "protocol", "detail": "a request was not valid JSON"})
            continue
        if request.get("job") == "dugup.worker:quit":
            return 0
        try:
            result = resolve(request["job"])(request.get("payload") or {})
            answer(out, {"id": request.get("id"), "ok": True, "result": result})
        except MemoryError:
            # after running out of memory this process is not to be trusted with another job
            answer(out, {"id": request.get("id"), "ok": False, "kind": "out_of_memory",
                         "detail": "the job needed more memory than the worker is allowed"})
            return 4
        except BaseException as exc:   # a job's failure is an answer, not the end of the worker
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            detail = f"{type(exc).__name__}: {exc}"
            kind = "no_network" if isinstance(exc, NoNetwork) else "error"
            answer(out, {"id": request.get("id"), "ok": False, "kind": kind, "detail": detail,
                         "trace": traceback.format_exc(limit=6)})
    return 0


def hello(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The built-in job that says what this worker is."""
    return {"pid": os.getpid(), "network": _STATE.get("network", "unknown"), "python": sys.version.split()[0]}


_STATE: Dict[str, str] = {}


def main(argv: list) -> int:
    memory = 0
    for arg in argv:
        if arg.startswith("--memory-mb="):
            memory = int(arg.split("=", 1)[1])
    out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())    # printing by any library now goes to the error stream
    inp = os.fdopen(os.dup(sys.stdin.fileno()), "rb")
    end_with_parent()
    network = leave_the_network()
    import dugup.worker as importable    # run with -m this file is "__main__"; jobs see it under its real name
    importable._STATE["network"] = _STATE["network"] = network
    if memory and os.name != "nt":
        limit_own_memory(memory)
    answer(out, {"ready": True, "pid": os.getpid(), "network": network})
    try:
        return serve(inp, out)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
