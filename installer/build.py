"""Assemble what the Windows installer installs, in installer/build/DugUp:

    DugUp/
        dugup-data-folder.txt    the marker: data beside the program; the bundled Python is never indexed
        python/                  a standalone Python (with tkinter) and every package DugUp uses
        data/                    made on first run, never part of the installer

Also writes installer/build/defines.iss: the version and the download sizes the installer's pages show,
computed from DugUp's own pinned files so they cannot drift from what is really downloaded.

Run on Windows (GitHub Actions does): python installer/build.py
The Python download is pinned to one address and one SHA-256, and refused if it differs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# python-build-standalone 20261013: CPython 3.13.16, the "install_only" build, which includes tkinter and Tcl/Tk.
PYTHON_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261013/"
              "cpython-3.13.16%2B20261013-x86_64-pc-windows-msvc-install_only.tar.gz")
PYTHON_SHA256 = "e1f9233e780fbe951e531b0eb207d794e1c3fb39d523a6f481f4fc01ef2b6ca5"

# Every package, as ready-made wheels only (nothing is compiled). onnxruntime-directml in place of
# onnxruntime: the same runtime, plus a way to NVIDIA, AMD and Intel cards; DugUp uses a card only when
# a speed test on that computer shows it clearly faster.
PACKAGES = ["platformdirs", "pypdfium2>=4.30", "rapidocr", "onnxruntime-directml", "pillow", "numpy",
            "tokenizers", "pystray", "mcp>=2.3,<3"]

PRUNE = ("Lib/test", "Lib/idlelib/idle_test", "Lib/tkinter/test", "include", "libs")

MARKER = """# DugUp keeps its index, settings, logs and records in the folder named on the "data" line, beside this file.
# "skip" lines name the program's own folders, which are never indexed.
data = data
skip = python
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_python(cache: Path) -> Path:
    archive = cache / "python.tar.gz"
    if archive.is_file() and sha256(archive) == PYTHON_SHA256:
        return archive
    cache.mkdir(parents=True, exist_ok=True)
    print(f"Downloading the standalone Python from {PYTHON_URL.split('/')[2]} ...", flush=True)
    with urllib.request.urlopen(PYTHON_URL, timeout=120) as response, open(archive, "wb") as out:
        shutil.copyfileobj(response, out)
    got = sha256(archive)
    if got != PYTHON_SHA256:
        archive.unlink()
        sys.exit(f"The Python download's checksum is {got}, not the pinned {PYTHON_SHA256}; refusing it.")
    return archive


def unpack_python(archive: Path, stage: Path) -> Path:
    """The archive holds one folder, python/; it becomes stage/python. Nothing may land outside it."""
    target = stage / "python"
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.startswith("python/")]
        for member in members:
            dest = (stage / member.name).resolve()
            if not str(dest).startswith(str(stage.resolve()) + os.sep):
                sys.exit(f"The Python archive has a path outside its folder: {member.name}")
        if sys.version_info >= (3, 12):
            tar.extractall(stage, members=members, filter="data")
        else:
            tar.extractall(stage, members=members)
    for rel in PRUNE:
        shutil.rmtree(target / rel, ignore_errors=True)
    for pdb in target.rglob("*.pdb"):
        pdb.unlink()
    return target


def run(*args: str) -> str:
    print("+", " ".join(args), flush=True)
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        print(done.stdout, done.stderr, sep="\n")
        sys.exit(f"Failed ({done.returncode}): {' '.join(args)}")
    return done.stdout


SIZES = """
import json, sys
from dugup import __version__, embedding_models as m
total = lambda items: sum(i.size for i in items)
print(json.dumps({"version": __version__,
                  "meaning": total(m.BY_CHOICE["larger"]), "meaning_small": total(m.BY_CHOICE["small"]),
                  "looks": total(m.LOOKS_BY_CHOICE["base"]), "captions": total(m.CAPTIONS_BY_CHOICE["base"])}))
"""

CHECK = """
import onnxruntime, tkinter, dugup, rapidocr, PIL, numpy, tokenizers, pystray, pypdfium2, mcp
print("runtime providers:", onnxruntime.get_available_providers())
print("dugup", dugup.__version__, "from", dugup.__file__)
"""


def size_text(count: int) -> str:
    """As DugUp says it (downloads.size_text): 1 MB = 1,048,576 bytes, GB from 1 GB up."""
    if count >= 1024 ** 3:
        return f"{count / 1024 ** 3:,.1f} GB"
    return f"{count / (1024 * 1024):,.0f} MB"


def defines(python: Path, out: Path) -> dict:
    found = json.loads(run(str(python), "-c", SIZES).strip().splitlines()[-1])
    everything = found["meaning"] + found["looks"] + found["captions"]
    author = "DugUp"
    try:
        import tomllib
        author = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["authors"][0]["name"]
    except Exception:
        pass
    lines = [
        f'#define AppVersion "{found["version"]}"',
        f'#define AppPublisher "{author}"',
        f'#define SizeMeaning "{size_text(found["meaning"])}"',
        f'#define SizeMeaningSmall "{size_text(found["meaning_small"])}"',
        f'#define SizeLooks "{size_text(found["looks"])}"',
        f'#define SizeCaptions "{size_text(found["captions"])}"',
        f'#define SizeEverything "{size_text(everything)}"',
        f'#define BytesEverythingMB "{everything // (1024 * 1024)}"',
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(HERE / "build"), help="where to build (default installer/build)")
    args = parser.parse_args()
    if os.name != "nt":
        sys.exit("The installer's contents are built on Windows (the Python and the wheels are Windows ones).")
    out = Path(args.out)
    stage = out / "DugUp"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    python_dir = unpack_python(fetch_python(out / "cache"), stage)
    python = python_dir / "python.exe"
    pip = [str(python), "-m", "pip", "--disable-pip-version-check", "--no-input"]
    if subprocess.run([*pip, "--version"], capture_output=True).returncode != 0:
        run(str(python), "-m", "ensurepip", "--default-pip")
    run(*pip, "install", "--no-warn-script-location", "--only-binary=:all:", *PACKAGES)
    run(*pip, "install", "--no-warn-script-location", "--no-deps", str(REPO))
    (stage / "dugup-data-folder.txt").write_text(MARKER, encoding="utf-8")
    print(run(str(python), "-c", CHECK))
    found = defines(python, out / "defines.iss")
    size = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file())
    print(f"Built DugUp {found['version']} in {stage} ({size_text(size)} before compression).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
