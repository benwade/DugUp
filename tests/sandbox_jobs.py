"""Jobs for the Sandbox tests. They run inside a worker process, never in the test process."""

import os
import subprocess
import sys
import threading

import helpers


def echo(payload):
    return {"got": payload, "pid": os.getpid(), "accents": f"{helpers.CAFE} {helpers.CJK} {helpers.EMOJI}"}


def _mark(payload):
    """Tell the test this job is under way (the test waits for the file, not for a length of time)."""
    with open(payload["marker"], "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


def hang(payload):
    _mark(payload)
    threading.Event().wait()      # for ever: only being ended gets this job out


def spin(payload):
    _mark(payload)
    while True:                   # busy for ever, never yielding to anything
        pass


def boom(payload):
    raise RuntimeError(f"job bug {helpers.CAFE}")


def die(payload):
    os._exit(int(payload.get("code", 7)))    # the worker itself ends, as a native crash would end it


def hog(payload):
    blocks = []
    while True:
        blocks.append(bytearray(16 * 1024 * 1024))


def chatter(payload):
    print("a library prints to standard output")       # must not corrupt the conversation with the parent
    sys.stdout.write("{\"ok\": true, \"result\": \"forged\"}\n")
    sys.stdout.flush()
    return "the real answer"


def family(payload):
    """Start a child that starts a grandchild; both wait for ever. Report their ids, then hang."""
    grandchild = "import threading; threading.Event().wait()"
    child = ("import os, subprocess, sys, threading; "
             "p = subprocess.Popen([sys.executable, '-c', %r], creationflags=0x08000000 if os.name == 'nt' else 0); "
             "print(p.pid, flush=True); threading.Event().wait()" % grandchild)
    quiet = 0x08000000 if os.name == "nt" else 0     # no console window for the child of a windowless worker
    process = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                               creationflags=quiet)
    grand = int(process.stdout.readline())
    with open(payload["marker"], "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {process.pid} {grand}")
    threading.Event().wait()


def connect(payload):
    import socket
    socket.create_connection(("127.0.0.1", int(payload.get("port", 9))), timeout=2)
    return "connected"


def raw_socket(payload):
    import socket
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    return "made a socket"


def unserialisable(payload):
    return object()


def hang_quietly(payload):
    threading.Event().wait()      # stuck for ever, with nothing to tell the test


def answer_nonsense(payload):
    return {"neither": "a result", "nor": "a failure"}
