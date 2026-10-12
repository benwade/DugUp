"""The shared Store checks run against the real SqliteStore."""

import pytest

from conformance.store_checks import CHECKS


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_sqlite_store_meets_the_contract(name, store):
    CHECKS[name](store)
