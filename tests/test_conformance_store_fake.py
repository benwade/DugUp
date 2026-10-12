"""Store conformance on the in-memory fake: it must pass every check; each broken fake must be caught."""

import pytest

from conformance.common import expect_fail
from conformance.store_checks import CHECKS
from fakes import fake_store as fs

BROKEN = [
    (fs.SortsAfterTruncating, "sorting_happens_before_truncation"),
    (fs.PurgesWithoutBoundary, "purge_is_boundary_aware_and_complete"),
    (fs.PurgesWithoutBoundary, "purge_handles_windows_separators"),
    (fs.PurgeIgnoresCaseFlag, "purge_case_rule_is_explicit"),
    (fs.DishonestTotal, "total_and_truncation_are_honest"),
    (fs.NamesNotSearchable, "names_are_searchable_without_text"),
    (fs.CaseSensitive, "case_and_accents_do_not_matter"),
    (fs.AccentSensitive, "case_and_accents_do_not_matter"),
    (fs.AnyWordMatches, "every_word_must_match"),
    (fs.OneRowPerSegment, "one_row_per_file_with_a_match_count"),
    (fs.NoRollback, "transactions_are_all_or_nothing"),
    (fs.InnerFailureUndoesOuterWork, "nested_transactions_roll_back_only_the_inner_part"),
    (fs.UpsertDuplicates, "file_round_trip_and_update_in_place"),
    (fs.SilentUnknownIds, "unknown_row_ids_are_errors_with_a_reason"),
    (fs.AcceptsAnySortKey, "unknown_sort_keys_are_refused"),
    (fs.FingerprintHidesMissingKeys, "fingerprint_round_trips_and_reports_changes"),
    (fs.ReplaceAppends, "replacing_segments_replaces_and_is_repeatable"),
    (fs.AllowsNameSegments, "name_segments_cannot_be_written_by_callers"),
    (fs.ReplaceWritesBeforeValidating, "name_segments_cannot_be_written_by_callers"),
    (fs.NoEvidence, "search_rows_carry_evidence"),
    (fs.EvidenceAtTheStart, "evidence_points_at_the_words"),
    (fs.EvidenceOnEveryMatchingRow, "evidence_prefers_text_to_the_name"),
    (fs.PassageSurvivesReplace, "old_evidence_is_not_shown_for_new_text"),
    (fs.PurgesWithoutBoundary, "list_files_filters_and_walks"),
    (fs.PurgesWithoutBoundary, "state_counts_by_folder"),
    (fs.MoveLosesText, "move_keeps_text_and_state"),
    (fs.RemoveLeavesText, "remove_files_takes_them_out_completely"),
    (fs.MoveKeepsOldName, "move_keeps_text_and_state"),
    (fs.MoveOverwrites, "move_refuses_unknown_and_taken_paths"),
    (fs.MissingCountedUnderOldState, "missing_files_are_kept_and_flagged"),
    (fs.MissingLosesText, "missing_files_are_kept_and_flagged"),
    (fs.PurgeMissingIgnoresAge, "purge_missing_respects_age_and_folder"),
    (fs.PurgesWithoutBoundary, "purge_missing_respects_age_and_folder"),
    (fs.PagesAppend, "pages_round_trip_and_replace"),
    (fs.OcrTextNotSearchable, "ocr_text_is_searchable_with_a_box"),
    (fs.OcrSaveAppends, "ocr_text_is_searchable_with_a_box"),
    (fs.OcrWithoutBoxes, "ocr_text_is_searchable_with_a_box"),
    (fs.OcrFailureStaysQueued, "ocr_blank_and_failed_pages_leave_the_queue"),
    (fs.OcrQueueIncludesMissing, "ocr_queue_lists_waiting_pages"),
    (fs.RereadKeepsOcrState, "reread_clears_ocr"),
    (fs.RereadForgetsOcr, "replace_text_keeps_what_ocr_read"),
    (fs.RereadKeepsOcrOfPagesThatNoLongerNeedIt, "replace_text_keeps_what_ocr_read"),
    (fs.DeferredPagesLeaveTheQueue, "a_deferred_page_stays_queued_and_is_marked"),
    (fs.RequeueIgnoresTheEngine, "pages_read_by_another_engine_can_be_requeued"),
    (fs.IgnoresSecondSortKey, "sorting_by_several_keys"),
    (fs.AcceptsAnySortKey, "sorting_by_several_keys"),
    (fs.FiltersAfterTruncating, "filters_narrow_the_search"),
    (fs.PurgesWithoutBoundary, "filters_narrow_the_search"),
    (fs.SizeLimitsAreExclusive, "filters_narrow_the_search"),
    (fs.WhereIsIgnored, "where_the_words_must_be"),
    (fs.FileEvidenceStopsAtThree, "every_place_in_one_file_can_be_listed"),
    (fs.FileEvidenceOutOfOrder, "every_place_in_one_file_can_be_listed"),
    (fs.SetRootForgetsStatus, "roots_round_trip_and_keep_their_status"),
]


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_fake_store_passes(name):
    CHECKS[name](fs.FakeStore())


@pytest.mark.parametrize("broken,name", BROKEN, ids=[f"{b.__name__}-{n}" for b, n in BROKEN])
def test_broken_fake_store_is_caught(broken, name):
    expect_fail(CHECKS[name], broken())
