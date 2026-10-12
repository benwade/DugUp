"""The shared Extractor checks, run on real files read by the real extractors."""

import pytest

pytest.importorskip("pypdfium2", reason="the PDF reader pypdfium2 is not installed (pip install -r requirements.txt)")

from conformance.extractor_checks import CHECKS
from extractor_harness import RealHarness


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_real_extractors_meet_the_contract(name, tmp_path):
    CHECKS[name](RealHarness(tmp_path))
