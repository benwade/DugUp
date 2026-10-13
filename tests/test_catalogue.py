"""Every setting described once (catalogue.py), and `dugup config` built on it."""

import io
from dataclasses import fields

import pytest

import helpers
from dugup import catalogue, cli, settings as settings_module
from dugup.catalogue import ENTRIES, GROUPS, Machine
from dugup.settings import Settings
from dugup.shapes import Failure

GB = 1024 ** 3


# ---- the catalogue cannot fall behind the program ---------------------------------------------------------------

def test_every_setting_has_an_entry_and_every_entry_is_a_setting():
    real = {f.name for f in fields(Settings)} - catalogue.NOT_HERE
    described = set(ENTRIES)
    assert real - described == set(), "a setting was added without describing it in catalogue.py"
    assert described - real == set(), "the catalogue describes a setting that no longer exists"


def test_every_entry_is_complete_and_sits_in_a_group_that_exists():
    groups = {g.key for g in GROUPS}
    assert len(groups) == len(GROUPS)
    for entry in ENTRIES.values():
        assert entry.group in groups, entry.name
        assert entry.label and entry.help and not entry.label.endswith("."), entry.name
        assert not entry.everyday or entry.why, f"{entry.name}: an everyday setting says why a person is asked"
        if entry.choices:
            assert catalogue.default(entry.name) in [value for value, _ in entry.choices], \
                f"{entry.name}: the default must be one of the choices"
    for one in GROUPS:
        assert one.title and len(one.about) > 40 and catalogue.in_group(one.key), f"{one.key}: explained, and not empty"
    everyday = [e.name for e in ENTRIES.values() if e.everyday]
    assert 5 <= len(everyday) <= 12, "the wizard asks about a handful of things, not fifty"


def test_the_default_of_every_setting_passes_its_own_check():
    for name in ENTRIES:
        assert catalogue.parse(name, catalogue.default(name)) == catalogue.default(name), name
        assert catalogue.show(name, catalogue.default(name)) != "", name


def test_limits_in_the_catalogue_never_let_through_what_the_settings_refuse():
    """A value the catalogue accepts must be one Settings accepts, at both ends of its range."""
    for entry in ENTRIES.values():
        for edge in (entry.low, entry.high):
            if edge is None:
                continue
            value = catalogue.parse(entry.name, int(edge) if catalogue.kind(entry.name) == "int" else float(edge))
            assert not isinstance(value, Failure), (entry.name, edge, value)
            changed = catalogue.apply(Settings(), {entry.name: value})
            if isinstance(changed, Failure):
                assert entry.name in ("chunk_overlap", "chunk_chars"), \
                    f"{entry.name} = {edge}: allowed by the catalogue, refused by the settings ({changed.detail})"


# ---- what a person types ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,typed,expected", [
    ("ocr_gpu", "yes", True), ("ocr_gpu", "OFF", False), ("ocr_gpu", " true ", True),
    ("max_index_mb", "4,000", 4000), ("max_index_mb", "0", 0), ("near_words", "20", 20),
    ("picture_heavy_fraction", "0.5", 0.5), ("missing_retention_days", "7", 7.0),
    ("book_folder_level", "WORDS", "words"), ("book_folder_level", "", ""), ("text_compression", "none", "none"),
    ("intent_url", " http://127.0.0.1:1234 ", "http://127.0.0.1:1234"), ("intent_server", "C:\\Tools\\llama\\server.exe", "C:\\Tools\\llama\\server.exe"),
])
def test_what_is_typed_becomes_the_right_kind_of_value(name, typed, expected):
    value = catalogue.parse(name, typed)
    assert value == expected and type(value) is type(expected)


@pytest.mark.parametrize("name,typed,says", [
    ("ocr_gpu", "maybe", "yes or no"), ("max_index_mb", "lots", "whole number"), ("max_index_mb", "-1", "at least 0"),
    ("max_index_mb", "2.5", "whole number"), ("picture_heavy_fraction", "2", "at most 1"), ("near_words", "0", "at least 1"),
    ("book_folder_level", "everything", "one of: names, words, ocr, meaning, ''"), ("text_compression", "zip", "one of: zlib, none"),
    ("no_such_thing", "1", "no setting called"), ("ocr_dpi", "10", "at least 50 dpi"),
])
def test_what_will_not_do_is_refused_with_the_reason(name, typed, says):
    value = catalogue.parse(name, typed)
    assert isinstance(value, Failure) and says in value.detail, value


def test_values_that_depend_on_each_other_are_checked_together():
    answer = catalogue.apply(Settings(), {"chunk_chars": "100", "chunk_overlap": "500"})
    assert isinstance(answer, Failure) and "chunk_overlap" in answer.detail
    fine = catalogue.apply(Settings(), {"chunk_chars": "1000", "chunk_overlap": "100", "ocr_gpu": "yes"})
    assert (fine.chunk_chars, fine.chunk_overlap, fine.ocr_gpu) == (1000, 100, True)


def test_values_are_shown_in_plain_words():
    assert catalogue.show("ocr_gpu", True) == "yes" and catalogue.show("max_index_mb", 56337) == "56,337 MB"
    assert catalogue.show("book_folder_level", "names") == "file names only" and catalogue.show("intent_url", "") == "(empty)"
    assert catalogue.show("picture_heavy_fraction", 0.8) == "0.8" and catalogue.show("ocr_fast_readers", 0) == "worked out by DugUp" and catalogue.show("max_index_mb", 0) == "no limit"


# ---- defaults chosen from the machine ---------------------------------------------------------------------------

def test_the_card_is_suggested_only_when_a_test_showed_it_clearly_faster():
    pick = lambda **facts: catalogue.suggestions(Machine(cores=8, **facts))["ocr_gpu"]
    value, why = pick(card="graphics card (DirectML)", card_speedup=23.0)
    assert value is True and "23.0 times faster" in why
    value, why = pick(card="graphics card (DirectML)", card_speedup=1.1)
    assert value is False and "not clearly faster" in why and "1.1" in why
    value, why = pick(card="graphics card (DirectML)", card_speedup=0.4)
    assert value is False, "a card slower than the processor is never chosen"
    value, why = pick(card="graphics card (DirectML)")
    assert value is False and "not tried" in why
    value, why = pick()
    assert value is False and "no graphics card" in why


def test_the_spending_limit_and_reader_count_come_from_the_machine_with_their_reasons():
    found = catalogue.suggestions(Machine(cores=32, free_memory_bytes=35 * GB, free_disk_bytes=550 * GB))
    assert found["max_index_mb"] == (56320, "a tenth of the 550 GB that is free now")
    value, why = found["ocr_fast_readers"]
    assert value == 0 and "8 on this computer, which has 32 processor cores" in why
    small = catalogue.suggestions(Machine(cores=1, free_disk_bytes=2 * GB))
    assert small["max_index_mb"][0] == 500, "never a limit so small that nothing fits"
    unknown = catalogue.suggestions(Machine())
    assert unknown["max_index_mb"][0] == 0 and "could not be read" in unknown["max_index_mb"][1]
    assert set(found) == {e.name for e in ENTRIES.values() if e.suggest}, "every rule answered, and with a value its setting accepts"


# ---- config.toml ------------------------------------------------------------------------------------------------

def test_values_are_written_so_that_they_read_back_exactly(tmp_path):
    config = tmp_path / "data" / "config.toml"
    values = {"ocr_gpu": True, "max_index_mb": 4000, "picture_heavy_fraction": 0.5, "missing_retention_days": 7.0,
              "intent_server": "C:\\Tools\\it's here\\llama-server.exe", "intent_model_file": f"D:\\{helpers.CAFE} {helpers.CJK}\\m.gguf",
              "book_folders": 'a; "b"; c', "book_folder_level": ""}
    catalogue.write(config, values)
    loaded, notes = settings_module.load(config)
    assert notes == []
    for name, value in values.items():
        assert getattr(loaded, name) == value, name
    assert catalogue.changed(loaded) == values


def test_writing_leaves_the_rest_of_the_file_alone_and_never_writes_a_key_twice(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("# my notes\nnear_words = 9\nocr_gpu = false\nocr_gpu = false\n\n[extensions]\nlog = \"other\"\nocr_gpu = \"other\"\n",
                      encoding="utf-8")
    catalogue.write(config, {"ocr_gpu": True, "max_index_mb": 100})
    text = config.read_text(encoding="utf-8")
    assert text.count("\nocr_gpu = true") == 1 and "ocr_gpu = false" not in text, "the doubled line is mended, not added to"
    assert text.index("max_index_mb = 100") < text.index("[extensions]"), "new keys go above the first table"
    assert "# my notes" in text and 'ocr_gpu = "other"' in text and "near_words = 9" in text
    loaded, notes = settings_module.load(config)
    assert loaded.ocr_gpu is True and loaded.max_index_mb == 100 and loaded.near_words == 9 and notes == []
    catalogue.write(config, {"near_words": None})
    assert "near_words" not in config.read_text(encoding="utf-8") and settings_module.load(config)[0].near_words == 15
    catalogue.write(tmp_path / "new" / "config.toml", {"ocr_gpu": None})
    assert (tmp_path / "new" / "config.toml").read_text(encoding="utf-8") == ""


# ---- the command ------------------------------------------------------------------------------------------------

@pytest.fixture
def run(tmp_path):
    def go(*argv):
        out = io.StringIO()
        code = cli.main([*argv, "--data-dir", str(tmp_path / "data")], stdout=out, environ={})
        return code, out.getvalue()
    go.config = tmp_path / "data" / "config.toml"
    return go


def test_list_shows_the_everyday_settings_grouped_and_explained(run):
    code, text = run("config", "list")
    assert code == 0 and "not there yet: every setting is at its default" in text
    assert "Reading pictures and scans" in text and "ocr_gpu = no" in text and "Read on the graphics card." in text
    assert "chunk_chars" not in text and "more: dugup config list --all" in text and "dugup config set NAME VALUE" in text
    code, text = run("config", "list", "--all")
    assert all(name + " = " in text for name in ENTRIES), "every setting is listed"
    assert all(group.title in text for group in GROUPS)


def test_set_get_and_reset(run):
    code, text = run("config", "set", "ocr_gpu", "yes")
    assert code == 0 and "ocr_gpu = yes" in text and str(run.config) in text
    assert run.config.read_text(encoding="utf-8") == "ocr_gpu = true\n"
    run("config", "set", "ocr_gpu", "yes")
    assert run.config.read_text(encoding="utf-8") == "ocr_gpu = true\n", "setting it again does not add a second line"
    code, text = run("config", "get", "ocr_gpu")
    assert code == 0 and "ocr_gpu = yes   (the default is no)" in text and "Why it matters:" in text and "Choices: yes (yes); no (no)" in text
    assert "[changed]" in run("config", "list")[1]
    code, text = run("config", "set", "book_folders", "novels;", "comics")
    assert code == 0 and settings_module.load(run.config)[0].book_folders == "novels; comics"
    code, text = run("config", "reset", "ocr_gpu")
    assert code == 0 and "back at its default: no" in text and "ocr_gpu" not in run.config.read_text(encoding="utf-8")


def test_a_value_that_will_not_do_is_refused_and_nothing_is_saved(run):
    code, text = run("config", "set", "max_index_mb", "plenty")
    assert code == 1 and "whole number" in text and not run.config.exists()
    code, text = run("config", "set", "chunk_overlap", "5000")
    assert code == 1 and "chunk_overlap" in text and not run.config.exists()
    code, text = run("config", "set", "nonsense", "1")
    assert code == 1 and "no setting called" in text and "dugup config list --all" in text
    code, text = run("config", "set", "chunk_chars", "1000")
    assert code == 0 and "read every file again" in text, "a setting that shapes the index says what follows"
