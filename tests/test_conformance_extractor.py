"""Extractor conformance: the fake must pass every check; each broken fake must be caught."""

import pytest

from conformance.common import expect_fail
from conformance.extractor_checks import CHECKS
from fakes import fake_extractor as fe

BROKEN = [
    (fe.DropsGarbledPages, "garbled_layer_is_not_indexed"),
    (fe.DropsGarbledPages, "mixed_document_accounts_for_every_page"),
    (fe.IndexesGarbledText, "garbled_layer_is_not_indexed"),
    (fe.ScannerLayerNotSentToOcr, "scanner_layer_is_indexed_and_read"),
    (fe.BornDigitalSentToOcr, "born_digital_pdf_is_trusted"),
    (fe.BlankPagesSentToOcr, "blank_page_is_not_sent_to_ocr"),
    (fe.DropsUnreadablePages, "unreadable_page_is_stated"),
    (fe.EmptyForPassword, "password_is_stated"),
    (fe.EmptyForCorrupt, "corrupt_is_stated"),
    (fe.IgnoresStop, "stop_before_start"),
    (fe.IgnoresStop, "stop_between_pages"),
    (fe.StopsOnlyAtStart, "stop_between_pages"),
    (fe.ImageNotSentToOcr, "image_goes_to_ocr"),
    (fe.OfficeTextAsLayerText, "office_native_text"),
]


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_fake_passes(name):
    CHECKS[name](fe.Harness())


@pytest.mark.parametrize("broken,name", BROKEN, ids=[f"{b.__name__}-{n}" for b, n in BROKEN])
def test_broken_fake_is_caught(broken, name):
    expect_fail(CHECKS[name], fe.Harness(broken))
