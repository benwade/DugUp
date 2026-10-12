"""Getting the optional model and its runtime: the one part of the program that uses the internet.

Plain-words search needs two things nobody can be assumed to have: a program that runs a language model
(llama.cpp's `llama-server`) and a model file. `dugup models install` (or the button in the window)
fetches both into the program's data folder, and from then on the program finds them by itself.

Rules this module keeps:
- Nothing is downloaded unless the person asked for it, having been shown what, how large and from where.
- Exact files only. Each download is pinned to one address, one size and one SHA-256 checksum, written
  below. A file that does not match is deleted and refused, whatever the server sent.
- Only download requests leave the machine: no account, no identifier, nothing about the person's files.
- Interruptible and resumable: a stopped or broken download keeps what it has (`name.part`) and the next
  run asks the server for the rest.
- Room is checked first.

To move to a newer runtime or model, change the pins here (address, size, checksum) and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .indexer import Progress
from .protocols import StopSignal
from .shapes import Failure, FailureReason

CHUNK = 1024 * 256
AGENT = "DugUp (file search; fetching its optional model)"
RECORD = "installed.json"          # in the models folder: what was installed, and where


@dataclass(frozen=True)
class Pinned:
    """One exact file on the internet."""

    name: str
    url: str
    size: int
    sha256: str
    what: str                  # said to the person before anything is downloaded
    licence: str = ""


# llama.cpp, build b11429: the build its own release v0.6.0 (2026-10-05) names as the one to use.
RUNTIME_TAG = "b11429"
_RUNTIME_BASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_TAG}/llama-{RUNTIME_TAG}-bin-"
_RUNTIME_WHAT = "llama.cpp's model server (runs the model on the processor)"
_RUNTIME_LICENCE = "MIT licence, https://github.com/ggml-org/llama.cpp/blob/master/LICENSE"
RUNTIMES: Dict[Tuple[str, str], Pinned] = {
    ("windows", "x64"): Pinned("win-cpu-x64.zip", _RUNTIME_BASE + "win-cpu-x64.zip", 19398918,
                               "1283323272b04cd07905816a597a0da810918102de958f4ff6f7bbaa70ed2efe", _RUNTIME_WHAT, _RUNTIME_LICENCE),
    ("windows", "arm64"): Pinned("win-cpu-arm64.zip", _RUNTIME_BASE + "win-cpu-arm64.zip", 12229035,
                                 "ee0f631a9e58b146ff50714099d9cb498906af3143a773b580093a9214a8d1e5", _RUNTIME_WHAT, _RUNTIME_LICENCE),
    ("linux", "x64"): Pinned("ubuntu-x64.tar.gz", _RUNTIME_BASE + "ubuntu-x64.tar.gz", 17693462,
                             "f6d25dde8f51133143d1453da4fd5f73b145127177612a283bf7995957af3392", _RUNTIME_WHAT, _RUNTIME_LICENCE),
    ("linux", "arm64"): Pinned("ubuntu-arm64.tar.gz", _RUNTIME_BASE + "ubuntu-arm64.tar.gz", 13682895,
                               "ed44c0f79c3d02424f62bdc87e106318b0b89f49050e2e80c5f352723d14e00b", _RUNTIME_WHAT, _RUNTIME_LICENCE),
    ("macos", "arm64"): Pinned("macos-arm64.tar.gz", _RUNTIME_BASE + "macos-arm64.tar.gz", 11971406,
                               "740288ec6887be94280a5dfa25b5e23a78285cab104519e6c7e218904ee82459", _RUNTIME_WHAT, _RUNTIME_LICENCE),
    ("macos", "x64"): Pinned("macos-x64.tar.gz", _RUNTIME_BASE + "macos-x64.tar.gz", 11487431,
                             "29ac3ea02be6bd143e824973f2cc5fa74bc4094393a9eaab0ff6814f19dd8522", _RUNTIME_WHAT, _RUNTIME_LICENCE),
}

# The small function-calling model, 8-bit file, at one fixed revision of the repository that publishes it.
MODEL = Pinned(
    "functiongemma-270m-it-Q8_0.gguf",
    "https://huggingface.co/unsloth/functiongemma-270m-it-GGUF/resolve/1de0f7fc7cae0062012c2e91ef02e642c161aa57/"
    "functiongemma-270m-it-Q8_0.gguf",
    291558624, "8a17a4eda2b28eaf685a487129fd5295eeff3f722b9c6451dd3d98c18c23a98d",
    "a small language model that reads one sentence and fills in the search form",
    "Gemma Terms of Use, https://ai.google.dev/gemma/terms (by downloading you accept them)")


# The mid-size model, 4-bit file. Slower and far larger, and much better at reading a sentence. The times are an
# estimate for a processor with no graphics card, not a measurement: see EXPERIMENT_LOG.
LARGER = Pinned(
    "Phi-3.5-mini-instruct-Q4_K_M.gguf",
    "https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/resolve/6d70da17e749a471ccb62ade694486011a75cda3/"
    "Phi-3.5-mini-instruct-Q4_K_M.gguf",
    2393232672, "e4165e3a71af97f1b4820da61079826d8752a2088e313af0c7d346796c38eff5",
    "a mid-size language model that reads one sentence and fills in the search form (needs about 3 GB of memory; "
    "without a graphics card expect several seconds a sentence, more on an old machine)",
    "MIT licence, https://huggingface.co/microsoft/Phi-3.5-mini-instruct/blob/main/LICENSE")

SMALL_CHOICE, LARGER_CHOICE = "small", "larger"
MODELS: Dict[str, Pinned] = {SMALL_CHOICE: MODEL, LARGER_CHOICE: LARGER}
# Seconds one sentence is given when this model was installed (the setting intent_seconds can only raise it).
PATIENCE: Dict[str, float] = {SMALL_CHOICE: 30.0, LARGER_CHOICE: 180.0}


def this_machine(system: Optional[str] = None, machine: Optional[str] = None) -> Tuple[str, str]:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    name = {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(system, system)
    cpu = {"amd64": "x64", "x86_64": "x64", "x64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    return name, cpu


def megabytes(count: float) -> str:
    return f"{count / (1024 * 1024):,.0f} MB"


@dataclass(frozen=True)
class Plan:
    """What an install would download, for this machine."""

    runtime: Pinned
    model: Pinned
    folder: Path
    choice: str = SMALL_CHOICE

    @property
    def items(self) -> Tuple[Pinned, ...]:
        return (self.runtime, self.model)

    @property
    def total(self) -> int:
        return sum(item.size for item in self.items)

    def describe(self) -> str:
        lines = ["Plain-words search needs two downloads (nothing else is sent or fetched):"]
        for item in self.items:
            host = item.url.split("/")[2]
            lines.append(f"  - {item.what}: {megabytes(item.size)} from {host}")
            lines.append(f"      {item.licence}")
        lines.append(f"Together {megabytes(self.total)}, kept in {self.folder}")
        lines.append("Each file is checked against a known checksum and refused if it differs.")
        return "\n".join(lines)


def plan_for(folder: Path, system: Optional[str] = None, machine: Optional[str] = None,
             choice: str = SMALL_CHOICE) -> "Plan | Failure":
    if choice not in MODELS:
        return Failure(FailureReason.OTHER, f"there is no model called {choice!r}; the choices are: " + ", ".join(MODELS))
    key = this_machine(system, machine)
    runtime = RUNTIMES.get(key)
    if runtime is None:
        return Failure(FailureReason.UNSUPPORTED,
                       f"there is no ready-made model server for this kind of machine ({key[0]} {key[1]}). "
                       "Plain-words search can still use a server you run yourself: set intent_url in config.toml")
    return Plan(runtime, MODELS[choice], Path(folder), choice)


# ---- downloading --------------------------------------------------------------------------------------------

def _sha256(path: Path, stop: Optional[StopSignal] = None) -> Optional[str]:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if stop is not None and stop.is_set():
                return None
            block = handle.read(CHUNK * 4)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def fetch(item: Pinned, folder: Path, progress: Optional[Callable[[Progress], None]] = None,
          stop: Optional[StopSignal] = None, opener: Callable[..., Any] = urllib.request.urlopen,
          timer: Callable[[], float] = time.monotonic, seconds: float = 60.0) -> "Path | Failure":
    """Download one pinned file into `folder`, resuming a part that is already there. Returns its path,
    or why not. The finished file exists under its real name only once its size and checksum are right."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    final, part = folder / item.name, folder / (item.name + ".part")
    if final.exists():
        if final.stat().st_size == item.size and _sha256(final, stop) == item.sha256:
            return final
        if stop is not None and stop.is_set():
            return Failure(FailureReason.STOPPED, "stopped while a file was being checked")
        final.unlink()                      # not the file that is wanted: fetch it again
    have = part.stat().st_size if part.exists() else 0
    if have > item.size:                    # more than the whole file: not a part of this file
        part.unlink()
        have = 0
    started = timer()

    def tell(done: int) -> None:
        if progress is not None:
            progress(Progress("download", item.name, done, item.size, elapsed=timer() - started, current=item.name))

    if have < item.size:
        request = urllib.request.Request(item.url, headers={"User-Agent": AGENT})
        if have:
            request.add_header("Range", f"bytes={have}-")
        try:
            with opener(request, timeout=seconds) as response:
                status = int(getattr(response, "status", 200) or 200)
                if have and status != 206:      # the server sent the whole file again: start over
                    have = 0
                with open(part, "ab" if have else "wb") as out:
                    tell(have)
                    while True:
                        if stop is not None and stop.is_set():
                            return Failure(FailureReason.STOPPED, "stopped; what was downloaded is kept and will be continued")
                        block = response.read(CHUNK)
                        if not block:
                            break
                        out.write(block)
                        have += len(block)
                        if have > item.size:
                            break
                        tell(have)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and part.exists():      # "nothing after that point": the part is stale
                part.unlink()
            return Failure(FailureReason.UNREACHABLE, f"{item.url.split('/')[2]} refused the download (HTTP {exc.code})")
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return Failure(FailureReason.UNREACHABLE,
                           f"the download from {item.url.split('/')[2]} did not finish ({reason}); "
                           "what arrived is kept, run it again to continue")
    size = part.stat().st_size if part.exists() else 0
    if size != item.size:
        if size > item.size:
            part.unlink()
            return Failure(FailureReason.CORRUPT, f"{item.name} is larger than it should be; it was deleted")
        return Failure(FailureReason.UNREACHABLE, f"{item.name} arrived only in part ({megabytes(size)} of "
                                                  f"{megabytes(item.size)}); run it again to continue")
    found = _sha256(part, stop)
    if found is None:
        return Failure(FailureReason.STOPPED, "stopped while the download was being checked; it is kept")
    if found != item.sha256:
        part.unlink()
        return Failure(FailureReason.CORRUPT,
                       f"{item.name} is not the file that was expected (its checksum differs); it was deleted and not used")
    os.replace(part, final)
    return final


# ---- unpacking ----------------------------------------------------------------------------------------------

def _inside(folder: Path, name: str) -> Optional[Path]:
    """Where an archive member would land, or None when it would land outside the folder."""
    if not name or name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        return None
    target = (folder / name).resolve()
    root = folder.resolve()
    return target if target == root or root in target.parents else None


def unpack(archive: Path, folder: Path) -> "Path | Failure":
    """Unpack a .zip or .tar.gz into an empty folder. An archive that tries to write outside it is refused whole."""
    folder = Path(folder)
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if _inside(folder, member.filename) is None:
                        raise ValueError(f"a file in the archive points outside it ({member.filename!r})")
                bundle.extractall(folder)
        else:
            with tarfile.open(archive, "r:*") as bundle:
                members = bundle.getmembers()
                for member in members:
                    if _inside(folder, member.name) is None:
                        raise ValueError(f"a file in the archive points outside it ({member.name!r})")
                    if member.issym() or member.islnk():
                        link = os.path.join(os.path.dirname(member.name), member.linkname) if member.issym() else member.linkname
                        if _inside(folder, link) is None:
                            raise ValueError(f"a link in the archive points outside it ({member.name!r})")
                    elif not (member.isfile() or member.isdir()):
                        raise ValueError(f"the archive holds something that is neither a file nor a folder ({member.name!r})")
                if hasattr(tarfile, "data_filter"):
                    bundle.extractall(folder, members=members, filter="data")
                else:      # an older Python: the checks above are the protection
                    bundle.extractall(folder, members=members)
    except (ValueError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return Failure(FailureReason.CORRUPT, f"the runtime archive could not be unpacked ({exc})")
    return folder


def find_server(folder: Path) -> Optional[Path]:
    """The model server program inside an unpacked runtime."""
    for name in ("llama-server.exe", "llama-server"):
        found = sorted(Path(folder).rglob(name))
        if found:
            program = found[0]
            if os.name != "nt":
                program.chmod(program.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return program
    return None


# ---- install, status, remove --------------------------------------------------------------------------------

def read_record(folder: Path) -> Optional[Mapping[str, str]]:
    """What is installed, when it is all still there: {"server": path, "model_file": path, ...}. Else None."""
    try:
        data = json.loads((Path(folder) / RECORD).read_text(encoding="utf-8"))
        if os.path.isfile(data["server"]) and os.path.isfile(data["model_file"]):
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def install(folder: Path, progress: Optional[Callable[[Progress], None]] = None, stop: Optional[StopSignal] = None,
            opener: Callable[..., Any] = urllib.request.urlopen, plan: Optional[Plan] = None,
            free_bytes: Optional[Callable[[], Optional[int]]] = None, keep_free: int = 0) -> "Mapping[str, str] | Failure":
    """Download, check and unpack everything in the plan. Safe to run again: finished files are kept."""
    folder = Path(folder)
    chosen = plan or plan_for(folder)
    if isinstance(chosen, Failure):
        return chosen
    folder.mkdir(parents=True, exist_ok=True)
    needed = 0
    for item in chosen.items:
        done = folder / item.name
        part = folder / (item.name + ".part")
        if not (done.exists() and done.stat().st_size == item.size):
            needed += item.size - (part.stat().st_size if part.exists() else 0)
    needed += 4 * chosen.runtime.size            # the unpacked runtime, generously
    free = free_bytes() if free_bytes is not None else None
    if free is not None and free < needed + keep_free:
        return Failure(FailureReason.OTHER, f"there is not enough room: about {megabytes(needed)} is needed in {folder} "
                                            f"and {megabytes(free)} is free")
    paths: List[Path] = []
    for item in chosen.items:
        got = fetch(item, folder, progress, stop, opener)
        if isinstance(got, Failure):
            return got
        paths.append(got)
    if progress is not None:
        progress(Progress("unpack", chosen.runtime.name, 0, 1))
    runtime_folder = unpack(paths[0], folder / f"runtime-{RUNTIME_TAG}")
    if isinstance(runtime_folder, Failure):
        return runtime_folder
    server = find_server(runtime_folder)
    if server is None:
        return Failure(FailureReason.CORRUPT, "the runtime archive holds no llama-server program")
    record = {"server": str(server), "model_file": str(paths[1]), "runtime": RUNTIME_TAG, "model": chosen.model.name,
              "model_sha256": chosen.model.sha256, "choice": chosen.choice,
              "seconds": PATIENCE.get(chosen.choice, PATIENCE[SMALL_CHOICE])}
    temporary = folder / (RECORD + ".new")
    temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(temporary, folder / RECORD)
    return record


def remove(folder: Path) -> int:
    """Delete everything in the install folder (it holds nothing but what an install put there).
    Returns the bytes given back."""
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    freed = 0
    for entry in list(folder.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            freed += sum(f.stat().st_size for f in entry.rglob("*") if f.is_file() and not f.is_symlink())
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                freed += entry.lstat().st_size
                entry.unlink()
            except OSError:
                pass
    return freed
