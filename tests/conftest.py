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
