import pytest

from dugup.shapes import Failure, FailureReason, failed


def test_a_failure_carries_a_reason_and_a_detail():
    f = Failure(FailureReason.TIMED_OUT, "no answer in 60 seconds")
    assert f.reason is FailureReason.TIMED_OUT and f.detail == "no answer in 60 seconds"
    assert failed(f)


@pytest.mark.parametrize("detail", ["", "   ", "\n\t"])
def test_a_failure_must_say_why(detail):
    with pytest.raises(ValueError):
        Failure(FailureReason.OTHER, detail)


def test_the_reason_must_be_a_failure_reason_not_a_string():
    with pytest.raises(TypeError):
        Failure("timed_out", "text")  # type: ignore[arg-type]


def test_failed_is_false_for_results_including_empty_ones():
    for value in (None, (), [], 0, "", object()):
        assert not failed(value)


def test_failures_are_immutable_and_comparable():
    a = Failure(FailureReason.CORRUPT, "bad")
    assert a == Failure(FailureReason.CORRUPT, "bad")
    with pytest.raises(Exception):
        a.detail = "other"  # type: ignore[misc]


def test_every_reason_has_a_distinct_lower_case_value():
    values = [r.value for r in FailureReason]
    assert len(values) == len(set(values))
    assert all(v == v.lower() and " " not in v for v in values)
    for needed in ("not_installed", "timed_out", "password_protected", "corrupt", "crashed", "stopped", "unreachable",
                   "out_of_memory"):
        assert needed in values
