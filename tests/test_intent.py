"""Plain words to a search form. The model is a stand-in: a scripted object, a small web server inside the
test, or a stand-in server program started for real through the launcher. A server that must be caught
not answering holds its answer until the test lets it go; nothing relies on one side being slower."""

import datetime
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import helpers
from dugup import intent, launcher
from dugup.intent import (SCHEMA, ChatServer, IntentParser, LocalRuntime, Understood, from_settings, is_local, messages_for,
                          period_days, understood_from)
from dugup.launcher import alive
from dugup.settings import Settings
from dugup.shapes import Failure, FailureReason

TESTS = os.path.dirname(os.path.abspath(__file__))
MONDAY = datetime.date(2026, 10, 12)      # a Monday
FOLDERS = ["D:\\Claude", f"/home/me/{helpers.CAFE} notes"]


def form(**changes):
    base = {"words": "nursing", "types": [], "where": "anywhere", "period": "", "modified_after": "", "modified_before": "",
            "min_size": 0, "min_size_unit": "mb", "max_size": 0, "max_size_unit": "mb", "folder": "", "exact": False}
    base.update(changes)
    return base


# ---- periods and checking the model's answer -----------------------------------------------------------------

@pytest.mark.parametrize("period,today,expected", [
    ("today", MONDAY, ("2026-10-12", "2026-10-12")),
    ("yesterday", MONDAY, ("2026-10-11", "2026-10-11")),
    ("this_week", MONDAY, ("2026-10-12", "2026-10-12")),
    ("this_week", datetime.date(2026, 10, 15), ("2026-10-12", "2026-10-15")),
    ("last_week", MONDAY, ("2026-10-05", "2026-10-11")),
    ("last_week", datetime.date(2026, 10, 18), ("2026-10-05", "2026-10-11")),
    ("this_month", MONDAY, ("2026-10-01", "2026-10-12")),
    ("last_month", MONDAY, ("2026-09-01", "2026-09-30")),
    ("last_month", datetime.date(2026, 1, 15), ("2025-12-01", "2025-12-31")),
    ("last_month", datetime.date(2028, 3, 31), ("2028-02-01", "2028-02-29")),
    ("this_year", MONDAY, ("2026-01-01", "2026-10-12")),
    ("last_year", MONDAY, ("2025-01-01", "2025-12-31")),
    ("", MONDAY, ("", "")), ("next_decade", MONDAY, ("", "")),
])
def test_periods_are_worked_out_here_not_by_the_model(period, today, expected):
    assert period_days(period, today) == expected


def test_a_good_answer_becomes_the_form_fields():
    got = understood_from(form(words='nursing  "care plan"', types=["pdf", "office"], where="text", period="last_month",
                               min_size=1.5, min_size_unit="mb", max_size=2, max_size_unit="gb", folder="d:\\claude\\", exact=True),
                          FOLDERS, MONDAY)
    assert got == Understood('nursing "care plan"', "D:\\Claude", "text", ("pdf", "office"), "2026-09-01", "2026-09-30",
                             "1.5m", "2g", True, ())
    assert got.describe() == ('words: nursing "care plan"; types: pdf, office; words in: text; modified 2026-09-01 to 2026-09-30; '
                              "at least 1.5m; at most 2g; in D:\\Claude; exact form")
    plain = understood_from(form(), FOLDERS, MONDAY)
    assert plain == Understood(words="nursing") and plain.describe() == "words: nursing"


def test_explicit_dates_are_used_and_a_period_wins_over_them():
    got = understood_from(form(modified_after="2026-03-01", modified_before="2026-03-31T00:00:00"), FOLDERS, MONDAY)
    assert (got.after, got.before, got.notes) == ("2026-03-01", "2026-03-31", ())
    both = understood_from(form(period="yesterday", modified_after="1999-01-01"), FOLDERS, MONDAY)
    assert (both.after, both.before) == ("2026-10-11", "2026-10-11")


@pytest.mark.parametrize("changes,note,check", [
    (dict(types=["pdf", "spreadsheet"]), "kind of file", lambda u: u.kinds == ("pdf",)),
    (dict(where="everywhere"), "where the words", lambda u: u.where == "anywhere"),
    (dict(modified_after="last Tuesday"), "date it gave", lambda u: u.after == ""),
    (dict(modified_after="2026-05-01", modified_before="2026-04-01"), "wrong order", lambda u: (u.after, u.before) == ("", "")),
    (dict(period="fortnight"), "time it named", lambda u: u.after == ""),
    (dict(min_size="big"), "size it gave", lambda u: u.smallest == ""),
    (dict(min_size=-4), "size it gave", lambda u: u.smallest == ""),
    (dict(max_size=3, max_size_unit="parsecs"), "size it gave", lambda u: u.largest == ""),
    (dict(folder="C:\\Elsewhere"), "not one of the indexed folders", lambda u: u.folder == ""),
    (dict(words="   "), "no words", lambda u: u.words == ""),
])
def test_what_makes_no_sense_is_dropped_and_said(changes, note, check):
    got = understood_from(form(**changes), FOLDERS, MONDAY)
    assert isinstance(got, Understood) and check(got) and any(note in n for n in got.notes), got


def test_odd_but_harmless_answers():
    assert understood_from(form(types=["pdf", "office", "text", "image"]), FOLDERS, MONDAY).kinds == (), "every kind is no filter"
    assert understood_from(form(types="pdf"), FOLDERS, MONDAY).kinds == ()
    assert understood_from(form(exact="yes"), FOLDERS, MONDAY).exact is False, "only a real true ticks the box"
    assert understood_from({"words": "only this"}, FOLDERS, MONDAY) == Understood(words="only this"), "missing fields are empty fields"
    for nonsense in (None, "a string", ["a", "list"], 5):
        answer = understood_from(nonsense, FOLDERS, MONDAY)
        assert isinstance(answer, Failure) and answer.detail


def test_the_request_tells_the_model_the_day_and_the_folders():
    system, user = messages_for(f"find {helpers.CJK} notes", FOLDERS, MONDAY)
    assert system["role"] == "system" and "JSON object only" in system["content"]
    assert "Today is 2026-10-12 (Monday)" in user["content"] and "- D:\\Claude" in user["content"]
    assert user["content"].endswith(f"Sentence: find {helpers.CJK} notes")
    assert "(none)" in messages_for("x", [], MONDAY)[1]["content"]
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"]), "every field is required, so the model cannot leave one out"


class Scripted:
    def __init__(self, answer):
        self.answer, self.asked, self.closed = answer, [], False

    def complete(self, messages, schema):
        self.asked.append((messages, schema))
        return self.answer

    def close(self):
        self.closed = True


def test_the_parser_asks_once_and_checks_the_answer():
    backend = Scripted(form(types=["image"], period="today"))
    parser = IntentParser(backend, today=lambda: MONDAY)
    got = parser.understand("  photos of   nursing from today ", FOLDERS)
    assert got.kinds == ("image",) and (got.after, got.before) == ("2026-10-12", "2026-10-12")
    assert backend.asked[0][1] is SCHEMA and backend.asked[0][0][1]["content"].endswith("Sentence: photos of nursing from today")
    assert IntentParser(backend).understand("   ").reason is FailureReason.OTHER and len(backend.asked) == 1
    failing = IntentParser(Scripted(Failure(FailureReason.TIMED_OUT, "slow")))
    assert failing.understand("x") == Failure(FailureReason.TIMED_OUT, "slow")
    parser.close()
    assert backend.closed


# ---- a chat server -------------------------------------------------------------------------------------------

class Model:
    """A small web server inside the test. Each request is recorded and answered from a script."""

    def __init__(self):
        self.requests = []
        self.reply = lambda body: (200, {"choices": [{"message": {"content": json.dumps(form())}}]})
        self.hold = None            # when set, a request waits for this event before it is answered
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                model.requests.append((self.path, body))
                if model.hold is not None:
                    model.hold.wait()
                status, answer = model.reply(body)
                data = answer if isinstance(answer, bytes) else json.dumps(answer).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass                # the client gave up: that is what the test wanted to see

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.hold is not None:
            self.hold.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def model():
    made = Model()
    yield made
    made.close()


def test_a_request_asks_for_json_that_fits_the_schema(model):
    answer = ChatServer(model.url, "some-model").complete(messages_for(f"{helpers.CAFE} {helpers.CJK}", FOLDERS, MONDAY), SCHEMA)
    assert answer == form()
    path, body = model.requests[0]
    assert path == "/v1/chat/completions" and body["model"] == "some-model" and body["stream"] is False
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["messages"][1]["content"].endswith(f"{helpers.CAFE} {helpers.CJK}"), "non-ASCII text arrives whole"
    ChatServer(model.url + "/").complete([], SCHEMA)
    assert "model" not in model.requests[1][1], "no model name is sent when none is set"


def test_a_model_that_thinks_first_is_understood(model):
    model.reply = lambda body: (200, {"choices": [{"message": {"content": "", "reasoning_content": json.dumps(form(words="thought"))}}]})
    assert ChatServer(model.url).complete([], SCHEMA)["words"] == "thought"


@pytest.mark.parametrize("reply,reason", [
    (lambda body: (500, {"error": "boom"}), FailureReason.OTHER),
    (lambda body: (200, {"choices": [{"message": {"content": "Sure! Here is your search:"}}]}), FailureReason.OTHER),
    (lambda body: (200, {"choices": []}), FailureReason.OTHER),
    (lambda body: (200, b"\xff\xfe not json at all"), FailureReason.OTHER),
])
def test_every_bad_answer_is_a_stated_failure(model, reply, reason):
    model.reply = reply
    answer = ChatServer(model.url).complete([], SCHEMA)
    assert isinstance(answer, Failure) and answer.reason is reason and answer.detail


def test_a_model_that_does_not_answer_in_time_is_given_up_on(model):
    model.hold = threading.Event()         # the server has the request and sits on it until the test ends
    answer = ChatServer(model.url, seconds=0.3).complete([], SCHEMA)
    assert isinstance(answer, Failure) and answer.reason is FailureReason.TIMED_OUT and "0.3 s" in answer.detail
    assert len(model.requests) == 1, "the request did arrive; it was the answer that never came"


def test_no_server_at_the_address_is_said_plainly():
    port = intent.free_port()              # free a moment ago, and nothing was started on it
    answer = ChatServer(f"http://127.0.0.1:{port}").complete([], SCHEMA)
    assert isinstance(answer, Failure) and answer.reason is FailureReason.UNREACHABLE and str(port) in answer.detail


def test_what_you_type_is_not_sent_to_another_machine_unless_you_say_so():
    assert is_local("http://127.0.0.1:1234") and is_local("http://localhost:8080/") and is_local("http://[::1]:9")
    assert not is_local("http://192.168.1.20:1234") and not is_local("https://example.com") and not is_local("nonsense")
    sent = []

    def opener(request, timeout):
        sent.append(request.full_url)
        raise OSError("not really sent")

    refused = ChatServer("http://192.168.1.20:1234", opener=opener).complete([], SCHEMA)
    assert refused.reason is FailureReason.UNSUPPORTED and "intent_allow_remote" in refused.detail and sent == []
    ChatServer("http://192.168.1.20:1234", allow_remote=True, opener=opener).complete([], SCHEMA)
    assert sent == ["http://192.168.1.20:1234/v1/chat/completions"]
    assert ChatServer("ftp://127.0.0.1").complete([], SCHEMA).reason is FailureReason.UNSUPPORTED


# ---- a server the program starts itself ----------------------------------------------------------------------

def stand_in(args, **more):
    """Start the stand-in server program with the arguments a real one would get."""
    return launcher.launch([sys.executable, os.path.join(TESTS, "fake_model_server.py")] + list(args[1:]), **more)


def test_the_runtime_is_started_on_first_use_reused_and_ended(tmp_path):
    model_file = str(tmp_path / f"{helpers.CAFE} model.gguf")
    runtime = LocalRuntime("llama-server", model_file, launch=stand_in, gpu_layers=0)
    assert runtime._running is None, "nothing runs until it is needed"
    parser = IntentParser(runtime, today=lambda: MONDAY)
    try:
        got = parser.understand("budget reports", FOLDERS)
        assert got.words == "budget reports" and got.kinds == ("pdf",) and (got.after, got.smallest) == ("2026-09-01", "1m")
        pid = runtime._running.pid
        assert alive(pid)
        raw = runtime.complete(messages_for("again", [], MONDAY), SCHEMA)
        assert raw["served_by"] == model_file and raw["gpu_layers"] == "0", "the server got the model file and stays off the GPU"
        assert runtime._running.pid == pid, "the same server answers the second sentence"
    finally:
        parser.close()
    assert runtime._running is None
    from test_sandbox import wait_gone
    wait_gone([pid])       # closing the parser ended the server it had started


def test_the_arguments_keep_the_server_on_this_machine():
    args = LocalRuntime("C:\\Tools\\llama-server.exe", "D:\\models\\small.gguf", gpu_layers=7, extra_args=["--threads", "4"]).arguments(5123)
    assert args[:3] == ["C:\\Tools\\llama-server.exe", "--model", "D:\\models\\small.gguf"]
    assert args[args.index("--host") + 1] == "127.0.0.1" and args[args.index("--port") + 1] == "5123"
    assert args[args.index("--n-gpu-layers") + 1] == "7" and args[-2:] == ["--threads", "4"]


def test_a_server_that_cannot_load_its_model_is_a_stated_failure(tmp_path):
    runtime = LocalRuntime("llama-server", str(tmp_path / "broken.gguf"), launch=stand_in)
    answer = runtime.complete([], SCHEMA)
    assert isinstance(answer, Failure) and answer.reason is FailureReason.CRASHED and "exit code 3" in answer.detail
    assert runtime._running is None


def test_a_missing_server_program_is_named(tmp_path):
    answer = LocalRuntime(str(tmp_path / "no-such-llama-server"), "model.gguf").complete([], SCHEMA)
    assert isinstance(answer, Failure) and answer.reason is FailureReason.NOT_INSTALLED and "no-such-llama-server" in answer.detail


def test_a_server_that_never_becomes_ready_is_ended_after_its_time():
    now = [0.0]
    closed = []

    class NeverReady:
        pid = 1

        def poll(self):
            return None

        def close(self):
            closed.append(True)

    def refuse(request, timeout):
        raise OSError("connection refused")

    runtime = LocalRuntime("llama-server", "m.gguf", start_seconds=5.0, launch=lambda args, **more: NeverReady(),
                           opener=refuse, timer=lambda: now[0], pause=lambda seconds: now.__setitem__(0, now[0] + seconds))
    answer = runtime.ensure()
    assert isinstance(answer, Failure) and answer.reason is FailureReason.TIMED_OUT and closed == [True]


def test_settings_choose_how_the_model_is_reached():
    assert from_settings(Settings()) is None, "nothing set up: the feature does not exist"
    assert from_settings(Settings(intent_server="llama-server")) is None, "a server program without a model file is not enough"
    remote = from_settings(Settings(intent_url="http://127.0.0.1:1234", intent_model="m", intent_seconds=9))
    assert isinstance(remote.backend, ChatServer) and (remote.backend.model, remote.backend.seconds) == ("m", 9)
    local = from_settings(Settings(intent_server="llama-server", intent_model_file="m.gguf", intent_gpu_layers=3))
    assert isinstance(local.backend, LocalRuntime) and local.backend.gpu_layers == 3 and local.backend._running is None
    with pytest.raises(ValueError):
        Settings(intent_seconds=0)
