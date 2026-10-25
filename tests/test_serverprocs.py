"""Running DugUp servers (serverprocs.py): finding them, and telling which ones the Claude app's settings do
not name. The case behind it: the settings named the installed DugUp, but a server from a source checkout,
left running, kept answering, and nothing in the settings file could show that."""

import io
import json
import subprocess

import pytest

from dugup import assistantsetup as setup
from dugup import cli, serverprocs
from dugup.cli import mcp_cmds
from dugup.serverprocs import Server, is_server, split_command_line

INSTALLED = r"C:\Users\a\AppData\Local\Programs\DugUp\python\python.exe"
CHECKOUT = r"D:\Claude\projects\digupwin-app\.venv\Scripts\python.exe"


def _done(stdout, code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr="")


# ---- reading command lines ----------------------------------------------------------------------------------

def test_a_command_line_splits_on_spaces_outside_quotes_and_keeps_backslashes():
    line = r'"C:\Program Files\DugUp\python.exe" -m dugup mcp serve --data-dir "D:\My Data"'
    assert split_command_line(line) == [r"C:\Program Files\DugUp\python.exe", "-m", "dugup", "mcp", "serve",
                                        "--data-dir", r"D:\My Data"]
    assert split_command_line("") == [] and split_command_line('  ""  x') == ["", "x"]


def test_only_dugup_mcp_serve_counts_as_a_server():
    assert is_server(["python.exe", "-m", "dugup", "mcp", "serve"])
    assert is_server([r"C:\x\dugup.exe", "mcp", "serve", "--data-dir", "d"])
    assert not is_server(["python.exe", "-m", "dugup", "mcp", "status"])
    assert not is_server(["python.exe", "-m", "other", "mcp", "serve"])
    query = split_command_line('powershell -Command "' + serverprocs.WINDOWS_QUERY + '"')
    assert not is_server(query), "the listing itself is never taken for a server"


# ---- listing ------------------------------------------------------------------------------------------------

def test_windows_answers_of_none_one_or_several_are_all_read():
    one = {"ProcessId": 43500, "CommandLine": f"{CHECKOUT} -m dugup mcp serve"}
    two = {"ProcessId": 41324, "CommandLine": f"{CHECKOUT} -m dugup mcp serve"}
    other = {"ProcessId": 7, "CommandLine": "node server.js mcp serve"}
    found = serverprocs.find(lambda *a, **k: _done(json.dumps([one, two, other])), platform="win32")
    assert [s.pid for s in found] == [43500, 41324] and found[0].program == CHECKOUT
    assert [s.pid for s in serverprocs.find(lambda *a, **k: _done(json.dumps(one)), platform="win32")] == [43500]
    assert serverprocs.find(lambda *a, **k: _done(""), platform="win32") == []


def test_when_processes_cannot_be_listed_nothing_is_claimed():
    def missing(*a, **k):
        raise FileNotFoundError("no powershell")
    assert serverprocs.find(missing, platform="win32") is None
    assert serverprocs.find(lambda *a, **k: _done("", code=1), platform="win32") is None
    assert serverprocs.find(lambda *a, **k: _done("not json"), platform="win32") is None


def test_ps_output_is_read_on_linux_and_mac():
    text = ("  101 /usr/bin/python3 -m dugup mcp serve\n"
            "  102 /usr/bin/python3 -m dugup mcp status\n"
            "  103 bash\n")
    found = serverprocs.find(lambda *a, **k: _done(text), platform="linux")
    assert [(s.pid, s.program) for s in found] == [(101, "/usr/bin/python3")]


# ---- telling them apart -------------------------------------------------------------------------------------

def test_a_server_from_another_copy_is_told_apart_from_the_named_one():
    entry = {"command": INSTALLED, "args": ["-m", "dugup", "mcp", "serve"]}
    named = Server(1, INSTALLED, ("-m", "dugup", "mcp", "serve"), "")
    leftover = Server(43500, CHECKOUT, ("-m", "dugup", "mcp", "serve"), "")
    other_folder = Server(2, INSTALLED, ("-m", "dugup", "mcp", "serve", "--data-dir", r"D:\x"), "")
    assert serverprocs.not_named([named, leftover, other_folder], entry) == [leftover, other_folder]
    assert serverprocs.not_named([leftover], None) == [], "with no entry there is nothing to compare against"


def test_the_advice_names_each_process_and_how_to_end_it():
    leftover = [Server(43500, CHECKOUT, (), "x"), Server(41324, CHECKOUT, (), "y")]
    said = serverprocs.advice(leftover, platform="win32")
    assert "process 43500" in said and "process 41324" in said and "Stop-Process -Id 43500, 41324" in said
    assert "kill 43500 41324" in serverprocs.advice(leftover, platform="linux")
    assert serverprocs.advice([]) == "" and serverprocs.short_advice([]) == ""
    assert "43500, 41324" in serverprocs.short_advice(leftover)


def test_stale_servers_reads_the_claude_settings_and_compares(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    assert setup.stale_servers(config, find=lambda: pytest.fail("no settings, nothing to look for")) == []
    config.write_text(json.dumps({"mcpServers": {"dugup": {"command": INSTALLED,
                                                           "args": ["-m", "dugup", "mcp", "serve"]}}}), encoding="utf-8")
    leftover = Server(43500, CHECKOUT, ("-m", "dugup", "mcp", "serve"), "")
    assert setup.stale_servers(config, find=lambda: [leftover]) == [leftover]
    assert setup.stale_servers(config, find=lambda: None) == [], "unknown is not reported as stale"


def test_mcp_status_warns_about_a_leftover_server(tmp_path, monkeypatch):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"dugup": {"command": INSTALLED,
                                                           "args": ["-m", "dugup", "mcp", "serve"]}}}), encoding="utf-8")
    monkeypatch.setattr(mcp_cmds, "claude_config_path", lambda environ, *a, **k: config)
    monkeypatch.setattr(serverprocs, "find", lambda *a, **k: [Server(43500, CHECKOUT, ("-m", "dugup", "mcp", "serve"),
                                                                      f"{CHECKOUT} -m dugup mcp serve")])
    out = io.StringIO()
    args = cli.build_parser().parse_args(["mcp", "status", "--data-dir", str(tmp_path / "data")])
    assert mcp_cmds.cmd_mcp(args, None, out, {}) == 0
    said = out.getvalue()
    assert "still running" in said and "process 43500" in said and CHECKOUT in said
    monkeypatch.setattr(serverprocs, "find", lambda *a, **k: [])
    out = io.StringIO()
    mcp_cmds.cmd_mcp(args, None, out, {})
    assert "still running" not in out.getvalue()
