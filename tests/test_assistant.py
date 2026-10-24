"""Read-only search for an assistant (assistant.py, mcpserver.py, `dugup mcp`): off by default, allowed folders
only, passages not whole files, models let go when idle."""

import dataclasses
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import helpers
from dugup import cli
from dugup.assistant import MOST_PASSAGES, OFF, PASSAGE_MOST, Refused, Tools, folders_allowed
from dugup.cli import mcp_cmds
from dugup.datadir import DataLayout
from dugup.service import LocalService
from dugup.settings import Settings
from dugup.shapes import Root


class Switch:
    """The settings as the person last saved them: tests change them between calls."""

    def __init__(self, **values):
        self.settings = Settings(mcp_enabled=True, **values)

    def set(self, **values):
        self.settings = dataclasses.replace(self.settings, **values)

    def __call__(self):
        return self.settings


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / helpers.AWKWARD_FOLDER
    shared, private = root / "shared", root / "private"
    shared.mkdir(parents=True)
    private.mkdir()
    (shared / "rent.txt").write_text("the quarterly rent invoice for the harbour flat", encoding="utf-8")
    (shared / "copy of notes.txt").write_text("harbour notes kept twice", encoding="utf-8")
    (private / "notes.txt").write_text("harbour notes kept twice", encoding="utf-8")      # the same content
    (private / "health.txt").write_text("a private letter that mentions rent and the harbour", encoding="utf-8")
    long_text = " ".join(f"harbour word{i}" for i in range(400))
    (shared / "long.txt").write_text(long_text, encoding="utf-8")
    return root, shared, private


@pytest.fixture
def made(tmp_path, corpus):
    root, shared, private = corpus
    layout = DataLayout(tmp_path / "data").ensure()
    service = LocalService(layout, Settings())
    service.set_root(Root(str(root)))
    service.run_indexing()
    switch = Switch()
    tools = Tools(service, switch, start_idle_watch=False)
    yield tools, switch, shared, private
    tools.close()
    service.close()


def paths(answer):
    return sorted(Path(r["path"]).name for r in answer["results"])


def test_off_unless_turned_on_and_off_again_at_once(made):
    tools, switch, shared, _ = made
    assert Settings().mcp_enabled is False, "off by default"
    assert tools.search("rent")["results"]
    switch.set(mcp_enabled=False)                 # turned off while an assistant is connected
    for call in (lambda: tools.search("rent"), tools.status, lambda: tools.passages(str(shared / "rent.txt"), "rent")):
        with pytest.raises(Refused) as caught:
            call()
        assert str(caught.value) == OFF


def test_only_the_allowed_folders_are_ever_seen(made):
    tools, switch, shared, private = made
    assert paths(tools.search("rent")) == ["health.txt", "rent.txt"], "every indexed folder when none are named"
    switch.set(mcp_folders=str(shared))
    found = tools.search("harbour", limit=50)
    assert all(Path(r["path"]).parent == shared for r in found["results"]) and found["results"]
    for row in found["results"]:
        assert not any(str(private) in p for p in row.get("also_at", [])), "another copy in a hidden folder is not named"
    with pytest.raises(Refused):
        tools.search("rent", folder=str(private))
    with pytest.raises(Refused):
        tools.passages(str(private / "health.txt"), "rent")
    assert all(not f["path"].startswith(str(private)) for f in tools.status()["folders"])


def test_several_allowed_folders_are_searched_together_without_repeats(made):
    tools, switch, shared, private = made
    switch.set(mcp_folders=f"{shared}; {private}")
    found = tools.search("harbour", limit=50)
    assert len({r["path"] for r in found["results"]}) == len(found["results"])
    assert {Path(r["path"]).parent for r in found["results"]} == {shared, private}


def test_a_result_carries_a_passage_and_more_passages_are_a_few_short_ones(made):
    tools, _, shared, _ = made
    row = next(r for r in tools.search("invoice")["results"])
    assert "invoice" in row["passage"] and "closeness" not in row, "word results come best first, without a score"
    more = tools.passages(str(shared / "long.txt"), "harbour", limit=99)
    assert 1 <= len(more["passages"]) <= MOST_PASSAGES
    assert all(len(p["text"]) <= PASSAGE_MOST for p in more["passages"]), "never the whole file"
    with pytest.raises(Refused):
        tools.passages(str(shared / "not indexed.txt"), "harbour")


@pytest.mark.parametrize("asked", [dict(how="sideways"), dict(kinds=["spreadsheet"]), dict(modified_after="March 5"),
                                   dict(query="  ")])
def test_unclear_requests_are_refused_with_the_reason(made, asked):
    tools = made[0]
    with pytest.raises(Refused):
        tools.search(**{"query": "rent", **asked})


def test_models_are_let_go_after_the_idle_minutes(tmp_path):
    now = [1000.0]
    freed = []

    class Service:
        def free_models(self):
            freed.append(True)
            return ["the question model (searching by meaning)"]

        def release_thread_store(self):
            pass

    switch = Switch(mcp_idle_minutes=5.0)
    tools = Tools(Service(), switch, clock=lambda: now[0], start_idle_watch=False)
    try:
        assert tools.release_if_idle() == [] and not freed, "nothing was loaded for an assistant"
        tools._last_model_use = now[0]
        now[0] += 299
        assert tools.release_if_idle() == [] and not freed
        now[0] += 2
        assert tools.release_if_idle() and freed == [True]
        assert tools.release_if_idle() == [], "once"
        switch.set(mcp_idle_minutes=0.0)
        tools._last_model_use = now[0] - 99999
        assert tools.release_if_idle() == [], "0 keeps them"
    finally:
        tools.close()


def test_the_folder_setting_is_read_generously():
    assert folders_allowed("") is None and folders_allowed(" ; ") is None
    assert folders_allowed('C:\\Docs; "D:\\My Notes" ;') == [os.path.abspath("C:\\Docs"), os.path.abspath("D:\\My Notes")]


# ---- the commands ------------------------------------------------------------------------------------------

def call(argv, tmp_path, ask=lambda q: "n"):
    out = io.StringIO()
    args = cli.build_parser().parse_args(argv + ["--data-dir", str(tmp_path / "data")])
    code = mcp_cmds.cmd_mcp(args, None, out, {}, ask=ask)
    return code, out.getvalue()


def test_enable_says_what_is_sent_and_asks_first(tmp_path):
    code, said = call(["mcp", "enable"], tmp_path, ask=lambda q: "n")
    assert code == 0 and "sent to the assistant's service" in said and "Nothing was changed" in said
    assert "mcp_enabled" not in (tmp_path / "data" / "config.toml").read_text(encoding="utf-8") \
        if (tmp_path / "data" / "config.toml").exists() else True
    allowed = tmp_path / "Shared notes"
    allowed.mkdir()
    code, said = call(["mcp", "enable", "--folders", f"{allowed};{tmp_path / 'not there'}"], tmp_path, ask=lambda q: "y")
    assert code == 1 and "do not exist" in said
    code, said = call(["mcp", "enable", "--folders", str(allowed)], tmp_path, ask=lambda q: "y")
    assert code == 0 and "Turned on" in said and str(allowed) in said
    code, said = call(["mcp", "status"], tmp_path)
    assert "ON" in said and str(allowed) in said
    code, said = call(["mcp", "disable"], tmp_path)
    assert code == 0 and "Turned off" in said
    assert "is off" in call(["mcp", "status"], tmp_path)[1]


def test_connect_adds_dugup_and_keeps_everything_else(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}), encoding="utf-8")
    code, said = call(["mcp", "connect", "--write", "--config", str(config)], tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert code == 0 and data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    entry = data["mcpServers"]["dugup"]
    assert entry["args"][:4] == ["-m", "dugup", "mcp", "serve"] and "--data-dir" in entry["args"]
    assert (tmp_path / "claude_desktop_config.json.bak").is_file() and "still off" in said
    config.write_text("{ not json", encoding="utf-8")
    code, said = call(["mcp", "connect", "--write", "--config", str(config)], tmp_path)
    assert code == 1 and "not valid JSON" in said and config.read_text(encoding="utf-8") == "{ not json"


def test_the_server_runs_on_the_console_python(tmp_path):
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    assert mcp_cmds.console_python(str(tmp_path / "pythonw.exe")) == str(tmp_path / "python.exe")
    assert mcp_cmds.console_python(str(tmp_path / "python.exe")) == str(tmp_path / "python.exe")
    assert mcp_cmds.console_python(str(tmp_path / "DugUp.exe")) == str(tmp_path / "python.exe"), "DugUp.exe has no console either"
    assert mcp_cmds.claude_config_path({"APPDATA": "C:\\Users\\a\\AppData\\Roaming"}, "win32").parts[-2:] == \
        ("Claude", "claude_desktop_config.json")


# ---- the real thing: an MCP client talking to `dugup mcp serve` ----------------------------------------------

def test_an_assistant_can_search_over_mcp_and_nothing_else(made, tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    tools, switch, shared, private = made
    data = tools.service.layout.root
    config = data / "config.toml"
    config.write_text(f'mcp_enabled = true\nmcp_folders = {json.dumps(str(shared))}\n', encoding="utf-8")
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment = {**os.environ, "PYTHONPATH": source + os.pathsep + os.environ.get("PYTHONPATH", "")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "dugup", "mcp", "serve", "--data-dir", str(data)],
                                   env=environment)
    seen = {}

    async def talk():
        with anyio.fail_after(120):
            await conversation()

    async def conversation():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                seen["tools"] = {t.name: t.annotations.read_only_hint for t in listed.tools}
                found = await session.call_tool("search", {"query": "rent"})
                seen["search"] = (found.is_error, found.content[0].text)
                hidden = await session.call_tool("passages", {"path": str(private / "health.txt"), "words": "rent"})
                seen["hidden"] = hidden.is_error

    anyio.run(talk)
    assert seen["tools"] == {"search": True, "passages": True, "status": True}, "three tools, all read-only"
    is_error, text = seen["search"]
    assert not is_error and "rent.txt" in text and "health.txt" not in text
    assert seen["hidden"] is True


def test_the_claude_app_entry_is_told_apart_this_copy_another_copy_or_none(tmp_path):
    from dugup import assistantsetup as setup
    config = tmp_path / "claude.json"
    here = setup.server_entry(python=str(tmp_path / "installed" / "python.exe"))
    assert setup.connection(config, here) == "none"
    other = setup.server_entry(python=str(tmp_path / "source" / ".venv" / "python.exe"))
    config.write_text(json.dumps({"mcpServers": {"dugup": other, "else": {"command": "x"}}}), encoding="utf-8")
    assert setup.connection(config, here) == "other" and setup.connected_command(config) == other["command"]
    done, _ = setup.connect(config, here)
    assert done and setup.connection(config, here) == "this"
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["else"] == {"command": "x"}, "the rest is kept"
    with_folder = setup.server_entry(str(tmp_path / "data"), python=str(tmp_path / "installed" / "python.exe"))
    assert setup.connection(config, with_folder) == "other", "another data folder is another DugUp too"


def test_with_no_index_the_connection_starts_and_every_answer_says_what_to_do(tmp_path, monkeypatch):
    from dugup import mcpserver
    from dugup.assistant import NoIndex, Refused
    served = []
    monkeypatch.setattr(mcpserver, "serve", lambda service, load, tools=None: served.append(tools))
    data = tmp_path / "data"
    out = io.StringIO()
    assert cli.main(["mcp", "serve", "--data-dir", str(data)], stdout=out, environ={}) == 0, "it starts, not fails"
    tools = served[0]
    assert isinstance(tools, NoIndex)
    for call in (lambda: tools.search("rent"), tools.status, lambda: tools.passages("x", "y")):
        with pytest.raises(Refused) as caught:
            call()
        assert "no index" in str(caught.value) and "Connect the Claude app to this DugUp" in str(caught.value)
    data.mkdir(exist_ok=True)
    (data / "index.sqlite").write_bytes(b"")
    with pytest.raises(Refused) as caught:
        tools.search("rent")
    assert "Restart the Claude desktop app" in str(caught.value)
