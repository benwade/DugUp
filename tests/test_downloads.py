"""Downloading everything at once: sizes said first, room checked first, progress that only moves forward,
one failure never stopping the rest, and the meaning model chosen for the machine before it is fetched."""

import io
import threading
import types

import pytest

from dugup import cli, downloads
from dugup.catalogue import Machine
from dugup.cli import download_cmds
from dugup.downloads import Download, describe, room_for
from dugup.models import Pinned
from dugup.shapes import Failure, FailureReason

GB = 1024 ** 3


def _item(name, size):
    return Pinned(name, f"https://example.invalid/{name}", size, "0" * 64, f"the {name} file")


def _download(tmp_path, key, *sizes, everyday=True):
    items = tuple(_item(f"{key}-{i}.onnx", size) for i, size in enumerate(sizes))
    return Download(key, key.title(), f"do {key} things", tmp_path / "models" / key, items, everyday=everyday)


def fake_fetcher(fail=(), stop_after=None):
    """Writes each file at its full size, telling progress in two steps, as models.fetch does."""
    calls = []

    def fetch(item, folder, progress, stop):
        calls.append(item.name)
        if item.name in fail:
            return Failure(FailureReason.UNREACHABLE, "the host did not answer")
        if stop_after is not None and len(calls) > stop_after:
            return Failure(FailureReason.STOPPED, "stopped")
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / (item.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        progress(types.SimpleNamespace(done=have, total=item.size, phase="download", root=item.name))
        (folder / item.name).write_bytes(b"\0" * item.size)
        if part.exists():
            part.unlink()
        progress(types.SimpleNamespace(done=item.size, total=item.size, phase="download", root=item.name))
        return folder / item.name

    fetch.calls = calls
    return fetch


# ---- what is said before anything starts --------------------------------------------------------------------

def test_the_listing_gives_each_size_and_what_is_already_here(tmp_path):
    a, b = _download(tmp_path, "meaning", 3000), _download(tmp_path, "looks", 1000, 1000)
    b.folder.mkdir(parents=True)
    (b.folder / "looks-0.onnx").write_bytes(b"\0" * 1000)
    (b.folder / "looks-1.onnx.part").write_bytes(b"\0" * 400)
    assert a.missing() == 3000 and b.missing() == 600
    text = describe([a, b], room_for([a, b], 10 * GB, 0))
    assert "Meaning: 3 KB" not in text and "600 B" not in text      # sizes are in MB/GB, never odd units
    assert "Looks:" in text and "left" in text and "Still to download" in text and "checksum" in text


def test_everything_leaves_out_the_experimental_model(tmp_path):
    every = [_download(tmp_path, "meaning", 10), _download(tmp_path, "words", 10, everyday=False)]
    assert [d.key for d in downloads.everything(every)] == ["meaning"]


def test_too_little_room_refuses_before_anything_is_fetched(tmp_path):
    chosen = [_download(tmp_path, "meaning", 5000)]
    fetcher = fake_fetcher()
    got = downloads.fetch(chosen, free=lambda: 6000, keep_free=2000, fetcher=fetcher)
    assert isinstance(got, Failure) and "not enough room" in got.detail
    assert fetcher.calls == [] and not chosen[0].folder.exists()
    assert "not enough room" in describe(chosen, room_for(chosen, 6000, 2000))


# ---- fetching -----------------------------------------------------------------------------------------------

def test_everything_arrives_and_progress_only_moves_forward_to_the_whole(tmp_path):
    chosen = [_download(tmp_path, "meaning", 300), _download(tmp_path, "looks", 200, 100)]
    seen = []
    got = downloads.fetch(chosen, lambda d, t, title: seen.append((d, t)), fetcher=fake_fetcher())
    assert got.done == ["meaning", "looks"] and not got.failed and not got.stopped
    assert all(d.present for d in chosen)
    dones = [d for d, _ in seen]
    assert dones == sorted(dones) and dones[-1] == 600 and all(t == 600 for _, t in seen)


def test_a_resumed_part_is_not_counted_twice(tmp_path):
    chosen = [_download(tmp_path, "meaning", 1000)]
    chosen[0].folder.mkdir(parents=True)
    (chosen[0].folder / "meaning-0.onnx.part").write_bytes(b"\0" * 700)
    seen = []
    downloads.fetch(chosen, lambda d, t, title: seen.append((d, t)), fetcher=fake_fetcher())
    assert seen[-1] == (300, 300) and max(d for d, _ in seen) == 300


def test_one_failure_is_reported_and_the_rest_still_come(tmp_path):
    chosen = [_download(tmp_path, "meaning", 10), _download(tmp_path, "looks", 10), _download(tmp_path, "captions", 10)]
    got = downloads.fetch(chosen, fetcher=fake_fetcher(fail={"looks-0.onnx"}))
    assert got.done == ["meaning", "captions"] and got.failed == [("looks", "the host did not answer")]
    text = download_cmds.summary(got, chosen)
    assert "Ready: Meaning, Captions" in text and "Not downloaded: Looks" in text and "first use" in text


def test_a_stop_keeps_what_came_and_says_it_continues(tmp_path):
    chosen = [_download(tmp_path, "meaning", 10), _download(tmp_path, "looks", 10)]
    got = downloads.fetch(chosen, fetcher=fake_fetcher(stop_after=1))
    assert got.stopped and got.done == ["meaning"] and chosen[0].present and not chosen[1].present
    assert "continue where it left off" in download_cmds.summary(got, chosen)


def test_a_stop_asked_before_the_next_download_stops_there(tmp_path):
    chosen = [_download(tmp_path, "meaning", 10), _download(tmp_path, "looks", 10)]
    stop = threading.Event()
    got = downloads.fetch(chosen, lambda d, t, title: stop.set() if d >= 10 else None, stop, fetcher=fake_fetcher())
    assert got.stopped and got.done == ["meaning"]


# ---- the meaning model is chosen before it is fetched --------------------------------------------------------

def test_the_meaning_model_is_chosen_for_the_machine_and_written_down(tmp_path):
    from dugup.settings import Settings
    config = tmp_path / "config.toml"
    small_machine = Machine(cores=2, free_memory_bytes=8 * GB)
    settings, said = downloads.settle_meaning_model(config, Settings(), lambda: small_machine)
    assert settings.meaning_model == "small" and "small model" in said and "2 processor cores" in said
    from dugup.settings import load
    assert load(config)[0].meaning_model == "small", "written down, so the setup that follows agrees"


def test_a_meaning_model_the_person_chose_is_kept(tmp_path):
    from dugup.settings import Settings
    config = tmp_path / "config.toml"
    config.write_text('meaning_model = "larger"\n', encoding="utf-8")
    looked = []
    settings, said = downloads.settle_meaning_model(config, Settings(), lambda: looked.append(1))
    assert settings.meaning_model == "larger" and said == "" and looked == []


# ---- the command ----------------------------------------------------------------------------------------------

@pytest.fixture
def small_downloads(tmp_path, monkeypatch):
    every = [_download(tmp_path, "meaning", 300), _download(tmp_path, "looks", 200),
             _download(tmp_path, "words", 100, everyday=False)]
    monkeypatch.setattr(downloads, "available", lambda layout, settings, plain_words=True: every)
    monkeypatch.setattr(download_cmds, "look", lambda layout, settings: Machine(cores=16, free_memory_bytes=32 * GB))
    real = downloads.fetch
    monkeypatch.setattr(downloads, "fetch", lambda *a, **k: real(*a, fetcher=fake_fetcher(), **k))
    return every


def run(tmp_path, *argv, ask=lambda q: "n"):
    out = io.StringIO()
    args = cli.build_parser().parse_args(["download", *argv, "--data-dir", str(tmp_path / "data")])
    code = download_cmds.cmd_download(args, None, out, {}, install_signals=False, ask=ask)
    return code, out.getvalue()


def test_list_shows_and_fetches_nothing(tmp_path, small_downloads):
    code, text = run(tmp_path, "--list")
    assert code == 0 and "Meaning" in text and "Looks" in text and "Not included: Words" in text
    assert not any(d.present for d in small_downloads)


def test_it_asks_first_and_no_means_nothing(tmp_path, small_downloads):
    code, text = run(tmp_path)
    assert code == 0 and "Nothing was downloaded" in text and not small_downloads[0].present


def test_yes_fetches_everything_but_the_experimental_model(tmp_path, small_downloads):
    code, text = run(tmp_path, "--yes")
    assert code == 0, text
    assert small_downloads[0].present and small_downloads[1].present and not small_downloads[2].present
    assert "100%" in text and "Ready: Meaning, Looks" in text
    assert "larger model" in text, "the machine's choice of meaning model is said"


def test_only_and_plain_words_choose(tmp_path, small_downloads):
    run(tmp_path, "--only", "looks", "--yes")
    assert small_downloads[1].present and not small_downloads[0].present
    run(tmp_path, "--plain-words", "--yes")
    assert small_downloads[2].present


# ---- the window -----------------------------------------------------------------------------------------------

@pytest.fixture
def root():
    tkinter = pytest.importorskip("tkinter")
    try:
        made = tkinter.Tk()
    except tkinter.TclError:
        pytest.skip("no display")
    made.withdraw()
    yield made
    made.destroy()
    import gc
    gc.collect()


def test_the_window_downloads_and_says_what_is_ready(tmp_path, root):
    from dugup.datadir import DataLayout
    from dugup.downloadwindow import run_in_window
    chosen = [_download(tmp_path, "meaning", 300), _download(tmp_path, "looks", 200)]
    layout = DataLayout(tmp_path / "data")
    (tmp_path / "data").mkdir()
    fetch = lambda *a, **k: downloads.fetch(*a, fetcher=fake_fetcher(), **k)
    code = run_in_window(chosen, layout, 0, notice="Search by meaning will use the small model.", fetch=fetch,
                         root=root, close_when_done=True)
    assert code == 0 and all(d.present for d in chosen)


def test_the_window_reports_a_failure_without_raising(tmp_path, root):
    from dugup.datadir import DataLayout
    from dugup.downloadwindow import run_in_window
    chosen = [_download(tmp_path, "meaning", 300)]
    (tmp_path / "data").mkdir()

    def broken(*a, **k):
        raise RuntimeError("the network went away")

    assert run_in_window(chosen, DataLayout(tmp_path / "data"), 0, fetch=broken, root=root, close_when_done=True) == 1


def test_the_window_with_nothing_to_do_says_so(tmp_path, root):
    from dugup.datadir import DataLayout
    from dugup.downloadwindow import run_in_window
    chosen = [_download(tmp_path, "meaning", 300)]
    chosen[0].folder.mkdir(parents=True)
    (chosen[0].folder / "meaning-0.onnx").write_bytes(b"\0" * 300)
    (tmp_path / "data").mkdir()
    assert run_in_window(chosen, DataLayout(tmp_path / "data"), 0, root=root, close_when_done=True) == 0
