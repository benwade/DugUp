"""OcrBackend conformance: the fake must pass every check; each broken fake must be caught."""

import pytest

from conformance.common import expect_fail
from conformance.ocr_checks import CHECKS
from fakes import fake_ocr as fo

BROKEN = [
    (fo.EmptyOnCrash, "crash_is_a_failure_not_silence_or_exception"),
    (fo.RaisesOnCrash, "crash_is_a_failure_not_silence_or_exception"),
    (fo.EmptyForUnsupportedLanguage, "unsupported_language_is_stated"),
    (fo.SwallowsDamagedPictures, "damaged_picture_is_stated"),
    (fo.IgnoresTimeLimit, "time_limit_is_kept"),
    (fo.IgnoresStop, "stop_ends_a_stuck_read"),
    (fo.IgnoresStop, "stop_before_start"),
    (fo.DropsNonAscii, "non_ascii_text_survives"),
    (fo.IgnoresRegion, "region_limits_the_reading_but_keeps_page_coordinates"),
    (fo.PixelBoxes, "boxes_are_whole_page_and_size_independent"),
]


@pytest.fixture
def harness_for():
    made = []

    def make(cls=fo.FakeOcr):
        h = fo.Harness(cls)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_fake_passes(name, harness_for):
    CHECKS[name](harness_for())


@pytest.mark.parametrize("broken,name", BROKEN, ids=[f"{b.__name__}-{n}" for b, n in BROKEN])
def test_broken_fake_is_caught(broken, name, harness_for):
    expect_fail(CHECKS[name], harness_for(broken))
