"""Conformance checks for FileSource (CONTRACTS.md part 1).

Each check takes a harness `h` with:
  h.source                       the FileSource under test
  h.supports_file_ids            False when the platform gives no file ids (moves then show as add + remove)
  h.new_root() / h.nonexistent_root()
  h.put(root, rel, content=b"x") -> full path     create or rewrite a file (a rewrite changes size or time)
  h.rename(root, old_rel, new_rel)                keeps the file id
  h.delete(root, rel) / h.clear(root)
  h.make_unreachable(root) / h.make_reachable(root)
  h.full_path(root, rel)
  h.snapshot(root)                                everything a read could disturb, comparable with ==
Optional abilities, each switched by a flag so a harness that cannot do them skips those checks:
  h.supports_offline            and h.put(root, rel, content, offline=True)   a cloud placeholder
  h.supports_unreadable_folders and h.make_folder_unreadable(root, rel) / h.make_folder_readable(root, rel)
"""

from __future__ import annotations

import threading

from dugup.protocols import ChangeSet, Listing, ScanRules, WARN_MASS_LOSS, WARN_ROOT_EMPTY
from dugup.shapes import Failure, FailureReason

from .common import CountingStop, require, require_failure, require_result

NO_RULES = ScanRules()


def _full(h, root, rules=NO_RULES, stop=None):
    result = require_result(h.source.scan(root, rules, stop), "scan")
    require(isinstance(result, Listing), "scan: did not return a Listing")
    return result


def _scan(h, root, rules=NO_RULES, stop=None):
    return _full(h, root, rules, stop).entries


def _listing(h, root, rules=NO_RULES):
    return {e.path: e for e in _scan(h, root, rules)}


def _changes(h, root, previous, rules=NO_RULES):
    result = require_result(h.source.changes(root, previous, rules), "changes")
    require(isinstance(result, ChangeSet), "changes: did not return a ChangeSet")
    return result


def check_scan_lists_files(h):
    root = h.new_root()
    expected = {
        h.put(root, "a.txt", b"12345"): 5,
        h.put(root, "sub/b.pdf", b"123"): 3,
        h.put(root, "sub/deeper/c.docx", b"1"): 1,
    }
    entries = _scan(h, root)
    require(len(entries) == len(expected), f"scan: expected {len(expected)} files, got {len(entries)}")
    require(len({e.path for e in entries}) == len(entries), "scan: the same path was listed twice")
    for e in entries:
        require(e.path in expected, "scan: listed a path that was never created")
        require(e.size == expected[e.path], "scan: wrong size")
        require(e.modified > 0, "scan: no modified time")
        require(e.file_id is None or (isinstance(e.file_id, str) and e.file_id), "scan: file_id must be text or None")
    if h.supports_file_ids:
        require(all(e.file_id for e in entries), "scan: this platform gives file ids but none were listed")
        require(len({e.file_id for e in entries}) == len(entries), "scan: two files share one file id")


def check_empty_folder_is_empty_not_failure(h):
    root = h.new_root()
    require(h.source.reachable(root), "reachable: an existing empty folder must be reachable")
    result = h.source.scan(root, NO_RULES)
    require(result == Listing(), "scan: an empty folder must give an empty listing, not a failure or None")


def check_unreachable_is_reported_not_empty(h):
    root = h.new_root()
    h.put(root, "a.txt")
    previous = _listing(h, root)
    h.make_unreachable(root)
    require(not h.source.reachable(root), "reachable: an unreachable folder was reported reachable")
    require_failure(_safe(lambda: h.source.scan(root, NO_RULES)), FailureReason.UNREACHABLE, "scan of unreachable folder")
    require_failure(_safe(lambda: h.source.changes(root, previous, NO_RULES)), FailureReason.UNREACHABLE,
                    "changes of unreachable folder")
    h.make_reachable(root)
    require(h.source.reachable(root), "reachable: the folder came back but is still reported unreachable")
    require(len(_scan(h, root)) == 1, "scan: the listing is wrong after the folder came back")


def check_missing_folder_is_unreachable(h):
    root = h.nonexistent_root()
    require(not h.source.reachable(root), "reachable: a folder that does not exist was reported reachable")
    require_failure(_safe(lambda: h.source.scan(root, NO_RULES)), FailureReason.UNREACHABLE, "scan of missing folder")


def _safe(fn):
    """Turn an exception into a value so the check can say what happened."""
    try:
        return fn()
    except Exception as exc:
        return exc


def check_no_change_gives_empty_changeset(h):
    root = h.new_root()
    h.put(root, "a.txt")
    h.put(root, "b/c.txt")
    previous = _listing(h, root)
    result = _changes(h, root, previous)
    require(result == ChangeSet(), "changes: nothing changed but the answer was not empty")


def check_added_changed_removed(h):
    root = h.new_root()
    h.put(root, "keep.txt", b"1")
    h.put(root, "edit.txt", b"1")
    h.put(root, "gone.txt", b"1")
    previous = _listing(h, root)
    new_path = h.put(root, "new.txt", b"1")
    edit_path = h.put(root, "edit.txt", b"22222")
    h.delete(root, "gone.txt")
    result = _changes(h, root, previous)
    require([e.path for e in result.added] == [new_path], "changes: added is wrong")
    require([e.path for e in result.changed] == [edit_path], "changes: changed is wrong")
    require(list(result.removed) == [h.full_path(root, "gone.txt")], "changes: removed is wrong")
    require(not result.moved and not result.warnings, "changes: reported a move or a warning that did not happen")


def check_move_is_recognised(h):
    root = h.new_root()
    h.put(root, "old/name.txt", b"abc")
    previous = _listing(h, root)
    h.rename(root, "old/name.txt", "new/place.txt")
    result = _changes(h, root, previous)
    old, new = h.full_path(root, "old/name.txt"), h.full_path(root, "new/place.txt")
    if h.supports_file_ids:
        require([(o, e.path) for o, e in result.moved] == [(old, new)], "changes: the move was not recognised")
        require(not result.added and not result.removed, "changes: a moved file was also listed as added or removed")
    else:
        require([e.path for e in result.added] == [new] and list(result.removed) == [old],
                "changes: without file ids a move must show as one addition and one removal")


def check_moved_and_edited_file_is_also_changed(h):
    if not h.supports_file_ids:
        return
    root = h.new_root()
    h.put(root, "a.txt", b"abc")
    previous = _listing(h, root)
    h.rename(root, "a.txt", "b.txt")
    h.put(root, "b.txt", b"abcdefgh")
    result = _changes(h, root, previous)
    new = h.full_path(root, "b.txt")
    require([m[1].path for m in result.moved] == [new], "changes: the move was not recognised")
    require([e.path for e in result.changed] == [new], "changes: a moved file with new content must also be listed as changed")


def check_empty_after_files_warns(h):
    root = h.new_root()
    for i in range(5):
        h.put(root, f"f{i}.txt")
    previous = _listing(h, root)
    h.clear(root)
    result = _changes(h, root, previous)
    require(WARN_ROOT_EMPTY in result.warnings, "changes: a folder that was full and is now empty must carry the root_empty warning")
    require(len(result.removed) == 5, "changes: the removals must still be listed next to the warning")


def check_mass_loss_warns_but_small_loss_does_not(h):
    root = h.new_root()
    for i in range(30):
        h.put(root, f"f{i:02d}.txt")
    previous = _listing(h, root)
    h.delete(root, "f00.txt")
    small = _changes(h, root, previous)
    require(WARN_MASS_LOSS not in small.warnings, "changes: one lost file out of thirty must not raise mass_loss")
    for i in range(1, 20):
        h.delete(root, f"f{i:02d}.txt")
    big = _changes(h, root, previous)
    require(WARN_MASS_LOSS in big.warnings, "changes: losing twenty of thirty files must raise mass_loss")
    require(len(big.removed) == 20, "changes: the removals must still be listed next to the warning")


def check_rules_apply(h):
    root = h.new_root()
    keep = h.put(root, "docs/a.pdf")
    h.put(root, "docs/b.tmp")
    h.put(root, "node_modules/x/c.pdf")
    h.put(root, "notes.txt")
    rules = ScanRules(include=("*.pdf", "*.tmp"), exclude=("*.tmp", "node_modules"))
    require([e.path for e in _scan(h, root, rules)] == [keep], "scan: include and exclude rules were not applied")
    previous = _listing(h, root, rules)
    h.put(root, "docs/new.tmp")
    h.put(root, "node_modules/y/d.pdf")
    require(_changes(h, root, previous, rules) == ChangeSet(), "changes: an excluded file showed up as a change")


def check_never_writes(h):
    root = h.new_root()
    h.put(root, "a.txt")
    h.put(root, "b/c.txt")
    before = h.snapshot(root)   # taken before the source's first read, so even that read is checked
    previous = _listing(h, root)
    _scan(h, root)
    _changes(h, root, previous)
    h.source.reachable(root)
    require(h.snapshot(root) == before, "the source changed the folder it was reading")


def check_unreadable_folder_is_named_and_its_files_are_kept(h):
    if not h.supports_unreadable_folders:
        return
    root = h.new_root()
    open_file = h.put(root, "open/b.txt")
    h.put(root, "locked/a.txt")
    previous = _listing(h, root)
    require(len(previous) == 2, "scan: the setup listing is wrong")
    h.make_folder_unreadable(root, "locked")
    try:
        listing = _full(h, root)
        locked = h.full_path(root, "locked")
        require([e.path for e in listing.entries] == [open_file], "scan: a folder that cannot be read still listed files")
        require([s.path for s in listing.skipped] == [locked], "scan: a folder that cannot be read must be named in skipped")
        require(all(s.reason.strip() for s in listing.skipped), "scan: a skipped folder must say why")
        result = _changes(h, root, previous)
        require(result.removed == (), "changes: files below a folder that could not be read must not be reported as removed")
        require([s.path for s in result.skipped] == [locked], "changes: the skipped folder must be named")
    finally:
        h.make_folder_readable(root, "locked")
    require(_changes(h, root, previous) == ChangeSet(), "changes: once the folder can be read again nothing has changed")


def check_offline_files_are_flagged(h):
    if not h.supports_offline:
        return
    root = h.new_root()
    placeholder = h.put(root, "cloud.docx", offline=True)
    local = h.put(root, "local.docx")
    flags = {e.path: e.offline for e in _scan(h, root)}
    require(flags == {placeholder: True, local: False}, "scan: a cloud placeholder must be listed and flagged offline; a local file must not")


def check_stop_before_start(h):
    root = h.new_root()
    h.put(root, "a.txt")
    stop = threading.Event()
    stop.set()
    require_failure(_safe(lambda: h.source.scan(root, NO_RULES, stop)), FailureReason.STOPPED, "scan with stop already set")
    require_failure(_safe(lambda: h.source.changes(root, {}, NO_RULES, stop)), FailureReason.STOPPED,
                    "changes with stop already set")


def check_stop_between_files(h):
    root = h.new_root()
    for i in range(60):
        h.put(root, f"d{i % 6}/f{i}.txt")
    stop = CountingStop(after=5)
    require_failure(_safe(lambda: h.source.scan(root, NO_RULES, stop)), FailureReason.STOPPED,
                    "scan stopped part way (the source must look at the stop request between files)")
    require(stop.looks >= 5, "scan: the stop request was not looked at while scanning")
    require(len(_scan(h, root)) == 60, "scan: after a stopped scan the next scan must still be complete")


CHECKS = {
    "scan_lists_files": check_scan_lists_files,
    "empty_folder_is_empty_not_failure": check_empty_folder_is_empty_not_failure,
    "unreachable_is_reported_not_empty": check_unreachable_is_reported_not_empty,
    "missing_folder_is_unreachable": check_missing_folder_is_unreachable,
    "no_change_gives_empty_changeset": check_no_change_gives_empty_changeset,
    "added_changed_removed": check_added_changed_removed,
    "move_is_recognised": check_move_is_recognised,
    "moved_and_edited_file_is_also_changed": check_moved_and_edited_file_is_also_changed,
    "empty_after_files_warns": check_empty_after_files_warns,
    "mass_loss_warns_but_small_loss_does_not": check_mass_loss_warns_but_small_loss_does_not,
    "rules_apply": check_rules_apply,
    "never_writes": check_never_writes,
    "unreadable_folder_is_named_and_its_files_are_kept": check_unreadable_folder_is_named_and_its_files_are_kept,
    "offline_files_are_flagged": check_offline_files_are_flagged,
    "stop_before_start": check_stop_before_start,
    "stop_between_files": check_stop_between_files,
}
