"""DirectoryScan against the shared FileSource checks, on real temporary folders."""

import pytest

from conformance.filesource_checks import CHECKS
from dirscan_harness import DirScanHarness, NoIndexHarness


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_directory_scan_meets_the_contract(name, tmp_path):
    CHECKS[name](DirScanHarness(tmp_path))


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_directory_scan_without_file_ids_still_meets_the_contract(name, tmp_path):
    h = DirScanHarness(tmp_path, collect_ids=False)
    h.supports_file_ids = False   # moves then show as one removal plus one addition
    CHECKS[name](h)


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_directory_scan_meets_the_contract_when_listings_carry_no_file_index(name, tmp_path):
    CHECKS[name](NoIndexHarness(tmp_path))
