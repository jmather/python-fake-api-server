"""The shared conformance suite, run against a live server process."""

from fake_api_server.conformance_runner import main


def test_conformance_suite():
    assert main() == 0
