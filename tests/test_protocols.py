"""The ten contracts exist as Protocols, the shapes behave, and real and fake parts have the right methods."""

import threading

import pytest

from dugup import protocols as p
from dugup.shapes import Box, FileKind, FileRecord


def test_all_ten_contracts_are_defined():
    for name in ("FileSource", "Converter", "Extractor", "OcrBackend", "Embedder", "Store", "Sandbox", "Annotator",
                 "Service", "IdleSource"):
        assert hasattr(p, name), name


def test_an_event_is_a_stop_signal():
    assert isinstance(threading.Event(), p.StopSignal)


def test_sqlite_store_has_the_store_methods(store):
    assert isinstance(store, p.Store)


def test_the_fakes_have_the_contract_methods():
    from fakes import fake_extractor, fake_filesource, fake_ocr, fake_store
    assert isinstance(fake_store.FakeStore(), p.Store)
    assert isinstance(fake_filesource.Harness().source, p.FileSource)
    assert isinstance(fake_ocr.FakeOcr(), p.OcrBackend)
    assert isinstance(fake_extractor.Harness().extractor, p.Extractor)


def test_a_part_missing_a_method_is_not_that_part():
    class Half:
        def reachable(self, root):
            return True
    assert not isinstance(Half(), p.FileSource)


def test_unreadable_page_must_say_why_and_pages_count_from_one():
    with pytest.raises(ValueError):
        p.PageInfo(1, p.PageVerdict.UNREADABLE, False)
    with pytest.raises(ValueError):
        p.PageInfo(0, p.PageVerdict.BORN_DIGITAL, False)
    assert p.PageInfo(1, p.PageVerdict.UNREADABLE, False, "bad object").unread_reason


def test_pages_needing_ocr_lists_page_numbers():
    ex = p.Extraction((), (p.PageInfo(1, p.PageVerdict.BORN_DIGITAL, False), p.PageInfo(2, p.PageVerdict.GARBLED, True)))
    assert ex.pages_needing_ocr == (2,)


def test_ocr_confidence_must_be_a_fraction_when_given():
    box = Box(0, 0, 10, 10)
    p.OcrLine("x", box)
    p.OcrLine("x", box, 0.0)
    with pytest.raises(ValueError):
        p.OcrLine("x", box, 1.5)


@pytest.mark.parametrize("state,plugged", [(p.PowerState.MAINS, True), (p.PowerState.UNKNOWN, True), (p.PowerState.BATTERY, False)])
def test_unknown_power_counts_as_plugged_in(state, plugged):
    assert p.is_plugged_in(p.PowerReading(state)) is plugged
