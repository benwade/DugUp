"""Plain words to a search form, by rules. A table of sentences and what each must become."""

import datetime

import pytest

import helpers
from dugup.intent import Understood, from_settings
from dugup.plainwords import RuleReader, read
from dugup.settings import Settings
from dugup.shapes import Failure

TODAY = datetime.date(2026, 10, 12)       # a Monday
FOLDERS = ["D:\\Claude", "/home/me/Documents", f"/home/me/{helpers.CAFE} notes"]


def U(words="", **more):
    return Understood(words=words, **more)


@pytest.mark.parametrize("sentence,expected", [
    # the sentences tried on the small model (EXPERIMENT_LOG): the rules get all seven
    ("PDFs about nursing from last month bigger than 1 MB",
     U("nursing", kinds=("pdf",), after="2026-09-01", before="2026-09-30", smallest="1m")),
    ("photos with the word bakery on a sign", U("bakery", kinds=("image",), where="ocr")),
    ("find education in file names", U("education", where="name")),
    ("notes about the harbour meeting from this year", U("notes harbour meeting", after="2026-01-01", before="2026-10-12")),
    ("spreadsheets with budget 2024 under 500 kb", U("budget 2024", kinds=("office",), largest="500k")),
    ("anything about cochlear implants I wrote yesterday", U("cochlear implants", after="2026-10-11", before="2026-10-11")),
    ("text files mentioning Saurav and embeddings", U("Saurav embeddings", kinds=("text",))),
    # kinds
    ("word documents about the garden fence", U("garden fence", kinds=("office",))),
    ("the word garden in pdf files", U("garden", kinds=("pdf",))),
    ("excel sheets and powerpoint slides on revenue", U("revenue", kinds=("office",))),
    ("pdfs and pictures of receipts", U("receipts", kinds=("pdf", "image"))),
    ("scanned pages about the lease", U("lease", kinds=("image",))),
    ("screenshots of the error message", U("error message", kinds=("image",))),
    ("web pages about tkinter", U("tkinter", kinds=("text",))),
    # where
    ("files named invoice", U("invoice", where="name")),
    ("documents whose name contains draft", U("draft", where="name")),
    ("budget in the text", U("budget", where="text")),
    ("harbour in a photo", U("harbour", where="ocr")),
    ("the word exit on the sign", U("exit", kinds=("image",), where="ocr")),
    # sizes
    ("videos over 2.5 GB", U("videos", smallest="2.5g")),
    ("reports smaller than 200k", U("reports", largest="200k")),
    ("logs at least 10 megabytes", U("logs", smallest="10m")),
    ("files named report between 1 mb and 5 mb", U("report", where="name", smallest="1m", largest="5m")),
    ("notes under 4000 bytes", U("notes", largest="4000")),
    ("big files about taxes", U("taxes", notes=('"big" is not a size; say one, such as "over 10 MB"',))),
    # times
    ("minutes from today", U("minutes", after="2026-10-12", before="2026-10-12")),
    ("minutes from last week", U("minutes", after="2026-10-05", before="2026-10-11")),
    ("minutes this month", U("minutes", after="2026-10-01", before="2026-10-12")),
    ("minutes from last year", U("minutes", after="2025-01-01", before="2025-12-31")),
    ("minutes from the last 2 weeks", U("minutes", after="2026-09-28", before="2026-10-12")),
    ("minutes in the past three days", U("minutes", after="2026-10-09", before="2026-10-12")),
    ("minutes since 2026-01-15", U("minutes", after="2026-01-15")),
    ("minutes before 2026-02-01", U("minutes", before="2026-02-01")),
    ("minutes between 2026-01-01 and 2026-03-31", U("minutes", after="2026-01-01", before="2026-03-31")),
    ("minutes dated 2026-07-04", U("minutes", after="2026-07-04", before="2026-07-04")),
    ("minutes from March", U("minutes", after="2026-03-01", before="2026-03-31")),
    ("minutes from December", U("minutes", after="2025-12-01", before="2025-12-31")),      # a month still to come: last year's
    ("minutes in Feb 2028", U("minutes", after="2028-02-01", before="2028-02-29")),
    ("minutes in 2024", U("minutes", after="2024-01-01", before="2024-12-31")),
    ("the budget 2024 report", U("budget 2024 report")),                                   # a bare year is a word
    ("May I see the may report", U("May see may report")),                                 # "may" is not a month here
    ("the march of progress", U("march progress")),
    ("minutes since 2026-13-45", U("minutes")),                                            # not a date: left out
    ("minutes since 2026-05-01 before 2026-04-01",
     U("minutes", notes=("the dates are in the wrong order, so they were left out",))),
    # exact, quotes, folders
    ("the exact word education", U("education", exact=True)),
    ('word documents containing "care plan" since 2026-01-15', U('"care plan"', kinds=("office",), after="2026-01-15")),
    ('"last month" in pdf files', U('"last month"', kinds=("pdf",))),                      # a quoted phrase is never read as a filter
    ("receipts in Documents", U("receipts", folder="/home/me/Documents")),
    ("receipts in the claude folder", U("receipts", folder="D:\\Claude")),
    (f"recipes in {helpers.CAFE} notes", U("recipes", folder=f"/home/me/{helpers.CAFE} notes")),
    ("receipts in Elsewhere", U("receipts Elsewhere")),                                    # not an indexed folder: a word
    # plain words stay plain words
    ("education policy", U("education policy")),
    ("where is my resume", U("resume")),
    (f"find {helpers.CJK} {helpers.CAFE}", U(f"{helpers.CJK} {helpers.CAFE}")),
    ("R2-D2 schematics", U("R2-D2 schematics")),
])
def test_sentences(sentence, expected):
    assert read(sentence, FOLDERS, TODAY) == expected


def test_a_sentence_with_only_filters_says_no_words_were_found():
    got = read("pdfs from last month", FOLDERS, TODAY)
    assert got.words == "" and got.kinds == ("pdf",) and "no words to look for" in got.notes[0]


def test_an_unclosed_quote_is_not_an_error():
    assert read('find "care plan', FOLDERS, TODAY).words == "care plan"


def test_the_reader_is_the_default_and_needs_nothing_set_up():
    reader = from_settings(Settings())
    assert isinstance(reader, RuleReader)
    got = reader.understand("pdfs about nursing", FOLDERS)
    assert got.words == "nursing" and got.kinds == ("pdf",)
    assert isinstance(reader.understand("   "), Failure)
    reader.close()
    assert RuleReader(today=lambda: TODAY).understand("notes from yesterday").after == "2026-10-11"
