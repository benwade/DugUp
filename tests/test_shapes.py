import random

import pytest

from dugup.shapes import (
    BOX_SCALE, Box, BoxError, Evidence, FileState, box_from_polygon, to_unit_box,
)


def test_box_scales_to_0_1000_on_both_axes():
    assert to_unit_box(200, 100, 400, 300, width=2000, height=1000) == Box(100, 100, 200, 300)


def test_box_with_swapped_corners_is_put_in_order():
    assert to_unit_box(400, 300, 200, 100, 2000, 1000) == Box(100, 100, 200, 300)


def test_box_sticking_out_of_the_page_is_clamped():
    assert to_unit_box(-50, -50, 5000, 5000, 1000, 1000) == Box(0, 0, 1000, 1000)


def test_box_wholly_outside_the_page_is_an_error_with_a_reason():
    with pytest.raises(BoxError, match="outside"):
        to_unit_box(2000, 10, 2100, 20, 1000, 1000)
    with pytest.raises(BoxError, match="outside"):
        to_unit_box(-30, -30, -10, -10, 1000, 1000)


@pytest.mark.parametrize("width,height", [(0, 100), (100, 0), (-5, 100), (100, -5)])
def test_page_without_size_is_an_error_not_a_division_by_zero(width, height):
    with pytest.raises(BoxError, match="positive"):
        to_unit_box(0, 0, 1, 1, width, height)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "10"])
def test_non_finite_or_non_numeric_input_is_rejected(bad):
    with pytest.raises(BoxError):
        to_unit_box(bad, 0, 10, 10, 100, 100)


def test_any_box_inside_or_overlapping_a_page_lands_inside_0_1000():
    rng = random.Random(7)
    for _ in range(500):
        width, height = rng.uniform(50, 4000), rng.uniform(50, 4000)
        x0, x1 = rng.uniform(-100, width + 100), rng.uniform(-100, width + 100)
        y0, y1 = rng.uniform(-100, height + 100), rng.uniform(-100, height + 100)
        try:
            box = to_unit_box(x0, y0, x1, y1, width, height)
        except BoxError:
            continue  # wholly outside is allowed to be an error
        assert 0 <= box.x0 <= box.x1 <= BOX_SCALE
        assert 0 <= box.y0 <= box.y1 <= BOX_SCALE


def test_rotated_polygon_becomes_its_bounding_box():
    quad = [(100, 50), (300, 80), (290, 130), (90, 100)]
    assert box_from_polygon(quad, 1000, 1000) == Box(90, 50, 300, 130)


def test_empty_or_malformed_polygon_is_an_error():
    with pytest.raises(BoxError, match="no points"):
        box_from_polygon([], 100, 100)
    with pytest.raises(BoxError, match="pairs"):
        box_from_polygon([(1, 2, 3)], 100, 100)


def test_box_type_itself_refuses_out_of_range_or_misordered_values():
    with pytest.raises(BoxError):
        Box(0, 0, 1001, 10)
    with pytest.raises(BoxError):
        Box(10, 0, 5, 10)
    with pytest.raises(BoxError):
        Box(0.5, 0, 10, 10)


def test_evidence_validates_its_span_and_page():
    Evidence(file_id=1, page=0, start=0, end=0)  # page 0 means not page-bound
    with pytest.raises(ValueError):
        Evidence(file_id=1, page=-1, start=0, end=5)
    with pytest.raises(ValueError):
        Evidence(file_id=1, page=1, start=5, end=2)


def test_state_names_are_stable_because_they_are_stored_in_the_index():
    assert {s.value for s in FileState} == {
        "pending", "done", "partial", "skipped", "failed", "missing", "unavailable",
    }
