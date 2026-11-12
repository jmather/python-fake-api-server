"""The shared conformance suite, run against a live server process."""

import pytest

from fake_api_server.conformance_runner import _compare_json, main


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        (True, 1),
        (True, 1.0),
        (False, 0),
        (False, 0.0),
        (1, True),
        (1.0, True),
        (0, False),
        (0.0, False),
    ],
)
def test_conformance_json_rejects_boolean_number_confusion(expected, actual):
    assert _compare_json(expected, actual, {}, "$.value")


def test_conformance_json_rejects_nested_boolean_number_confusion():
    expected = {"records": [{"success": True}]}
    actual = {"records": [{"success": 1}]}
    assert _compare_json(expected, actual, {}, "$.value")


@pytest.mark.parametrize(
    ("expected", "actual"),
    [(True, True), (False, False), (1, 1.0), (0, 0.0)],
)
def test_conformance_json_preserves_valid_matches(expected, actual):
    assert _compare_json(expected, actual, {}, "$.value") == []


def test_conformance_suite():
    assert main() == 0
