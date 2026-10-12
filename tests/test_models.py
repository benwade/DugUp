"""Fetching the optional model and its runtime. The internet is a small web server inside the test; a
download that must be caught half way is held there until the test lets it go."""

import hashlib
import io
import json
import os
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import helpers
from dugup import cli, models
from dugup.datadir import DataLayout
from dugup.intent import IntentParser, LocalRuntime, from_settings
from dugup.models import MODEL, RUNTIMES, Pinned, Plan, fetch, find_server, install, plan_for, read_record, remove, unpack
from dugup.service import LocalService
from dugup.settings import Settings
from dugup.shapes import Failure, FailureReason


class Site:
    """Serves files by path, honours Range, records what it was asked, and can hold a response part way."""

    def __init__(self):
        self.files = {}
        self.requests = []          # (path, Range header or None)
        self.ignore_range = False
        self.hold_after = None      # bytes to send before waiting for `release`
        self.release = threading.Event()
        self.sent_first_part = threading.Event()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.requests.append((self.path, self.headers.get("Range"), self.headers.get("User-Agent")))
                data = site.files.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                start = 0
                asked = self.headers.get("Range")
                if asked and not site.ignore_range:
                    start = int(asked.split("=")[1].rstrip("-"))
                    if start >= len(data):
                        self.send_error(416)
                        return
                    self.send_response(206)
                else:
                    self.send_response(200)
                body = data[start:]
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    if site.hold_after is not None:
                        self.wfile.write(body[:site.hold_after])
                        self.wfile.flush()
                        site.sent_first_part.set()
                        site.release.wait()
                        body = body[site.hold_after:]
                    self.wfile.write(body)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def pin(self, name, data, sha=None, size=None):
        self.files["/" + name] = data
        return Pinned(name, f"{self.base}/{name}", len(data) if size is None else size,
                      sha or hashlib.sha256(data).hexdigest(), f"test file {name}", "test licence")

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    made = Site()
    yield made
    made.close()


DATA = bytes(range(256)) * 4000        # about 1 MB, more than one chunk


# ---- one file ------------------------------------------------------------------------------------------------

def test_a_download_is_checked_and_reports_its_progress(site, tmp_path):
    item = site.pin(f"model {helpers.CAFE}.gguf", DATA)
    item = Pinned("model.gguf", site.pin("model.gguf", DATA).url, len(DATA), hashlib.sha256(DATA).hexdigest(), "x")
    seen = []
    path = fetch(item, tmp_path / helpers.AWKWARD_FOLDER, seen.append)
    assert path.read_bytes() == DATA and path.name == "model.gguf"
    assert not (path.parent / "model.gguf.part").exists()
    assert seen[0].done == 0 and seen[-1].done == seen[-1].total == len(DATA) and all(p.phase == "download" for p in seen)
    assert [a.done for a in seen] == sorted(a.done for a in seen)
    assert site.requests[-1][1] is None and "DugUp" in site.requests[-1][2]
    count = len(site.requests)
    assert fetch(item, path.parent) == path and len(site.requests) == count, "a finished, correct file is not fetched again"


def test_a_stopped_download_keeps_its_part_and_the_next_run_asks_only_for_the_rest(site, tmp_path):
    item = site.pin("model.gguf", DATA)
    site.hold_after = 300_000                   # the server sends this much and then waits: the rest cannot arrive early
    stop = threading.Event()
    result = fetch(item, tmp_path, progress=lambda p: stop.set() if p.done > 0 else None, stop=stop)
    assert isinstance(result, Failure) and result.reason is FailureReason.STOPPED
    site.release.set()
    part = tmp_path / "model.gguf.part"
    kept = part.stat().st_size
    assert 0 < kept < len(DATA) and not (tmp_path / "model.gguf").exists()
    site.hold_after = None
    path = fetch(item, tmp_path)
    assert path.read_bytes() == DATA
    assert site.requests[-1][1] == f"bytes={kept}-", "only the missing part was asked for"


def test_a_server_that_ignores_the_range_means_starting_over(site, tmp_path):
    item = site.pin("model.gguf", DATA)
    (tmp_path / "model.gguf.part").write_bytes(DATA[:1000])
    site.ignore_range = True
    assert fetch(item, tmp_path).read_bytes() == DATA


def test_a_part_that_is_not_part_of_this_file_is_caught_by_the_checksum(site, tmp_path):
    item = site.pin("model.gguf", DATA)
    (tmp_path / "model.gguf.part").write_bytes(b"\x00" * 5000)        # some other file's beginning
    answer = fetch(item, tmp_path)
    assert isinstance(answer, Failure) and answer.reason is FailureReason.CORRUPT
    assert not (tmp_path / "model.gguf.part").exists() and not (tmp_path / "model.gguf").exists()
    assert fetch(item, tmp_path).read_bytes() == DATA, "and the next run fetches it cleanly"


@pytest.mark.parametrize("case", ["wrong checksum", "too large", "too small"])
def test_a_file_that_is_not_the_pinned_one_is_refused(site, tmp_path, case):
    if case == "wrong checksum":
        item = site.pin("model.gguf", DATA, sha="0" * 64)
    elif case == "too large":
        item = site.pin("model.gguf", DATA, size=len(DATA) - 10)
    else:
        item = site.pin("model.gguf", DATA, size=len(DATA) + 10)
    answer = fetch(item, tmp_path)
    assert isinstance(answer, Failure) and not (tmp_path / "model.gguf").exists()
    assert answer.reason is (FailureReason.UNREACHABLE if case == "too small" else FailureReason.CORRUPT)
    assert (tmp_path / "model.gguf.part").exists() == (case == "too small"), "an incomplete part may be continued; a wrong one is deleted"


def test_a_wrong_file_under_the_real_name_is_replaced(site, tmp_path):
    item = site.pin("model.gguf", DATA)
    (tmp_path / "model.gguf").write_bytes(b"something else entirely")
    assert fetch(item, tmp_path).read_bytes() == DATA


def test_a_missing_file_and_a_missing_server_are_said_plainly(site, tmp_path):
    gone = Pinned("nothing.gguf", site.base + "/nothing.gguf", 10, "0" * 64, "x")
    answer = fetch(gone, tmp_path)
    assert answer.reason is FailureReason.UNREACHABLE and "HTTP 404" in answer.detail
    site.close()
    answer = fetch(Pinned("m.gguf", site.base + "/m.gguf", 10, "0" * 64, "x"), tmp_path)
    assert answer.reason is FailureReason.UNREACHABLE and "run it again" in answer.detail


# ---- archives ------------------------------------------------------------------------------------------------

def zip_bytes(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        for name, data in entries.items():
            bundle.writestr(name, data)
    return out.getvalue()


def tar_bytes(entries, links=()):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as bundle:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            bundle.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            bundle.addfile(info)
    return out.getvalue()


def test_archives_are_unpacked_and_the_server_program_is_found(tmp_path):
    (tmp_path / "w.zip").write_bytes(zip_bytes({"llama-b1/llama-server.exe": b"MZ", "llama-b1/llama.dll": b"x"}))
    folder = unpack(tmp_path / "w.zip", tmp_path / "win")
    assert find_server(folder) == folder / "llama-b1" / "llama-server.exe"
    (tmp_path / "l.tar.gz").write_bytes(tar_bytes({"llama-b1/llama-server": b"#!/bin/sh\n", "llama-b1/libllama.so.0.1": b"x"},
                                                  links=[("llama-b1/libllama.so", "libllama.so.0.1")]))
    folder = unpack(tmp_path / "l.tar.gz", tmp_path / "lin")
    program = find_server(folder)
    assert program == folder / "llama-b1" / "llama-server"
    if os.name != "nt":
        assert os.access(program, os.X_OK), "the program is made runnable"
        assert (folder / "llama-b1" / "libllama.so").resolve() == (folder / "llama-b1" / "libllama.so.0.1").resolve()
    assert find_server(tmp_path) is not None and find_server(tmp_path / "win" / "llama-b1" / "nowhere") is None


@pytest.mark.parametrize("kind,entries,links", [
    ("zip", {"../escaped.txt": b"x"}, ()),
    ("zip", {"/absolute.txt": b"x"}, ()),
    ("tar", {"../escaped.txt": b"x"}, ()),
    ("tar", {"ok.txt": b"x"}, [("link", "../../outside")]),
    ("tar", {"ok.txt": b"x"}, [("link", "/etc/passwd")]),
])
def test_an_archive_that_reaches_outside_its_folder_is_refused_whole(tmp_path, kind, entries, links):
    archive = tmp_path / ("a.zip" if kind == "zip" else "a.tar.gz")
    archive.write_bytes(zip_bytes(entries) if kind == "zip" else tar_bytes(entries, links))
    answer = unpack(archive, tmp_path / "out")
    assert isinstance(answer, Failure) and answer.reason is FailureReason.CORRUPT
    assert not (tmp_path / "out").exists() and not (tmp_path / "escaped.txt").exists()


def test_a_broken_archive_is_a_stated_failure(tmp_path):
    (tmp_path / "bad.zip").write_bytes(b"this is not a zip file")
    assert unpack(tmp_path / "bad.zip", tmp_path / "out").reason is FailureReason.CORRUPT


# ---- the pins and the plan -----------------------------------------------------------------------------------

def test_every_pin_is_complete_and_points_where_it_says():
    for item in list(RUNTIMES.values()) + [MODEL]:
        assert item.url.startswith("https://") and item.url.endswith(item.name)
        assert item.size > 1_000_000 and len(item.sha256) == 64 and int(item.sha256, 16) >= 0
        assert item.what and item.licence
    assert all(models.RUNTIME_TAG in item.url and "github.com/ggml-org/llama.cpp/releases/download" in item.url
               for item in RUNTIMES.values())
    assert "huggingface.co" in MODEL.url and "/resolve/1de0f7fc" in MODEL.url, "the model is pinned to one revision, not to 'main'"
    assert "Gemma Terms of Use" in MODEL.licence


@pytest.mark.parametrize("system,machine,expected", [
    ("Windows", "AMD64", "win-cpu-x64.zip"), ("Windows", "ARM64", "win-cpu-arm64.zip"), ("Linux", "x86_64", "ubuntu-x64.tar.gz"),
    ("Linux", "aarch64", "ubuntu-arm64.tar.gz"), ("Darwin", "arm64", "macos-arm64.tar.gz"), ("Darwin", "x86_64", "macos-x64.tar.gz")])
def test_the_runtime_is_chosen_for_the_machine(tmp_path, system, machine, expected):
    plan = plan_for(tmp_path, system, machine)
    assert plan.runtime.name == expected and plan.model is MODEL and plan.total == plan.runtime.size + MODEL.size
    text = plan.describe()
    assert "github.com" in text and "huggingface.co" in text and "Gemma Terms of Use" in text and "MIT licence" in text
    assert "278 MB" in text and str(tmp_path) in text and "checksum" in text


def test_a_machine_with_no_ready_made_runtime_is_told_what_still_works(tmp_path):
    answer = plan_for(tmp_path, "FreeBSD", "riscv64")
    assert isinstance(answer, Failure) and answer.reason is FailureReason.UNSUPPORTED and "intent_url" in answer.detail


# ---- the whole install ---------------------------------------------------------------------------------------

def small_plan(site, folder, runtime_entries=None):
    model = site.pin("small-model.gguf", DATA)
    if os.name == "nt":
        runtime = site.pin("rt.zip", zip_bytes(runtime_entries or {"llama-x/llama-server.exe": b"MZ stand-in"}))
    else:
        runtime = site.pin("rt.tar.gz", tar_bytes(runtime_entries or {"llama-x/llama-server": b"#!/bin/sh\n"}))
    return Plan(runtime, model, folder)


def test_install_then_status_then_remove(site, tmp_path):
    folder = tmp_path / "models" / "intent"
    plan = small_plan(site, folder)
    seen = []
    record = install(folder, seen.append, plan=plan, free_bytes=lambda: 10**12)
    assert os.path.isfile(record["server"]) and record["model_file"] == str(folder / "small-model.gguf")
    assert read_record(folder) == record and {p.phase for p in seen} == {"download", "unpack"}
    requests = len(site.requests)
    assert install(folder, plan=plan) == record and len(site.requests) == requests, "installing again downloads nothing"
    os.remove(record["server"])
    assert read_record(folder) is None, "a record whose files are gone does not count as installed"
    assert install(folder, plan=plan) == record
    freed = remove(folder)
    assert freed > len(DATA) and read_record(folder) is None and list(folder.iterdir()) == []
    assert remove(folder) == 0 and remove(tmp_path / "never") == 0


def test_install_checks_for_room_before_downloading(site, tmp_path):
    answer = install(tmp_path / "m", plan=small_plan(site, tmp_path / "m"), free_bytes=lambda: 1000, keep_free=0)
    assert isinstance(answer, Failure) and "not enough room" in answer.detail and site.requests == []
    assert not isinstance(install(tmp_path / "m", plan=small_plan(site, tmp_path / "m"), free_bytes=lambda: None), Failure), \
        "a drive that cannot be measured does not block it"


def test_an_archive_without_the_server_program_is_refused(site, tmp_path):
    entries = {"llama-x/readme.txt": b"no program here"}
    answer = install(tmp_path / "m", plan=small_plan(site, tmp_path / "m", entries))
    assert isinstance(answer, Failure) and "no llama-server" in answer.detail and read_record(tmp_path / "m") is None


def test_the_installed_model_is_used_only_when_the_model_reader_is_chosen(site, tmp_path):
    layout = DataLayout(tmp_path / "data").ensure()
    with LocalService(layout, Settings(intent_reader="model")) as service:
        assert service.intent is None and service.models_installed() is None
        assert isinstance(service.understand("pdfs about nursing"), Failure)
        result = service.install_models(plan=small_plan(site, service.models_folder))
        assert not isinstance(result, Failure)
        reader = service.intent
        assert isinstance(reader, IntentParser) and isinstance(reader.backend, LocalRuntime)
        assert reader.backend.program == result["server"] and reader.backend.model_file == result["model_file"]
        assert reader.backend._running is None, "installing starts nothing"
        assert service.remove_models() > 0 and service.intent is None
    with LocalService(layout, Settings()) as service:
        service.install_models(plan=small_plan(site, service.models_folder))
        assert type(service.intent).__name__ == "RuleReader", "by default the built-in rules read the sentence, installed model or not"
    named = from_settings(Settings(intent_reader="model", intent_url="http://127.0.0.1:1"), {"server": "s", "model_file": "m"})
    assert type(named.backend).__name__ == "ChatServer", "a server named in the settings comes before an installed one"


# ---- the command line ----------------------------------------------------------------------------------------

def run(data, *argv, **more):
    out = io.StringIO()
    code = cli.main([*argv, "--data-dir", str(data)], stdout=out, environ={}, **more)
    return code, out.getvalue()


def test_models_commands(site, tmp_path, monkeypatch):
    data = tmp_path / "data"
    assert run(data, "models", "status")[0] == 1, "no index yet: start with a folder"
    run(data, "init")
    code, text = run(data, "models", "status")
    assert code == 0 and "not installed" in text and "huggingface.co" in text and "dugup models install --yes" in text
    code, text = run(data, "models", "install")
    assert code == 1 and "Nothing was downloaded" in text and site.requests == [], "without --yes only the plan is shown"

    plan = small_plan(site, data / "models" / "intent")
    monkeypatch.setattr(models, "plan_for", lambda folder, system=None, machine=None: plan)
    code, text = run(data, "models", "install", "--yes")
    assert code == 0 and "Installed." in text and "[download] small-model.gguf" in text and "unpacking" in text
    code, text = run(data, "models", "status")
    assert "Installed: runtime" in text and "small-model.gguf" in text
    code, text = run(data, "models", "remove")
    assert code == 0 and "given back" in text
    assert "Nothing was installed" in run(data, "models", "remove")[1]


def test_a_stopped_install_says_how_to_continue(site, tmp_path, monkeypatch):
    data = tmp_path / "data"
    run(data, "init")
    plan = small_plan(site, data / "models" / "intent")
    monkeypatch.setattr(models, "plan_for", lambda folder, system=None, machine=None: plan)
    stop = threading.Event()
    stop.set()
    out = io.StringIO()
    args = cli.build_parser().parse_args(["models", "install", "--yes", "--data-dir", str(data)])
    code = cli.cmd_models(args, None, out, {}, stop=stop, install_signals=False)
    assert code == 130 and "Run `dugup models install --yes` again to continue" in out.getvalue()


def test_download_progress_line():
    from dugup.indexer import Progress
    line = cli.progress_text(Progress("download", "model.gguf", 50 * 1048576, 200 * 1048576, elapsed=10.0))
    assert line == "[download] model.gguf: 50.0 of 200.0 MB (25%)  5.0 MB/s  about 30 s left"
    assert cli.progress_text(Progress("download", "m", 0, 0)).startswith("[download] m: 0.0 of 0.0 MB (0%)  0.0 MB/s")
    assert cli.progress_text(Progress("unpack", "rt.zip", 0, 1)) == "[download] unpacking rt.zip"
