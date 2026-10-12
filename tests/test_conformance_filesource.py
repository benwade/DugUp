"""FileSource conformance: the fake must pass every check; each broken fake must be caught."""

import pytest

from conformance.common import expect_fail
from conformance.filesource_checks import CHECKS
from fakes import fake_filesource as ff

BROKEN = [
    (ff.EmptyWhenUnreachable, "unreachable_is_reported_not_empty"),
    (ff.EmptyWhenUnreachable, "missing_folder_is_unreachable"),
    (ff.RemovedWhenUnreachable, "unreachable_is_reported_not_empty"),
    (ff.RaisesWhenUnreachable, "unreachable_is_reported_not_empty"),
    (ff.DropsWarnings, "empty_after_files_warns"),
    (ff.DropsWarnings, "mass_loss_warns_but_small_loss_does_not"),
    (ff.IgnoresFileIds, "move_is_recognised"),
    (ff.WritesToTree, "never_writes"),
    (ff.IgnoresStop, "stop_before_start"),
    (ff.IgnoresStop, "stop_between_files"),
    (ff.StopsOnlyAtStart, "stop_between_files"),
    (ff.IgnoresRules, "rules_apply"),
    (ff.SilentlySkipsFolders, "unreadable_folder_is_named_and_its_files_are_kept"),
    (ff.RemovesFilesBelowSkippedFolders, "unreadable_folder_is_named_and_its_files_are_kept"),
    (ff.DropsOfflineFlag, "offline_files_are_flagged"),
]


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_fake_passes(name):
    CHECKS[name](ff.Harness())


@pytest.mark.parametrize("broken,name", BROKEN, ids=[f"{b.__name__}-{n}" for b, n in BROKEN])
def test_broken_fake_is_caught(broken, name):
    expect_fail(CHECKS[name], ff.Harness(broken))
