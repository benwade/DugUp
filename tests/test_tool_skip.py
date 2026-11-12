"""Tool data and downloaded requirements are left out by every scan (skip_tool_data), not only by the skip list a
folder was given when it was set up: an environment made later, under any name, is left out too (Ben, 2026-10-28).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import helpers
from dugup import cli
from dugup.datadir import DataLayout
from dugup.dirscan import DirectoryScan
from dugup.protocols import ScanRules
from dugup.service import LocalService
from dugup.suggest import tool_folder


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / helpers.AWKWARD_FOLDER
    _write(root / "notes.txt", "my own notes about the harbour")
    _write(root / "app" / "main.py", "print('harbour')")
    _write(root / "app" / "node_modules" / "left" / "index.js", "harbour package")
    _write(root / "app" / "venv" / "lib" / "site.py", "harbour library")
    _write(root / "zonos_env" / "pyvenv.cfg", "home = C:/Python313")          # an environment with a name of its own
    _write(root / "zonos_env" / "Lib" / "thing.py", "harbour library")
    _write(root / "conda_stuff" / "conda-meta" / "history", "harbour")        # a conda environment
    _write(root / "conda_stuff" / "lib" / "pkg.py", "harbour library")
    _write(root / "repo" / ".git" / "config", "harbour")
    return root


def _names(listing):
    return sorted(Path(e.path).name for e in listing.entries)


def test_a_tool_folder_is_known_by_its_name_or_by_what_it_holds():
    nothing = lambda child: False
    assert tool_folder("node_modules", nothing) and tool_folder("SITE-PACKAGES", nothing) and tool_folder(".git", nothing)
    assert tool_folder("zonos_env", lambda child: child == "pyvenv.cfg") == "downloaded requirements: a Python environment"
    assert tool_folder("anything", lambda child: child == "conda-meta")
    assert tool_folder("Letters", nothing) is None and tool_folder("environment notes", nothing) is None


def test_the_scan_leaves_out_tool_data_found_by_name_or_by_its_marker(tree):
    source = DirectoryScan()
    every = source.scan(str(tree), ScanRules())
    assert {"index.js", "site.py", "thing.py", "pkg.py", "config"} <= set(_names(every)), "off: everything is listed"
    kept = source.scan(str(tree), ScanRules(skip_tool=True))
    assert _names(kept) == ["main.py", "notes.txt"]
    assert source.last_stats["tool_skipped"] == 5


def test_an_include_pattern_that_names_a_tool_folder_lets_it_in(tree):
    listing = DirectoryScan().scan(str(tree), ScanRules(include=("zonos_env", "*.txt"), skip_tool=True))
    assert _names(listing) == ["notes.txt", "pyvenv.cfg", "thing.py"], "what a person asked for is never left out"


class Shell:
    def __init__(self, tmp_path, docs):
        self.data, self.docs = tmp_path / "data", docs

    def run(self, *argv):
        out = io.StringIO()
        return cli.main([*argv, "--data-dir", str(self.data)], stdout=out, environ={}), out.getvalue()

    def indexed(self):
        with LocalService(DataLayout(self.data)) as service:
            return sorted(Path(r.path).name for r in service.store.list_files(missing=False))


def test_an_environment_made_later_under_any_name_is_left_out_and_the_setting_lets_it_in(tmp_path, tree):
    sh = Shell(tmp_path, tree)
    assert sh.run("roots", "add", str(tree))[0] == 0
    assert sh.run("index")[0] == 0
    assert sh.indexed() == ["main.py", "notes.txt"], "left out by default"
    # next month: a new environment, named as its tool likes, inside an indexed folder
    _write(tree / "new tool" / "voices_venv" / "pyvenv.cfg", "home = C:/Python313")
    _write(tree / "new tool" / "voices_venv" / "Lib" / "big.py", "harbour library")
    _write(tree / "new tool" / "readme.txt", "my notes on the new tool")
    assert sh.run("index")[0] == 0
    assert sh.indexed() == ["main.py", "notes.txt", "readme.txt"], "the new environment is left out too"
    # the person wants them after all
    assert sh.run("config", "set", "skip_tool_data", "no")[0] == 0
    assert sh.run("index")[0] == 0
    assert {"big.py", "index.js", "site.py", "thing.py", "pkg.py"} <= set(sh.indexed())
    # and back: they leave the index as a choice, not as files gone missing
    assert sh.run("config", "set", "skip_tool_data", "yes")[0] == 0
    code, text = sh.run("index")
    assert code == 0 and "left the index" in text and "gone 0" in text and "WARNING" not in text
    assert sh.indexed() == ["main.py", "notes.txt", "readme.txt"]
    assert "missing" not in sh.run("status")[1]


def test_the_pickers_checkbox_is_the_setting(tmp_path, tree):
    data = DataLayout(tmp_path / "data").ensure()
    with LocalService(data) as service:
        material = service.plan_folder(str(tree))
        plan = material["plan"]
        assert plan.skip_tool is True, "the picker starts from the setting"
        plan.skip_tool = False
        assert not hasattr(service.apply_plan(plan), "reason")
        assert service.settings.skip_tool_data is False, "unticked in the picker: off for every scan"
    from dugup import settings as settings_module
    with LocalService(data, settings_module.load(data.config)[0]) as service:      # as the app starts: config.toml read
        assert service.settings.skip_tool_data is False, "and saved in config.toml"
        other = _write(tmp_path / "other" / "a.txt").parent
        assert service.plan_folder(str(other))["plan"].skip_tool is False, "the next folder's picker starts from it"


def test_the_setting_is_on_the_settings_windows_short_list():
    from dugup import catalogue
    from dugup.settings import Settings
    assert Settings().skip_tool_data is True and "skip_tool_data" in catalogue.SHORT_LIST
    assert catalogue.ENTRIES["skip_tool_data"].group == "folders"
