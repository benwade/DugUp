import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))  # so tests can `import helpers`


def pytest_collection_modifyitems(config, items):
    is_windows = os.name == "nt"
    skip_win = pytest.mark.skip(reason="Windows only")
    skip_posix = pytest.mark.skip(reason="POSIX only")
    for item in items:
        if "windows_only" in item.keywords and not is_windows:
            item.add_marker(skip_win)
        if "linux_only" in item.keywords and is_windows:
            item.add_marker(skip_posix)


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "index.sqlite"


@pytest.fixture
def store(store_path):
    from dugup.store import SqliteStore

    with SqliteStore(store_path) as opened:
        yield opened


@pytest.fixture(autouse=True)
def windows_stay_hidden_during_tests(monkeypatch):
    """The window tests build real windows and close them again, fifty times a run. On a desktop that is a
    storm of windows flashing by (seen on Windows). Every new top-level window is withdrawn as it is made;
    the tests drive the windows through their widgets, not through the screen. (A test that brings the
    main window back from being minimized still shows it for a moment; that is the window doing its job.)"""
    try:
        import tkinter
    except ImportError:
        return
    original = tkinter.Toplevel.__init__

    def hidden(self, *args, **kwargs):
        original(self, *args, **kwargs)
        try:
            self.withdraw()
        except Exception:
            pass

    monkeypatch.setattr(tkinter.Toplevel, "__init__", hidden)
