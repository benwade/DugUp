"""Plain words to a search form, by rules: no model, no download, the same answer every time.

"PDFs about nursing from last month bigger than 1 MB" holds four separate things, and three of them are
said in a small, regular vocabulary: a kind of file, a time, a size. Those are found here by pattern,
taken out of the sentence, and what is left (minus filler such as "find", "about", "files") is the words
to look for.

This is the default reader for the window's "In plain words" box. A language model can stand in its
place (intent.py, the `intent_reader` setting); measured on the smallest such model, the rules were the
more reliable of the two for these fields (EXPERIMENT_LOG).

English only. A sentence it cannot make sense of still gives its words back, so the person is never
worse off than typing them in the Words box.
"""

from __future__ import annotations

import datetime
import re
from typing import Callable, List, Optional, Sequence, Tuple

from .intent import Understood, period_days
from .shapes import WHERE_ANYWHERE, WHERE_NAME, WHERE_OCR, WHERE_TEXT, Failure, FailureReason, FileKind

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
_MONTH = "|".join(MONTHS) + "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_NUMBER = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
           "a": 1, "an": 1, "couple of": 2, "few": 3}

# kinds of file: each pattern is a way of naming the kind
KIND_WORDS: Tuple[Tuple[str, str], ...] = (
    (FileKind.PDF.value, r"pdfs?(?:\s+(?:files?|documents?))?"),
    # "word" alone is too common a word to mean the program: it needs "files" or "documents" after it
    (FileKind.OFFICE.value, r"(?:ms\s+|microsoft\s+)?word\s+(?:files?|documents?|docs?)"
                            r"|(?:excel|powerpoint|office|libreoffice|openoffice)(?:\s+(?:files?|documents?|docs?|sheets?))?"
                            r"|spreadsheets?|presentations?|slide\s?decks?|slides|docx|xlsx?|pptx?|od[tsp]"),
    (FileKind.TEXT.value, r"(?:plain\s+)?text\s+files?|txt(?:\s+files?)?|markdown(?:\s+files?)?|web\s?pages?|html(?:\s+files?)?"),
    (FileKind.IMAGE.value, r"pictures?|photos?|photographs?|images?|scans?|scanned\s+(?:pages?|documents?|files?)|screenshots?|screen\s+shots?"),
)
_SIZE_UNIT = r"(kb|k|kilobytes?|mb|m|megs?|megabytes?|gb|g|gigs?|gigabytes?|bytes?|b)"
_AMOUNT = r"(\d+(?:[.,]\d+)?)\s*" + _SIZE_UNIT + r"\b"
_MORE = r"(?:bigger|larger|greater|more|heavier)\s+than|over|above|at\s+least|exceeding|>\s*=?|no\s+(?:smaller|less)\s+than|minimum(?:\s+of)?"
_LESS = r"(?:smaller|less|lighter|fewer)\s+than|under|below|at\s+most|up\s+to|<\s*=?|no\s+(?:bigger|larger|more)\s+than|maximum(?:\s+of)?"
_DATE = r"(\d{4}-\d{1,2}-\d{1,2})"
FILLER = (
    r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(?:find|show|search(?:\s+for)?|look(?:ing)?(?:\s+for)?|get|list|give|fetch|locate|open|"
    r"i\s+(?:need|want|am\s+looking\s+for|'m\s+looking\s+for)|where\s+(?:is|are|was|were)|what\s+(?:is|are)|which)\b",
)
FILLER_WORDS = frozenset(
    "a an the my me our all any anything everything something some of and or that which who whose with within about on in at "
    "for from to by regarding concerning mentioning mention mentions mentioned containing contain contains contained including "
    "include includes have has had is are was were be been i we you it its this these those there here file files document "
    "documents doc docs stuff things thing item items ones one wrote written made created saved changed modified edited "
    "word words called named titled say says saying said talk talks talking discuss discusses discussing related relating "
    "please find show search look looking get list give fetch locate can could would like need want as than then".split())


def _size_text(number: str, unit: str) -> str:
    amount = float(number.replace(",", "."))
    letter = unit[0].lower()
    if letter == "b":                       # plain bytes: the size boxes take a bare number
        return f"{amount:g}"
    return f"{amount:g}{letter}"


def _month_range(year: int, month: int) -> Tuple[str, str]:
    first = datetime.date(year, month, 1)
    after = datetime.date(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1)
    return first.isoformat(), after.isoformat()


def _month_number(name: str) -> int:
    return next(i for i, month in enumerate(MONTHS, 1) if month.startswith(name.lower()[:3]))


def _valid_day(text: str) -> Optional[str]:
    try:
        year, month, day = (int(part) for part in text.split("-"))
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


class _Sentence:
    """The sentence being read. Each thing that is recognised is cut out, so it cannot be read twice
    and does not end up among the words to look for."""

    def __init__(self, text: str) -> None:
        self.quoted: List[str] = []
        if text.count('"') % 2 == 0:        # phrases in quotes are the person's exact words: set them aside untouched

            def keep(match: "re.Match[str]") -> str:
                phrase = " ".join(match.group(1).split())
                if phrase:
                    self.quoted.append(phrase)
                return " \x00 "             # a marker that keeps the phrase's place in the sentence

            text = re.sub(r'"([^"]*)"', keep, text)
        self.text = " " + " ".join(text.split()) + " "

    def take(self, pattern: str) -> Optional["re.Match[str]"]:
        match = re.search(r"(?<![\w-])(?:" + pattern + r")(?![\w-])", self.text, re.IGNORECASE)
        if match is not None:
            self.text = self.text[:match.start()] + " " + self.text[match.end():]
        return match

    def take_all(self, pattern: str) -> List["re.Match[str]"]:
        found = []
        while True:
            match = self.take(pattern)
            if match is None:
                return found
            found.append(match)


def read(text: str, folders: Sequence[str] = (), today: Optional[datetime.date] = None) -> Understood:
    """A sentence as search-form fields. Never fails: at worst every word is a word to look for."""
    today = today or datetime.date.today()
    s = _Sentence(text)
    notes: List[str] = []

    exact = s.take(r"(?:the\s+)?exact(?:ly)?(?:\s+(?:word|words|form|match|spelling))?(?:\s+only)?") is not None

    # where the words must be
    where = WHERE_ANYWHERE
    if s.take(r"(?:(?:with|having|that\s+have|whose)\s+)?(?:in|inside)\s+(?:the\s+|their\s+|its\s+)?(?:file\s*)?names?"
              r"|(?:file\s*)?names?\s+(?:contains?|containing|has|have|having|includes?|including|with)"
              r"|(?:files?\s+)?(?:named|called|titled)|whose\s+names?\s+(?:contains?|has|have|includes?)|by\s+name"):
        where = WHERE_NAME
    elif s.take(r"on\s+(?:a|the)\s+(?:sign|label|poster|banner|whiteboard|receipt)s?"
                r"|(?:in|inside|on|from)\s+(?:a|an|the|my|some)?\s*(?:pictures?|photos?|photographs?|images?|scans?|screenshots?)"
                r"|(?:read\s+)?by\s+ocr|ocr(?:\s+text)?"):
        where = WHERE_OCR
    elif s.take(r"(?:in|inside)\s+(?:the\s+)?(?:text|contents?|body)(?:\s+of\s+(?:the\s+)?(?:files?|documents?))?"):
        where = WHERE_TEXT

    # sizes
    smallest = largest = ""
    for match in s.take_all(r"(?:" + _MORE + r")\s*" + _AMOUNT):
        smallest = _size_text(match.group(1), match.group(2))
    for match in s.take_all(r"(?:" + _LESS + r")\s*" + _AMOUNT):
        largest = _size_text(match.group(1), match.group(2))
    between = s.take(r"between\s+" + _AMOUNT + r"\s+and\s+" + _AMOUNT)
    if between is not None:
        smallest, largest = _size_text(between.group(1), between.group(2)), _size_text(between.group(3), between.group(4))
    if s.take(r"(?:big|large|huge)\s+(?:files?|ones)") and not smallest:
        notes.append('"big" is not a size; say one, such as "over 10 MB"')

    # times
    after = before = ""
    span = s.take(r"between\s+" + _DATE + r"\s+and\s+" + _DATE)
    if span is not None:
        after, before = _valid_day(span.group(1)) or "", _valid_day(span.group(2)) or ""
    for match in s.take_all(r"(?:since|after|from|starting|on\s+or\s+after)\s+" + _DATE):
        after = _valid_day(match.group(1)) or after
    for match in s.take_all(r"(?:before|until|till|up\s+to|to|on\s+or\s+before)\s+" + _DATE):
        before = _valid_day(match.group(1)) or before
    single = s.take(r"(?:on|dated)\s+" + _DATE)
    if single is not None and _valid_day(single.group(1)):
        after = before = _valid_day(single.group(1)) or ""
    recent = s.take(r"(?:(?:in|over|during|within|from)\s+)?(?:the\s+)?(?:last|past|previous)\s+(\d+|" + "|".join(_NUMBER)
                    + r")\s+(day|week|month|year)s?")
    if recent is not None:
        count = int(recent.group(1)) if recent.group(1).isdigit() else _NUMBER[recent.group(1).lower()]
        days = count * {"day": 1, "week": 7, "month": 30, "year": 365}[recent.group(2).lower()]
        after, before = (today - datetime.timedelta(days=days)).isoformat(), today.isoformat()
    named = s.take(r"(?:(?:from|in|during|since|of|for)\s+)?(?:earlier\s+)?(today|yesterday|(?:this|last)\s+(?:week|month|year))")
    if named is not None:
        after, before = period_days("_".join(named.group(1).lower().split()), today)
    # a month counts only with a word before it ("in March") or a year after it ("March 2026"):
    # "may" and "march" are ordinary words too
    month = (s.take(r"(?:from|in|during|of|for|since)\s+(" + _MONTH + r")\.?(?:\s+(?:of\s+)?(\d{4}))?(?=\s)")
             or s.take(r"(" + _MONTH + r")\.?\s+(?:of\s+)?(\d{4})(?=\s)"))
    if month is not None and not (after or before):
        number = _month_number(month.group(1))
        year = int(month.group(2)) if month.group(2) else (today.year if number <= today.month else today.year - 1)
        after, before = _month_range(year, number)
    year_only = s.take(r"(?:from|in|during|of)\s+(?:the\s+year\s+)?((?:19|20)\d{2})")
    if year_only is not None and not (after or before):
        after, before = f"{year_only.group(1)}-01-01", f"{year_only.group(1)}-12-31"
    if after and before and after > before:
        notes.append("the dates are in the wrong order, so they were left out")
        after = before = ""

    # an order: "most recent", "newest", "latest", "oldest", "biggest" are not words to look for
    sort = ""
    if s.take(r"(?:the\s+)?(?:most\s+recent(?:ly)?(?:\s+(?:changed|modified|edited|saved|updated))?|newest|latest|"
              r"(?:most\s+)?recent(?:ly)?(?:\s+(?:changed|modified|edited|saved|updated))?)(?:\s+(?:version|copy|one|ones))?"):
        sort = "newest"
    elif s.take(r"(?:the\s+)?(?:oldest|earliest|first)(?:\s+(?:version|copy|one|ones))?"):
        sort = "oldest"
    elif s.take(r"(?:the\s+)?(?:biggest|largest)(?:\s+(?:version|copy|one|ones|files?))?") and not (smallest or largest):
        sort = "largest"
    elif s.take(r"(?:the\s+)?(?:smallest|tiniest)(?:\s+(?:version|copy|one|ones|files?))?") and not (smallest or largest):
        sort = "smallest"

    # kinds of file
    kinds: List[str] = []
    for kind, pattern in KIND_WORDS:
        if s.take_all(pattern):
            kinds.append(kind)
    if where == WHERE_OCR and not kinds:
        kinds.append(FileKind.IMAGE.value) if re.search(r"sign|label|poster|banner|whiteboard|receipt", text, re.IGNORECASE) else None

    # a folder, named by its last part or in full
    folder = ""
    for candidate in sorted(folders, key=len, reverse=True):
        short = candidate.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
        for name in (candidate, short):
            if name and s.take(r"(?:in|under|inside|from|within)\s+(?:the\s+|my\s+)?(?:folder\s+)?" + re.escape(name) + r"(?:\s+folder)?"):
                folder = candidate
                break
        if folder:
            break

    # what is left is the words to look for
    rest = s.text
    for pattern in FILLER:
        rest = re.sub(pattern, " ", rest.strip(), flags=re.IGNORECASE)
    words: List[str] = []
    phrases = iter(s.quoted)
    for token in rest.split():
        if token == "\x00":
            words.append('"' + next(phrases) + '"')
            continue
        bare = token.strip(".,;:!?()[]{}'\"")
        if bare and bare.lower() not in FILLER_WORDS and re.search(r"\w", bare):
            words.append(bare)
    if not words:
        notes.append("no words to look for were found in the sentence")
    return Understood(" ".join(words), folder, where, tuple(kinds), after, before, smallest, largest, exact, tuple(notes), sort)


class RuleReader:
    """The rules as a reader, with the same two methods a model-backed one has."""

    def __init__(self, today: Callable[[], datetime.date] = datetime.date.today) -> None:
        self.today = today

    def understand(self, text: str, folders: Sequence[str] = ()) -> "Understood | Failure":
        if not text.strip():
            return Failure(FailureReason.OTHER, "there is no sentence to read")
        return read(text, folders, self.today())

    def close(self) -> None:
        pass
