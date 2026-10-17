"""Install the built DugUp-Setup silently and use it the way a person would, then upgrade and uninstall.

Run on Windows after build.py and ISCC (GitHub Actions does):
    python installer/smoke.py installer/build/out/DugUp-Setup-<version>.exe

Every step checks what it should and stops with the reason when it does not. Timings for the graphics
runtime are printed as information: the CI machine has no graphics card, so they show the fallback.
"""

from __future__ import annotations

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


def install(setup: Path, folder: Path, log: Path) -> None:
    """Install silently; on failure, show the setup log, which says why."""
    log.parent.mkdir(parents=True, exist_ok=True)       # Setup will not start if it cannot write its log
    print("+", setup, "(silent install into", folder, ")", flush=True)
    done = subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                           f"/DIR={folder}", f"/LOG={log}"], timeout=1800)
    if done.returncode != 0:
        try:
            print(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            print("(no setup log was written)")
        sys.exit(f"FAILED: the installer ended with code {done.returncode}")


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
    expect(not (app / "data").exists(), "no data folder until DugUp first runs")
    dugup = [python, "-m", "dugup"]

    step("The window toolkit works in the bundled Python")
    run(python, "-c", "import tkinter; r = tkinter.Tk(); r.withdraw(); r.update(); r.destroy(); print('tk', tkinter.TkVersion)")

    step("dugup doctor")
    report = run(*dugup, "doctor", check=False)
    expect("sqlite fts5" in report and "[FAIL] sqlite" not in report, "the index's full-text search is available")
    expect("[  ok] tkinter" in report, "doctor sees the window toolkit")
    expect("[  ok] ocr: rapidocr" in report, "the OCR engine is installed")
    expect("can be used: graphics card" not in report, "no graphics card is claimed on a machine that has none")

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
    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    if os.name != "nt":
        sys.exit("This tries the Windows installer; run it on Windows.")
    sys.exit(main())
