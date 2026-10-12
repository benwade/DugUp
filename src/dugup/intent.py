"""Plain words to a search form: "PDFs about nursing from last month over 1 MB" becomes words and filters.

Optional. With nothing set up, this part does not exist as far as the rest of the program is concerned.

A small language model does the reading. It is asked for one JSON object with a fixed shape (a JSON
schema), over the chat interface that both llama.cpp's server and LM Studio offer, so either can stand
behind it. The answer is never trusted: every field is checked, anything that makes no sense is dropped
with a note, and the result only FILLS IN the search form, where the person sees it before searching.

Two ways to reach a model, chosen by settings:
  LocalRuntime   the program starts a model server itself (through the launcher, on this machine only,
                 on a free port) and ends it when it closes;
  a server URL   a server that is already running, for example LM Studio.
Both are just "a place that answers chat requests" to the parser, so a third way is one small class.

Small models are bad at arithmetic with dates, so the model is not asked for dates when the person
said something relative. It names a period ("last_month") and the dates are worked out here.

Privacy: the sentence the person typed is sent to the model server. The server must be on this machine
unless a setting says otherwise.
"""

from __future__ import annotations

import datetime
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import launcher
from .settings import Settings
from .shapes import WHERE_ANYWHERE, WHERE_CHOICES, Failure, FailureReason, FileKind

PERIODS = ("", "today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_year", "last_year")
KINDS = (FileKind.PDF.value, FileKind.OFFICE.value, FileKind.TEXT.value, FileKind.IMAGE.value)
SIZE_UNITS = ("kb", "mb", "gb")
LOOPBACK = ("127.0.0.1", "localhost", "::1")

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "words": {"type": "string", "description": "the words to look for; a phrase that must stand as typed goes in double quotes"},
        "types": {"type": "array", "items": {"type": "string", "enum": list(KINDS)},
                  "description": "kinds of file asked for; empty when none was named"},
        "where": {"type": "string", "enum": list(WHERE_CHOICES)},
        "period": {"type": "string", "enum": list(PERIODS), "description": "a time of last change said in relative words"},
        "modified_after": {"type": "string", "description": "YYYY-MM-DD, only when an explicit date was given; else empty"},
        "modified_before": {"type": "string", "description": "YYYY-MM-DD, only when an explicit date was given; else empty"},
        "min_size": {"type": "number", "description": "0 when no smallest size was given"},
        "min_size_unit": {"type": "string", "enum": list(SIZE_UNITS)},
        "max_size": {"type": "number", "description": "0 when no largest size was given"},
        "max_size_unit": {"type": "string", "enum": list(SIZE_UNITS)},
        "folder": {"type": "string", "description": "one of the listed folders, exactly as listed; empty when none was named"},
        "exact": {"type": "boolean", "description": "true only when the exact form of the words was asked for"},
    },
    "required": ["words", "types", "where", "period", "modified_after", "modified_before", "min_size", "min_size_unit",
                 "max_size", "max_size_unit", "folder", "exact"],
    "additionalProperties": False,
}

INSTRUCTIONS = (
    "You turn one sentence about finding files into a search form. Answer with the JSON object only.\n"
    "- words: only the topic words to look for inside files or their names. Leave out words that say a kind of "
    "file, a date, a size or a folder. Keep a phrase the person put in quotes inside double quotes.\n"
    "- types: pdf, office (Word, Excel, PowerPoint and the like), text (plain text, notes, web pages), "
    "image (pictures, photos, scans, screenshots). Empty when no kind was named.\n"
    "- where: name when the words must be in the file NAME, ocr when they must be in a picture or scan, "
    "text when they must be in the text, otherwise anywhere.\n"
    "- period: use it for relative times (today, yesterday, this week, last week, this month, last month, "
    "this year, last year). Use modified_after and modified_before only for dates the person wrote out.\n"
    "- sizes: the number and its unit as said; 0 when no size was given.\n"
    "- folder: only one of the folders listed, copied exactly; otherwise empty.\n"
    "- exact: true only when the person asked for the exact word."
)


@dataclass(frozen=True)
class Understood:
    """A sentence as the search form's fields, each in the form the window's boxes take.
    `notes` says what the model answered that was not usable, so nothing is dropped silently."""

    words: str = ""
    folder: str = ""
    where: str = WHERE_ANYWHERE
    kinds: Tuple[str, ...] = ()        # empty means every kind
    after: str = ""                    # YYYY-MM-DD, "on or after"
    before: str = ""                   # YYYY-MM-DD, "up to and including"
    smallest: str = ""                 # as typed in the size boxes: "1m", "200k"
    largest: str = ""
    exact: bool = False
    notes: Tuple[str, ...] = ()

    def describe(self) -> str:
        parts = [f"words: {self.words}" if self.words else "no words"]
        if self.kinds:
            parts.append("types: " + ", ".join(self.kinds))
        if self.where != WHERE_ANYWHERE:
            parts.append(f"words in: {self.where}")
        if self.after or self.before:
            parts.append(f"modified {self.after or 'any time'} to {self.before or 'now'}")
        if self.smallest:
            parts.append(f"at least {self.smallest}")
        if self.largest:
            parts.append(f"at most {self.largest}")
        if self.folder:
            parts.append(f"in {self.folder}")
        if self.exact:
            parts.append("exact form")
        return "; ".join(parts)


def period_days(period: str, today: datetime.date) -> Tuple[str, str]:
    """(first day, last day) of a named period, as YYYY-MM-DD; ("", "") for no period. Weeks start on Monday."""
    day = datetime.timedelta(days=1)
    if period == "today":
        first = last = today
    elif period == "yesterday":
        first = last = today - day
    elif period == "this_week":
        first, last = today - today.weekday() * day, today
    elif period == "last_week":
        last = today - (today.weekday() + 1) * day
        first = last - 6 * day
    elif period == "this_month":
        first, last = today.replace(day=1), today
    elif period == "last_month":
        last = today.replace(day=1) - day
        first = last.replace(day=1)
    elif period == "this_year":
        first, last = today.replace(month=1, day=1), today
    elif period == "last_year":
        first, last = datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31)
    else:
        return "", ""
    return first.isoformat(), last.isoformat()


def _day(value: object) -> Optional[str]:
    """A date the model wrote, as YYYY-MM-DD; "" for none; None when it is not a date."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _size(number: object, unit: object) -> Optional[str]:
    """A size as the size boxes take it ("1.5m"); "" for none; None when it makes no sense."""
    try:
        amount = float(number or 0)     # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if amount != amount or amount < 0 or amount == float("inf"):
        return None
    if amount == 0:
        return ""
    letter = {"kb": "k", "mb": "m", "gb": "g"}.get(str(unit or "mb").lower())
    if letter is None:
        return None
    return f"{amount:g}{letter}"


def understood_from(data: object, folders: Sequence[str], today: datetime.date) -> "Understood | Failure":
    """Check what the model answered, field by field. A field that makes no sense is left empty and noted."""
    if not isinstance(data, Mapping):
        return Failure(FailureReason.OTHER, "the model did not answer with a form")
    notes: List[str] = []
    words = " ".join(str(data.get("words") or "").split())
    raw_types = data.get("types") if isinstance(data.get("types"), (list, tuple)) else []
    kinds = tuple(dict.fromkeys(k for k in (str(t).lower() for t in raw_types) if k in KINDS))
    if len(kinds) != len(set(str(t).lower() for t in raw_types)):
        notes.append("a kind of file it named is not one this program knows")
    if set(kinds) == set(KINDS):
        kinds = ()
    where = str(data.get("where") or WHERE_ANYWHERE).lower()
    if where not in WHERE_CHOICES:
        notes.append(f"it did not say clearly where the words must be ({where!r})")
        where = WHERE_ANYWHERE
    after, before = _day(data.get("modified_after")), _day(data.get("modified_before"))
    if after is None or before is None:
        notes.append("a date it gave could not be read")
    after, before = after or "", before or ""
    period = str(data.get("period") or "").lower()
    if period and period not in PERIODS:
        notes.append(f"a time it named is not one this program knows ({period!r})")
    elif period:
        after, before = period_days(period, today)      # worked out here: more reliable than the model's dates
    if after and before and after > before:
        notes.append("the dates it gave are in the wrong order")
        after = before = ""
    smallest = _size(data.get("min_size"), data.get("min_size_unit"))
    largest = _size(data.get("max_size"), data.get("max_size_unit"))
    if smallest is None or largest is None:
        notes.append("a size it gave could not be read")
    folder = str(data.get("folder") or "").strip()
    if folder:
        match = next((f for f in folders if f.casefold().rstrip("/\\") == folder.casefold().rstrip("/\\")), None)
        if match is None:
            notes.append("the folder it named is not one of the indexed folders")
        folder = match or ""
    if not words:
        notes.append("it found no words to look for")
    return Understood(words, folder, where, kinds, after, before, smallest or "", largest or "",
                      data.get("exact") is True, tuple(notes))


def messages_for(text: str, folders: Sequence[str], today: datetime.date) -> List[Dict[str, str]]:
    listed = "\n".join(f"- {f}" for f in folders) if folders else "(none)"
    return [{"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": f"Today is {today.isoformat()} ({today.strftime('%A')}).\nFolders:\n{listed}\n\nSentence: {text}"}]


# ---- reaching a model ---------------------------------------------------------------------------------------

def is_local(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host.lower() in LOOPBACK


class ChatServer:
    """A server that answers chat requests in the OpenAI style (llama.cpp's server, LM Studio)."""

    def __init__(self, url: str, model: str = "", seconds: float = 30.0, allow_remote: bool = False,
                 opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.seconds = seconds
        self.allow_remote = allow_remote
        self.opener = opener

    def ready(self) -> "str | Failure":
        if not self.url.lower().startswith(("http://", "https://")):
            return Failure(FailureReason.UNSUPPORTED, f"the model server's address is not a web address: {self.url!r}")
        if not self.allow_remote and not is_local(self.url):
            return Failure(FailureReason.UNSUPPORTED,
                           "the model server is not on this machine, and what you type would be sent to it. "
                           "Set intent_allow_remote = true in config.toml if that is what you want")
        return self.url

    def complete(self, messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any]) -> "object | Failure":
        """Ask for one JSON object that fits the schema. Returns it parsed, or why there is none."""
        base = self.ready()
        if isinstance(base, Failure):
            return base
        body: Dict[str, Any] = {
            "messages": list(messages), "temperature": 0, "max_tokens": 400, "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": "search_form", "strict": True, "schema": dict(schema)}},
        }
        if self.model:
            body["model"] = self.model
        request = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener(request, timeout=self.seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            return Failure(FailureReason.OTHER, f"the model server refused the request (HTTP {exc.code})")
        except (socket.timeout, TimeoutError):
            return Failure(FailureReason.TIMED_OUT, f"the model did not answer within {self.seconds:g} s")
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return Failure(FailureReason.TIMED_OUT, f"the model did not answer within {self.seconds:g} s")
            return Failure(FailureReason.UNREACHABLE, f"the model server could not be reached at {base} ({reason})")
        try:
            message = json.loads(raw.decode("utf-8"))["choices"][0]["message"]
            # a model that thinks first may put its answer in reasoning_content and leave content empty
            text = message.get("content") or message.get("reasoning_content") or ""
            return json.loads(text)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, UnicodeDecodeError):
            return Failure(FailureReason.OTHER, "the model's answer was not the JSON that was asked for")

    def close(self) -> None:
        pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LocalRuntime:
    """A model server this program starts for itself and ends when it closes: llama.cpp's `llama-server`
    (or anything that takes the same arguments), listening on this machine only.

    Started on first use, so a person who never uses the plain-words box never pays for it.
    """

    def __init__(self, program: str, model_file: str, seconds: float = 30.0, start_seconds: float = 120.0,
                 gpu_layers: int = 0, extra_args: Sequence[str] = (), launch: Callable[..., Any] = launcher.launch,
                 opener: Callable[..., Any] = urllib.request.urlopen, timer: Callable[[], float] = time.monotonic,
                 pause: Callable[[float], None] = time.sleep, port: Optional[int] = None) -> None:
        self.program, self.model_file = program, model_file
        self.seconds, self.start_seconds = seconds, start_seconds
        self.gpu_layers = gpu_layers
        self.extra_args = list(extra_args)
        self.launch, self.opener, self.timer, self.pause = launch, opener, timer, pause
        self.port = port
        self._running: Any = None
        self._server: Optional[ChatServer] = None
        self._lock = threading.Lock()

    def arguments(self, port: int) -> List[str]:
        return [self.program, "--model", self.model_file, "--host", "127.0.0.1", "--port", str(port),
                "--ctx-size", "2048", "--n-gpu-layers", str(self.gpu_layers)] + self.extra_args

    def _healthy(self, base: str) -> bool:
        try:
            with self.opener(urllib.request.Request(base + "/health"), timeout=2.0) as response:
                return 200 <= int(getattr(response, "status", 200)) < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def ensure(self) -> "ChatServer | Failure":
        """The running server, started now if need be. Waits until it says it is ready (loading a model takes a while)."""
        with self._lock:
            if self._server is not None and self._running is not None and self._running.poll() is None:
                return self._server
            self._stop()
            port = self.port or free_port()
            started = self.launch(self.arguments(port), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            if isinstance(started, Failure):
                if started.reason is FailureReason.NOT_INSTALLED:
                    return Failure(FailureReason.NOT_INSTALLED, f"the model server program was not found: {self.program}")
                return started
            self._running = started
            base = f"http://127.0.0.1:{port}"
            deadline = self.timer() + self.start_seconds
            while not self._healthy(base):
                code = started.poll()
                if code is not None:
                    self._stop()
                    return Failure(FailureReason.CRASHED,
                                   f"the model server ended as soon as it started (exit code {code}); "
                                   "check the model file and the program in config.toml")
                if self.timer() >= deadline:
                    self._stop()
                    return Failure(FailureReason.TIMED_OUT, f"the model server was not ready within {self.start_seconds:g} s")
                self.pause(0.2)
            self._server = ChatServer(base, "", self.seconds, opener=self.opener)
            return self._server

    def complete(self, messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any]) -> "object | Failure":
        server = self.ensure()
        if isinstance(server, Failure):
            return server
        return server.complete(messages, schema)

    def _stop(self) -> None:
        if self._running is not None:
            self._running.close()        # the launcher ends the server and everything it started
        self._running, self._server = None, None

    def close(self) -> None:
        with self._lock:
            self._stop()


class IntentParser:
    """`backend` is anything with complete(messages, schema) and close(): a ChatServer, a LocalRuntime, a fake."""

    def __init__(self, backend, today: Callable[[], datetime.date] = datetime.date.today) -> None:
        self.backend = backend
        self.today = today

    def understand(self, text: str, folders: Sequence[str] = ()) -> "Understood | Failure":
        sentence = " ".join(text.split())
        if not sentence:
            return Failure(FailureReason.OTHER, "there is no sentence to read")
        today = self.today()
        answer = self.backend.complete(messages_for(sentence, folders, today), SCHEMA)
        if isinstance(answer, Failure):
            return answer
        return understood_from(answer, folders, today)

    def close(self) -> None:
        self.backend.close()


def from_settings(settings: Settings, installed: Optional[Mapping[str, str]] = None) -> Optional[IntentParser]:
    """Who reads a sentence typed in plain words. By default the built-in rules (plainwords.py).
    With intent_reader = "model": a server named in the settings, else the runtime and model that
    `dugup models install` put in place, else None (asked for, but nothing to run it with)."""
    if settings.intent_reader != "model":
        from .plainwords import RuleReader       # the default: built in, nothing to set up
        return RuleReader()
    if settings.intent_url:
        return IntentParser(ChatServer(settings.intent_url, settings.intent_model, settings.intent_seconds,
                                       settings.intent_allow_remote))
    if settings.intent_server and settings.intent_model_file:
        return IntentParser(LocalRuntime(settings.intent_server, settings.intent_model_file, settings.intent_seconds,
                                         settings.intent_start_seconds, settings.intent_gpu_layers))
    if installed and installed.get("server") and installed.get("model_file"):
        try:
            patience = max(settings.intent_seconds, float(installed.get("seconds") or 0))   # a larger model is given longer
        except (TypeError, ValueError):
            patience = settings.intent_seconds
        return IntentParser(LocalRuntime(installed["server"], installed["model_file"], patience,
                                         settings.intent_start_seconds, settings.intent_gpu_layers))
    return None
