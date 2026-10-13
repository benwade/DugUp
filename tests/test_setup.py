"""The setup wizard's choices (setup.py): built from the catalogue, started from what suits the computer."""

import dataclasses

import pytest

from dugup import catalogue
from dugup.catalogue import Entry, Group, Machine
from dugup.setup import WARNING, WHY_QUESTIONS, SetupPlan
from dugup.settings import Settings

GB = 1024 ** 3
STRONG = Machine(cores=32, free_memory_bytes=35 * GB, free_disk_bytes=550 * GB, card="graphics card (DirectML)", card_speedup=23.0)
PLAIN = Machine(cores=4, free_memory_bytes=6 * GB, free_disk_bytes=80 * GB)


def test_a_first_run_starts_from_what_suits_the_computer_and_says_why():
    plan = SetupPlan(Settings(), STRONG)
    assert plan.values["ocr_gpu"] is True and "23.0 times faster" in plan.hint("ocr_gpu")
    assert plan.values["max_index_mb"] == 56320 and "a tenth of the 550 GB" in plan.hint("max_index_mb")
    assert plan.changes() == {"ocr_gpu": True, "max_index_mb": 56320}
    plain = SetupPlan(Settings(), PLAIN)
    assert plain.values["ocr_gpu"] is False and "no graphics card" in plain.hint("ocr_gpu")
    assert plain.values["max_index_mb"] == 8192


def test_a_persons_own_choice_is_kept_and_the_suggestion_is_only_mentioned():
    chosen = Settings(max_index_mb=1000)
    plan = SetupPlan(chosen, STRONG)
    assert plan.values["max_index_mb"] == 1000, "a limit the person set before is not replaced"
    assert plan.hint("max_index_mb").startswith("DugUp would suggest 56,320 MB")
    later = SetupPlan(Settings(), STRONG, first_run=False)
    assert later.values["ocr_gpu"] is False and later.changes() == {}, "opening Settings later changes nothing by itself"
    assert "DugUp would suggest yes" in later.hint("ocr_gpu")
    later.use_suggestion("ocr_gpu")
    assert later.values["ocr_gpu"] is True and "Chosen for this computer" in later.hint("ocr_gpu")


def test_values_are_checked_as_they_are_given():
    plan = SetupPlan(Settings(), PLAIN)
    assert "whole number" in plan.set("max_index_mb", "plenty") and plan.values["max_index_mb"] == 8192
    assert plan.set("max_index_mb", "2,000") == "" and plan.values["max_index_mb"] == 2000
    assert plan.hint("max_index_mb").startswith("DugUp would suggest"), "the computer's reason no longer applies"
    assert plan.set("ocr_gpu", "yes") == "" and plan.set("book_folder_level", "words") == ""
    assert plan.changes() == {"max_index_mb": 2000, "ocr_gpu": True, "book_folder_level": "words"}
    advanced = SetupPlan(Settings(), PLAIN, advanced=True)
    assert advanced.set("chunk_chars", "100") != "" and "chunk_overlap" in advanced.set("chunk_chars", "100"), \
        "a value that clashes with another setting is refused with the reason"


def test_the_test_result_arriving_later_updates_untouched_choices_only():
    plan = SetupPlan(Settings(), dataclasses.replace(STRONG, card_speedup=None))
    assert plan.values["ocr_gpu"] is False and "not tried" in plan.hint("ocr_gpu")
    plan.set("max_index_mb", "3000")
    plan.use_machine(STRONG)                       # the speed test has finished
    assert plan.values["ocr_gpu"] is True, "still the computer's choice, so it follows the new fact"
    assert plan.values["max_index_mb"] == 3000, "the person's own number stays"


def test_pages_and_rows_come_from_the_catalogue():
    plan = SetupPlan(Settings(), PLAIN)
    pages = plan.pages()
    everyday = {e.name for e in catalogue.ENTRIES.values() if e.everyday}
    assert {entry.name for group in pages for entry in plan.rows(group.key)} == everyday
    assert all(plan.rows(group.key) for group in pages), "no empty page"
    everything = SetupPlan(Settings(), PLAIN, advanced=True)
    assert {e.name for g in everything.pages() for e in everything.rows(g.key)} == set(catalogue.ENTRIES)
    assert [g.key for g in everything.pages()] == [g.key for g in catalogue.GROUPS]


def test_a_new_setting_in_a_new_group_appears_without_touching_the_wizard(monkeypatch):
    """The point of the catalogue: describing a setting is all it takes."""
    @dataclasses.dataclass(frozen=True)
    class More(Settings):
        answer_model: str = "small"

    entry = Entry("answer_model", "llm", "Model that answers questions", "Which model writes the answer.",
                  why="A larger model answers better and needs more memory.", everyday=True,
                  choices=(("small", "the small one"), ("large", "the large one")),
                  suggest=lambda m: ("large", "there is memory to spare") if (m.free_memory_bytes or 0) > 16 * GB else ("small", "memory is tight"))
    monkeypatch.setattr(catalogue, "ENTRIES", {**catalogue.ENTRIES, "answer_model": entry})
    monkeypatch.setattr(catalogue, "GROUPS", catalogue.GROUPS + (Group("llm", "Asking questions", "A language model can answer from your files. " * 2),))
    monkeypatch.setattr(catalogue, "_KINDS", {**catalogue._KINDS, "answer_model": "str"})
    monkeypatch.setattr(catalogue, "_DEFAULTS", More())
    plan = SetupPlan(More(), STRONG)
    assert plan.pages()[-1].title == "Asking questions" and [e.name for e in plan.rows("llm")] == ["answer_model"]
    assert plan.values["answer_model"] == "large" and "memory to spare" in plan.hint("answer_model")
    assert ("Asking questions", ["Model that answers questions: the large one  (there is memory to spare)"]) in plan.summary()
    assert plan.set("answer_model", "huge") != "" and plan.set("answer_model", "small") == ""


def test_the_summary_says_every_choice_in_words_with_its_reason():
    plan = SetupPlan(Settings(), STRONG)
    plan.set("missing_retention_days", "7")
    lines = [line for _, group in plan.summary() for line in group]
    assert "Read on the graphics card: yes  (the graphics card read a sample page 23.0 times faster than the processor)" in lines
    assert "Keep files that have gone: 7 days  (your choice)" in lines
    plan.set("ocr_gpu", "no")
    assert "Read on the graphics card: no  (your choice; DugUp would suggest yes)" in [l for _, g in plan.summary() for l in g]
    assert "Book libraries: file names only" in lines, "an untouched default is listed plainly"
    assert len(lines) == sum(1 for e in catalogue.ENTRIES.values() if e.everyday)


def test_the_computer_is_described_plainly_whatever_could_be_found():
    assert SetupPlan(Settings(), STRONG).computer_lines() == [
        "Processor cores: 32", "Memory free now: 35 GB", "Disk space free where the index is kept: 550 GB",
        "Graphics card for reading pictures: graphics card (DirectML), 23.0 times the processor's speed on a sample page"]
    unknown = SetupPlan(Settings(), Machine()).computer_lines()
    assert "not known" in unknown[1] and "none that the reader can use" in unknown[3]
    assert "temporary" in WARNING and "choose for you" in WHY_QUESTIONS
