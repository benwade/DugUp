"""Settings: defaults, the config file, and how mistakes in it are reported."""

import pytest

import helpers
from dugup.settings import Settings, from_mapping, load
from dugup.shapes import FileKind


def test_a_missing_config_file_means_the_defaults(tmp_path):
    settings, notes = load(tmp_path / "config.toml")
    assert settings == Settings() and notes == []


def test_values_are_read_and_non_ascii_survives(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(f'# {helpers.CAFE}\nchunk_chars = 900\nchunk_overlap = 90\nmissing_retention_days = 7\n\n[extensions]\nnfo = "text"\nLOG = "other"\n'.encode("utf-8"))
    settings, notes = load(config)
    assert notes == [] and (settings.chunk_chars, settings.chunk_overlap, settings.missing_retention_days) == (900, 90, 7)
    assert settings.extensions["nfo"] is FileKind.TEXT and settings.extensions["log"] is FileKind.OTHER
    assert settings.extensions["pdf"] is FileKind.PDF, "the defaults stay unless they are changed"


def test_a_config_file_with_a_byte_order_mark_is_read(tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(b"\xef\xbb\xbfchunk_chars = 700\n")
    assert load(config)[0].chunk_chars == 700


def test_unknown_keys_and_wrong_types_are_reported_not_swallowed():
    settings, notes = from_mapping({"chunk_chars": "big", "no_such_key": 1, "max_pdf_pages": True, "extensions": {"x": "nonsense"}})
    assert settings.chunk_chars == Settings().chunk_chars and settings.max_pdf_pages == Settings().max_pdf_pages
    assert len(notes) == 4 and any("no_such_key" in n for n in notes) and any("nonsense" in n for n in notes)


def test_values_that_cannot_work_fall_back_to_the_defaults_with_a_note():
    settings, notes = from_mapping({"chunk_chars": 100, "chunk_overlap": 100})
    assert settings.chunk_overlap < settings.chunk_chars and (settings.chunk_chars, settings.chunk_overlap) != (1800, 200), \
        "one of the two falls back, not both, and the result is a valid pair"
    assert any("chunk" in n and "default" in n for n in notes)


def test_a_broken_config_file_is_the_defaults_with_a_note(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("this is = = not toml", encoding="utf-8")
    settings, notes = load(config)
    assert settings == Settings() and len(notes) == 1


@pytest.mark.parametrize("bad", [dict(chunk_chars=10), dict(chunk_overlap=-1), dict(max_pdf_pages=0),
                                 dict(garbled_fraction=0.0), dict(full_page_image_fraction=1.5), dict(missing_retention_days=-1)])
def test_impossible_settings_are_refused(bad):
    with pytest.raises(ValueError):
        Settings(**bad)


def test_the_fingerprint_changes_with_what_is_indexed_and_only_then():
    base = Settings().fingerprint()
    assert Settings(chunk_chars=900).fingerprint() != base
    assert Settings(batch_files=7, missing_retention_days=1).fingerprint() == base


def test_one_bad_value_does_not_throw_the_good_ones_away():
    """A typo in a timeout must not empty cleanup_protect: the protections a person wrote stay in force."""
    from dugup.settings import from_mapping
    settings, notes = from_mapping({"cleanup_protect": "G:\\Other computers\\My Laptop", "ocr_seconds": -5,
                                    "near_words": 9})
    assert settings.cleanup_protect == "G:\\Other computers\\My Laptop" and settings.near_words == 9
    assert settings.ocr_seconds == 120.0 and any("ocr_seconds" in n and "default" in n for n in notes)
    # two values that fail only together: one is set aside, the other kept
    settings, notes = from_mapping({"chunk_chars": 100, "chunk_overlap": 150, "cleanup_protect": "X:\\keep"})
    assert settings.cleanup_protect == "X:\\keep" and settings.chunk_overlap < settings.chunk_chars
    assert notes

