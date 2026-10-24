"""Assemble what the Windows installer installs, in installer/build/DugUp:

    DugUp/
        dugup-data-folder.txt    the marker: data beside the program; the bundled Python is never indexed
        python/                  a standalone Python (with tkinter) and every package DugUp uses
            DugUp.exe            DugUp's own program file (installer/launcher), beside pythonw.exe
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


def compiler_environment() -> dict:
    """The environment the Microsoft C compiler needs, from Visual Studio's vcvars64.bat (found with vswhere)."""
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        sys.exit("Visual Studio's C compiler is needed to build DugUp.exe (vswhere.exe was not found).")
    found = run(str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property", "installationPath").strip().splitlines()
    if not found:
        sys.exit("Visual Studio's C compiler is needed to build DugUp.exe (no installation with it was found).")
    vcvars = Path(found[-1]) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    dump = subprocess.run(f'"{vcvars}" >nul && set', shell=True, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if dump.returncode != 0:
        sys.exit(f"vcvars64.bat failed: {dump.stderr}")
    env = {}
    for line in dump.stdout.splitlines():
        name, eq, value = line.partition("=")
        if eq:
            env[name] = value
    return env


def build_launcher(python_dir: Path, version: str, work: Path) -> Path:
    """Compile installer/launcher into python_dir/DugUp.exe, with DugUp's icon and version, for the DLL of
    the Python that was unpacked there."""
    dlls = sorted(p.name for p in python_dir.glob("python3*.dll") if p.name.lower() != "python3.dll")
    if len(dlls) != 1:
        sys.exit(f"Expected one versioned Python DLL in {python_dir}, found {dlls}")
    numbers = (version.split("+")[0].split("-")[0].split(".") + ["0", "0", "0"])[:3]
    numbers = [n if n.isdigit() else "0" for n in numbers]
    source = HERE / "launcher"
    work.mkdir(parents=True, exist_ok=True)
    rc = (source / "launcher.rc.in").read_text(encoding="utf-8")
    shutil.copyfile(REPO / "src" / "dugup" / "dugup.ico", work / "dugup.ico")     # beside the .rc: a plain name
    for key, value in (("@ICON@", "dugup.ico"), ("@VERSION@", version),
                       ("@V1@", numbers[0]), ("@V2@", numbers[1]), ("@V3@", numbers[2])):
        rc = rc.replace(key, value)
    (work / "launcher.rc").write_text(rc, encoding="utf-8")
    env = compiler_environment()

    search = next((value for name, value in env.items() if name.upper() == "PATH"), "")

    def tool(*args: str) -> None:
        print("+", " ".join(args), flush=True)
        # Windows looks a program up on this script's own PATH, not the one handed to it: find it on the compiler's
        program = shutil.which(args[0], path=search)
        if program is None:
            sys.exit(f"{args[0]} was not found on the compiler's PATH")
        done = subprocess.run([program, *args[1:]], cwd=work, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        if done.returncode != 0:
            print(done.stdout, done.stderr, sep="\n")
            sys.exit(f"Failed ({done.returncode}): {' '.join(args)}")

    tool("rc", "/nologo", "/fo", "launcher.res", "launcher.rc")
    exe = python_dir / "DugUp.exe"
    tool("cl", "/nologo", "/O2", "/W4", "/WX", "/utf-8", f'/DPYTHON_DLL=L"{dlls[0]}"', str(source / "launcher.c"),
         "launcher.res", f"/Fe{exe}", "/link", "/SUBSYSTEM:WINDOWS", "user32.lib", "shell32.lib")
    return exe


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
    launcher = build_launcher(python_dir, found["version"], out / "launcher")
    print(run(str(launcher), "-c", "import sys, dugup; print('DugUp.exe runs', sys.version.split()[0], 'with dugup', dugup.__version__)"))
    size = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file())
    print(f"Built DugUp {found['version']} in {stage} ({size_text(size)} before compression).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
