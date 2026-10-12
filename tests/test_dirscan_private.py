"""Timing and counts for DirectoryScan on real folders. Prints numbers only: no names, no paths.

Not part of the normal run. Choose the folders yourself:

    PowerShell:  $env:DUGUP_PRIVATE_ROOTS = "D:\\some\\folder;G:\\other\\folder"
                 python -m pytest -m private -s tests\\test_dirscan_private.py

Folders are separated by semicolons. Set DUGUP_PRIVATE_EXCLUDES=suggested to also prune the suggested
folder names (.git, node_modules and so on) and see how much that saves. Nothing is written to any folder.
"""

import collections
import os
import time

import pytest

from dugup.dirscan import DirectoryScan
from dugup.filesource import SUGGESTED_EXCLUDES
from dugup.protocols import ChangeSet, Listing, ScanRules

pytestmark = pytest.mark.private


def _roots():
    raw = os.environ.get("DUGUP_PRIVATE_ROOTS", "")
    return [part.strip() for part in raw.split(";") if part.strip()]


def _rules():
    if os.environ.get("DUGUP_PRIVATE_EXCLUDES", "").strip().lower() == "suggested":
        return ScanRules(exclude=SUGGESTED_EXCLUDES)
    return ScanRules()


def _rate(count, seconds):
    return count / seconds if seconds > 0 else 0.0


def _timed(root, rules, collect_ids):
    scanner = DirectoryScan(collect_ids=collect_ids)
    began = time.perf_counter()
    result = scanner.scan(root, rules)
    seconds = time.perf_counter() - began
    return result, seconds, dict(scanner.last_stats)


def _timed_changes(root, rules, listing):
    """changes() against the earlier full listing: ids are looked up only for new or different paths."""
    scanner = DirectoryScan()
    previous = {x.path: x for x in listing.entries}
    began = time.perf_counter()
    result = scanner.changes(root, previous, rules)
    return result, time.perf_counter() - began, dict(scanner.last_stats)


def _skip_kinds(listing):
    kinds = collections.Counter(item.reason.split(":", 1)[0] for item in listing.skipped)
    return ", ".join(f"{name} x{count}" for name, count in sorted(kinds.items())) or "none"


def test_scan_time_with_and_without_file_ids():
    roots = _roots()
    if not roots:
        pytest.skip("set DUGUP_PRIVATE_ROOTS to one or more folders, separated by semicolons")
    rules = _rules()
    print()
    unopened = []
    for number, root in enumerate(roots, start=1):
        label = f"root {number}"
        with_ids_1, t_ids_1, stats = _timed(root, rules, True)
        if not isinstance(with_ids_1, Listing):
            print(f"{label}: could not be opened ({with_ids_1.reason.value}); check the path")
            unopened.append(label)
            continue
        _, t_ids_2, _ = _timed(root, rules, True)
        no_ids_1, t_plain_1, _ = _timed(root, rules, False)
        _, t_plain_2, _ = _timed(root, rules, False)

        files = len(with_ids_1.entries)
        # The same folder must give the same files whether or not ids are collected (unless it changed meanwhile).
        same_paths = {x.path for x in with_ids_1.entries} == {x.path for x in no_ids_1.entries}
        with_id = sum(1 for x in with_ids_1.entries if x.file_id)
        offline = sum(1 for x in with_ids_1.entries if x.offline)
        first_ids, first_plain = t_ids_1, t_plain_1
        again_ids, again_plain = t_ids_2, t_plain_2

        print(f"{label}: {files} files, {stats.get('folders', 0)} folders, {stats.get('links_skipped', 0)} links not followed, "
              f"{len(with_ids_1.skipped)} unreadable ({_skip_kinds(with_ids_1)}), {offline} offline placeholders")
        print(f"{label}: ids found for {with_id} of {files} files "
              f"({(with_id / files * 100) if files > 0 else 0.0:.1f} percent)")
        print(f"{label}: with ids    first {first_ids:.2f}s ({_rate(files, first_ids):.0f} files/s), repeat {again_ids:.2f}s")
        print(f"{label}: without ids first {first_plain:.2f}s ({_rate(files, first_plain):.0f} files/s), repeat {again_plain:.2f}s")
        extra = ((again_ids - again_plain) / files * 1000) if files > 0 else 0.0
        print(f"{label}: id cost on repeat scans about {extra:.3f} ms per file")
        if not same_paths:
            print(f"{label}: note: the two listings differed, so the folder changed while scanning")
        changes, t_changes, change_stats = _timed_changes(root, rules, with_ids_1)
        if isinstance(changes, ChangeSet):
            print(f"{label}: changes() on a repeat: {t_changes:.2f}s, {change_stats.get('id_lookups', 0)} id lookups, "
                  f"{len(changes.added)} added, {len(changes.changed)} changed, {len(changes.removed)} removed, "
                  f"{len(changes.moved)} moved")
        else:
            print(f"{label}: changes() did not finish ({changes.reason.value})")
    assert not unopened, f"could not open: {', '.join(unopened)}"
