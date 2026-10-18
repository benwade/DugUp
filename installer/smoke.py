"""Install the built DugUp-Setup silently and use it the way a person would, then upgrade and uninstall.

Run on Windows after build.py and ISCC (GitHub Actions does):
    python installer/smoke.py installer/build/out/DugUp-Setup-<version>.exe

Every step checks what it should and stops with the reason when it does not. Timings for the graphics
runtime are printed as information: the CI machine has no graphics card, so they show the fallback.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def step(title: str) -> None:
    print(f"\n=== {title}", flush=True)


def run(*args, check: bool = True, timeout: float = 1800) -> str:
    print("+", " ".join(str(a) for a in args), flush=True)
    done = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    output = (done.stdout or "") + (done.stderr or "")
    print(output.rstrip())
    if check and done.returncode != 0:
        sys.exit(f"FAILED ({done.returncode}): {' '.join(str(a) for a in args)}")
    return output


def expect(condition: bool, what: str) -> None:
    if not condition:
        sys.exit(f"FAILED: {what}")
    print(f"ok: {what}")


def wait_until(condition, what: str, limit: float = 300) -> None:
    """Wait for a condition (the uninstaller finishes in a copy of itself, after the first one returns)."""
    deadline = time.monotonic() + limit
    while not condition():
        if time.monotonic() > deadline:
            sys.exit(f"FAILED: {what} (not after {limit:.0f} seconds)")
        time.sleep(1)
    print(f"ok: {what}")


def marker_text(app: Path) -> str:
    try:
        return (app / "dugup-data-folder.txt").read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def install(setup: Path, folder: Path, log: Path, *extra: str) -> None:
    """Install silently; on failure, show the setup log, which says why."""
    log.parent.mkdir(parents=True, exist_ok=True)       # Setup will not start if it cannot write its log
    print("+", setup, "(silent install into", folder, ")", flush=True)
    done = subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                           f"/DIR={folder}", f"/LOG={log}", *extra], timeout=1800)
    if done.returncode != 0:
        try:
            print(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            print("(no setup log was written)")
        sys.exit(f"FAILED: the installer ended with code {done.returncode}")


ASSISTANT = r"""
import anyio, json, sys
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

async def main():
    params = StdioServerParameters(command=sys.argv[1], args=json.loads(sys.argv[2]))
    with anyio.fail_after(240):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                print("TOOLS", ",".join(sorted(t.name for t in listed.tools)))
                found = await session.call_tool("search", {"query": "zebrafish"})
                print("ERROR" if found.is_error else "OK", found.content[0].text[:600])

anyio.run(main)
"""

PICTURE = r"""
import sys
from PIL import Image, ImageDraw, ImageFont
image = Image.new("RGB", (900, 260), "white")
draw = ImageDraw.Draw(image)
try:
    font = ImageFont.truetype("arial.ttf", 110)
except OSError:
    font = ImageFont.load_default(size=110)
draw.text((40, 60), "HARBOUR", fill="black", font=font)
image.save(sys.argv[1])
"""

TIMING = r"""
import time, numpy, onnxruntime as ort, pathlib, rapidocr
print("providers:", ort.get_available_providers())
models = [p for p in pathlib.Path(rapidocr.__file__).parent.rglob("*.onnx") if "det" in p.name.lower()]
if not models:
    print("no detection model found to time")
else:
    model = str(models[0])
    for providers in (["CPUExecutionProvider"], ["DmlExecutionProvider", "CPUExecutionProvider"]):
        try:
            session = ort.InferenceSession(model, providers=providers)
            feed = {session.get_inputs()[0].name: numpy.zeros((1, 3, 640, 640), numpy.float32)}
            session.run(None, feed)                       # the first run includes setting up
            started = time.perf_counter()
            for _ in range(3):
                session.run(None, feed)
            print(providers[0], "->", session.get_providers()[0], ":", round((time.perf_counter() - started) / 3, 3), "s a page")
        except Exception as exc:
            print(providers[0], "could not be used:", type(exc).__name__, exc)
"""


def main() -> int:
    setup = Path(sys.argv[1]).resolve()
    root = Path(tempfile.mkdtemp(prefix="dugup smoke "))      # a space in the path, on purpose
    logs = Path(tempfile.mkdtemp(prefix="dugup logs "))       # outside the folder that gets indexed
    app = root / "Programs" / "DugUp"
    corpus = root / "My files"
    corpus.mkdir(parents=True)

    step("Install, silently, with no downloads")
    install(setup, app, logs / "install.log")
    python, pythonw = app / "python" / "python.exe", app / "python" / "pythonw.exe"
    expect(python.is_file() and pythonw.is_file(), "the bundled Python is installed")
    expect((app / "dugup-data-folder.txt").is_file(), "the marker sits beside the program")
    expect("data = data" in marker_text(app), "by default the index goes in the data folder inside the program's")
    expect(not (app / "data").exists(), "no data folder until DugUp first runs")
    dugup = [python, "-m", "dugup"]

    step("The window toolkit works in the bundled Python")
    run(python, "-c", "import tkinter; r = tkinter.Tk(); r.withdraw(); r.update(); r.destroy(); print('tk', tkinter.TkVersion)")

    step("The Start menu entries")
    programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    expect((programs / "DugUp.lnk").is_file(), "DugUp (the window) is in the Start menu")
    expect((programs / "DugUp Remote.lnk").is_file(), "DugUp Remote is in the Start menu")
    expect((programs / "Report a problem with DugUp.lnk").is_file(), "Report a problem is in the Start menu")
    run(*dugup, "remote", "--help")

    step("Report a problem gathers its report (printed here; the window does the same)")
    reported = run(*dugup, "report", "--print", timeout=120)
    expect("## DugUp" in reported and "the Windows installer" in reported and "## Health check" in reported,
           "the report names the version, how it was installed, and the health check")

    step("dugup doctor")
    report = run(*dugup, "doctor", check=False)
    expect("sqlite fts5" in report and "[FAIL] sqlite" not in report, "the index's full-text search is available")
    expect("[  ok] tkinter" in report, "doctor sees the window toolkit")
    expect("[  ok] ocr: rapidocr" in report, "the OCR engine is installed")
    card_line = next((line for line in report.splitlines() if "ocr: graphics card" in line), "")
    expect("none found" in card_line, "no graphics card is claimed on a machine that has none")

    step("The data folder is beside the program")
    where = run(*dugup, "data", "where")
    expect(str(app / "data") in where, "data where names the folder inside the install")

    step("Index files, read a picture's words, never index the program itself")
    (corpus / "notes.txt").write_text("A note about zebrafish and their stripes.", encoding="utf-8")
    run(python, "-c", PICTURE, corpus / "sign.png")
    run(*dugup, "roots", "add", root)                     # the whole test folder: the install is inside it
    run(*dugup, "index", "--no-meaning", timeout=1800)
    run(*dugup, "ocr", timeout=1800)
    found = run(*dugup, "search", "zebrafish")
    expect("notes.txt" in found, "a document's words are found")
    found = run(*dugup, "search", "harbour")
    expect("sign.png" in found, "the words in a picture are found (OCR through the bundled runtime)")
    found = run(*dugup, "search", "antigravity")           # a file name only the bundled Python has
    expect(str(app / "python").lower() not in found.lower() and "antigravity.py" not in found,
           "the bundled Python is never indexed")

    step("The graphics runtime on a machine without a card (information)")
    run(python, "-c", TIMING, check=False)

    step("Download one model the way the installer would, and search by meaning")
    run(*dugup, "config", "set", "meaning_model", "small")
    listing = run(*dugup, "download", "--list")
    expect("Search by meaning" in listing and "Pictures by likeness" in listing, "download --list names the parts")
    run(*dugup, "download", "--only", "meaning", "--yes", timeout=1800)
    run(*dugup, "meaning", "fill", "--no-gpu", timeout=1800)
    found = run(*dugup, "search", "--meaning", "fish with stripes")
    expect("notes.txt" in found, "search by meaning works on the processor")

    step("An assistant searches over MCP (dugup mcp), read-only, once it is turned on")
    expect("is off" in run(*dugup, "mcp", "status"), "off until turned on")
    claude = logs / "claude_desktop_config.json"
    run(*dugup, "mcp", "enable", "--yes")
    run(*dugup, "mcp", "connect", "--write", "--config", claude)
    entry = json.loads(claude.read_text(encoding="utf-8"))["mcpServers"]["dugup"]
    expect(entry["command"].lower().endswith("python.exe"), "the entry starts the console Python, which has standard input and output")
    answer = run(python, "-c", ASSISTANT, entry["command"], json.dumps(entry["args"]), timeout=300)
    expect("TOOLS passages,search,status" in answer and "notes.txt" in answer, "the tools answer a real MCP client")
    run(*dugup, "mcp", "disable")

    step("Upgrade over the top: the data stays")
    install(setup, app, logs / "upgrade.log")
    expect((app / "data" / "index.sqlite").is_file(), "the index survives an upgrade")
    found = run(*dugup, "search", "zebrafish")
    expect("notes.txt" in found, "and still answers")

    step("Uninstall silently: the program goes, the data stays")
    uninstaller = sorted(app.glob("unins*.exe"))
    expect(bool(uninstaller), "an uninstaller is there")
    run(uninstaller[-1], "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={logs / 'uninstall.log'}")
    try:
        wait_until(lambda: not (app / "python").exists(), "the program's files are removed")
    finally:
        leftover = sorted(str(p.relative_to(app)) for p in app.rglob("*") if p.is_file() and "data" not in p.parts)
        print("left in the program folder:", leftover[:20], "..." if len(leftover) > 20 else "")
        try:
            print((logs / "uninstall.log").read_text(encoding="utf-8", errors="replace")[-3000:])
        except OSError:
            print("(no uninstall log)")
    expect((app / "data" / "index.sqlite").is_file(), "a silent uninstall keeps the data")
    expect(not (app / "dugup-data-folder.txt").exists(), "the marker goes with the program")

    step("Install with the index in a folder of its own (/INDEXDIR, the page 'Where DugUp keeps its index')")
    other = root / "Programs" / "DugUp elsewhere"
    index = root / "Index kept apart"
    install(setup, other, logs / "install-indexdir.log", f"/INDEXDIR={index}")
    expect(f"data = {index}" in marker_text(other), "the marker names the chosen folder")
    python2 = other / "python" / "python.exe"
    where = run(python2, "-m", "dugup", "data", "where")
    expect(str(index) in where and "not a guarantee" in where, "data where names it, with the sync caveat")
    run(python2, "-m", "dugup", "init")
    expect((index / "index.sqlite").is_file(), "the index is made there")
    expect(not (other / "data").exists(), "and not inside the program's folder")
    install(setup, other, logs / "upgrade-indexdir.log")
    expect(f"data = {index}" in marker_text(other), "an upgrade keeps the chosen folder")
    uninstaller = sorted(other.glob("unins*.exe"))
    run(uninstaller[-1], "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={logs / 'uninstall2.log'}")
    wait_until(lambda: not (other / "python").exists(), "the program's files are removed")
    expect((index / "index.sqlite").is_file(), "a silent uninstall keeps the index in its own folder")
    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    if os.name != "nt":
        sys.exit("This tries the Windows installer; run it on Windows.")
    sys.exit(main())
