"""The command line, run in-process on real temporary folders."""

import io
import signal
import threading

import pytest

import helpers
from dugup import cli
from dugup.cli import ProgressPrinter, format_seconds, one_line, shorten
from dugup.indexer import Progress
from dugup.service import LocalService
from dugup.shapes import Failure, FailureReason


class Shell:
    def __init__(self, tmp_path):
        self.data = tmp_path / "data"
        self.docs = tmp_path / helpers.AWKWARD_FOLDER
        (self.docs / "sub").mkdir(parents=True)
        (self.docs / "a.txt").write_text("alpha needle text\nsecond line with a needle too", encoding="utf-8")
        (self.docs / "sub" / f"{helpers.CAFE} {helpers.CJK}.txt").write_text(f"beta words {helpers.CJK}", encoding="utf-8")
        (self.docs / "locked.docx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100))

    def run(self, *argv, stream=None):
        out = stream or io.StringIO()
        code = cli.main([*argv, "--data-dir", str(self.data)], stdout=out, environ={})
        return code, out.getvalue() if stream is None else out

    def ready(self):
        assert self.run("roots", "add", str(self.docs))[0] == 0
        assert self.run("index")[0] == 0
        return self


@pytest.fixture
def sh(tmp_path):
    return Shell(tmp_path)


def test_before_anything_exists_the_commands_say_how_to_start(sh):
    for command in (["search", "x"], ["status"], ["roots", "list"]):
        code, text = sh.run(*command)
        assert code == 1 and "roots add" in text
    assert not sh.data.exists(), "asking questions must not create the data folder"


def test_init_creates_the_data_folder_and_says_what_is_next(sh):
    code, text = sh.run("init")
    assert code == 0 and (sh.data / "index.sqlite").exists() and "roots add" in text
    assert sh.run("init")[0] == 0, "init twice is harmless"
    code, text = sh.run("index")
    assert code == 1 and "No folders to index yet" in text


def test_add_index_search_status(sh):
    code, text = sh.run("roots", "add", str(sh.docs), "--exclude", "*.tmp", "--suggested-excludes")
    assert code == 0 and str(sh.docs) in text and ".git" in text and "*.tmp" in text
    code, text = sh.run("index")
    assert code == 0 and "new 3" in text and "2 done, 0 partial, 1 failed" in text and "dugup status" in text

    code, text = sh.run("search", "needle")
    assert code == 0 and "Showing 1 to 1 of 1 file(s)" in text and "a.txt" in text and "alpha [needle] text" in text

    code, text = sh.run("search", helpers.CJK)
    assert code == 0 and f"{helpers.CAFE} {helpers.CJK}.txt" in text and f"[{helpers.CJK}]" in text

    code, text = sh.run("search", "nosuchword")
    assert code == 0 and "No files found" in text

    code, text = sh.run("status")
    assert code == 0 and "3 file(s)" in text and "done 2" in text and "failed 1" in text and "status --attention" in text
    code, text = sh.run("status", "--attention")
    assert "showing 1 of 1" in text and "locked.docx" in text and "password_protected" in text

    code, text = sh.run("roots", "list")
    assert code == 0 and str(sh.docs) in text and "3 file(s)" in text and "last scan never" not in text


def test_files_waiting_for_ocr_are_counted_apart_and_do_not_bury_real_trouble(sh):
    for i in range(60):
        (sh.docs / f"icon{i:02d}.png").write_bytes(b"\x89PNG stand-in")
    sh.ready()
    code, text = sh.run("status", "--attention")
    assert "60 file(s) wait for OCR" in text and "Needs attention: showing 1 of 1" in text
    assert "locked.docx" in text and "icon" not in text
    plain = sh.run("status")[1]
    assert "60 file(s) wait for OCR" in plain and "1 file(s) failed or were read only in part" in plain


def test_a_query_of_several_words_points_at_the_place_where_they_stand_together(sh):
    (sh.docs / "both.txt").write_text("gamma appears early on its own. " + "filler " * 60
                                      + "Much later gamma and then delta stand side by side.", encoding="utf-8")
    sh.ready()
    text = sh.run("search", "gamma", "delta")[1]
    assert "Much later [gamma and then delta] stand side by side." in text


def test_a_passage_held_by_two_overlapping_pieces_is_shown_once(sh):
    words = " ".join(f"w{i:04d}" for i in range(280))            # about 1,700 characters: the next words fall in the overlap
    (sh.docs / "long.txt").write_text(words + " uniqueoverlapword " + " ".join(f"t{i:04d}" for i in range(300)), encoding="utf-8")
    sh.ready()
    text = sh.run("search", "uniqueoverlapword")[1]
    assert "2 piece" not in text and text.count("[uniqueoverlapword]") == 1


def test_a_truncated_list_says_so_and_pages_on(sh):
    for i in range(7):
        (sh.docs / f"n{i}.txt").write_text("commonword", encoding="utf-8")
    sh.ready()
    code, text = sh.run("search", "commonword", "--limit", "3", "--sort", "name")
    assert "Showing 1 to 3 of 7 file(s)" in text and "--offset 3" in text and "n0.txt" in text and "n3.txt" not in text
    code, text = sh.run("search", "commonword", "--limit", "3", "--offset", "6", "--sort", "name")
    assert "Showing 7 to 7 of 7 file(s)" in text and "n6.txt" in text and "More:" not in text


def test_a_gone_file_is_shown_with_the_guides_note_and_its_removal_date(sh):
    for i in range(3):
        (sh.docs / f"keep{i}.txt").write_text("other", encoding="utf-8")
    sh.ready()
    (sh.docs / "a.txt").unlink()
    code, text = sh.run("index")
    assert code == 0 and "gone 1" in text
    code, text = sh.run("search", "needle")
    assert "Not at this path at the last scan" in text and "removed from the index on 20" in text
    assert "missing 1" in sh.run("status")[1]


def test_removing_a_root_from_the_command_line(sh):
    sh.ready()
    code, text = sh.run("roots", "remove", str(sh.docs))
    assert code == 0 and "3 file(s) left the index" in text
    assert "No files found" in sh.run("search", "needle")[1]
    code, text = sh.run("roots", "remove", str(sh.docs))
    assert code == 1 and "not one of the indexed folders" in text
    assert "No folders yet" in sh.run("roots", "list")[1]


def test_refusals_come_back_as_plain_words_and_exit_code_1(sh, tmp_path):
    code, text = sh.run("roots", "add", str(tmp_path / "nowhere"))
    assert code == 1 and "Could not do that (unreachable)" in text
    sh.ready()
    code, text = sh.run("index", "--root", str(tmp_path))
    assert code == 1 and "not an indexed folder" in text


def test_a_names_only_folder_from_the_command_line(sh):
    assert sh.run("roots", "add", str(sh.docs), "--policy", "names_only")[0] == 0
    code, text = sh.run("index")
    assert code == 0 and "Read 0 file(s)" in text
    assert "No files found" in sh.run("search", "needle")[1] and "a.txt" in sh.run("search", "a.txt")[1]


def test_changed_settings_ask_for_a_choice(sh):
    sh.ready()
    (sh.data / "config.toml").write_text("chunk_chars = 900\nmystery = 1\n", encoding="utf-8")
    code, text = sh.run("index")
    assert code == 1 and "chunk_chars" in text and "--reread" in text and "--keep" in text
    assert "[config] mystery" in text, "a key that is not understood is mentioned, not swallowed"
    code, text = sh.run("index", "--reread")
    assert code == 0 and "Read 3 file(s)" in text
    assert sh.run("index")[0] == 0


def test_a_stop_request_ends_the_run_cleanly_and_the_next_run_continues(sh, monkeypatch):
    for i in range(30):
        (sh.docs / f"n{i:02d}.txt").write_text(f"stopword {i}", encoding="utf-8")
    assert sh.run("roots", "add", str(sh.docs))[0] == 0

    class StopAfter(threading.Event):   # turns on after it has been looked at a number of times
        looks = 0

        def is_set(self):
            self.looks += 1
            return self.looks > 60

    args = cli.build_parser().parse_args(["index", "--data-dir", str(sh.data)])
    out = io.StringIO()
    code = cli.cmd_index(args, None, out, {}, stop=StopAfter(), install_signals=False)
    assert code == 130 and "[Interrupted] Stopped cleanly" in out.getvalue() and "again to continue" in out.getvalue()
    status = sh.run("status")[1]
    assert "pending" in status and "wait to be read" in status
    code, text = sh.run("index")
    assert code == 0 and "pending" not in sh.run("status")[1]
    assert "Showing 1 to 20 of 30" in sh.run("search", "stopword")[1]


def test_a_hard_interrupt_is_reported_and_exits_130(sh, monkeypatch):
    sh.ready()

    def boom(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(LocalService, "run_indexing", boom)
    before = signal.getsignal(signal.SIGINT)
    code, text = sh.run("index")
    assert code == 130 and "[Interrupted]" in text
    assert signal.getsignal(signal.SIGINT) is before, "the Ctrl+C handler must be put back"


def test_another_run_in_progress_is_reported(sh, monkeypatch):
    sh.ready()
    monkeypatch.setattr(LocalService, "run_indexing", lambda self, *a, **k: Failure(FailureReason.BUSY, "another program is indexing"))
    code, text = sh.run("index")
    assert code == 1 and "busy" in text and "another program is indexing" in text


def test_a_legacy_console_cannot_crash_any_command(sh):
    sh.ready()
    for command in (["search", helpers.CJK], ["status", "--attention"], ["roots", "list"], ["index"]):
        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        code, stream = sh.run(*command, stream=console)
        stream.flush()
        shown = stream.buffer.getvalue().decode("cp1252")
        assert code == 0 and "\\u65e5" in shown, command


def test_the_log_file_holds_no_text_and_no_names(sh):
    sh.ready()
    log = (sh.data / "logs" / "dugup.log").read_text(encoding="utf-8")
    assert "run finished" in log and "password_protected" in log
    for secret in ("needle", "alpha", "a.txt", "locked", helpers.CAFE, helpers.CJK):
        assert secret not in log


# ---- small pieces ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,text", [(None, "unknown"), (0, "0 s"), (59.4, "59 s"), (61, "1 min 01 s"),
                                          (3600, "1 h 00 min"), (3725, "1 h 02 min"), (-5, "0 s")])
def test_durations_read_naturally(seconds, text):
    assert format_seconds(seconds) == text


def test_text_helpers():
    assert one_line(" a\n\tb  c ") == "a b c"
    assert shorten("abcdefghij", 8) == "abcde..." and shorten("abc", 8) == "abc"


def test_progress_is_printed_at_most_once_an_interval_and_always_at_the_end():
    now = [0.0]
    out = io.StringIO()
    printer = ProgressPrinter(out, interval=1.0, timer=lambda: now[0])
    printer(Progress("scan", "root", 0, 0))
    for done in range(0, 100):
        now[0] += 0.1
        printer(Progress("read", "root", done, 100, 1, elapsed=now[0], current=f"/x/{helpers.CJK} file {done}.txt"))
    printer(Progress("read", "root", 100, 100, 1, elapsed=now[0]))
    printer.finish()
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("[scan]") and 8 <= len(lines) <= 14, "about one line a second, not one a file"
    assert "100 of 100 (100.0%)" in lines[-1] and "failed 1" in lines[-1]
    assert "files/s" in lines[2] and "left" in lines[2] and helpers.CJK in lines[2]


def test_progress_with_nothing_to_go_on_does_not_divide_by_zero():
    out = io.StringIO()
    ProgressPrinter(out, timer=lambda: 0.0)(Progress("read", "root", 0, 0))
    assert "0 of 0 (0.0%)" in out.getvalue() and "0.0 files/s" in out.getvalue()
