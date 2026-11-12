"""Scoring rules: method, path, headers, query, and optional body."""

import pytest

from fake_api_server.matching import (
    Endpoint,
    EndpointResponse,
    MultipleMatchError,
    find_best,
    score_endpoint,
)


def endpoint(path="/items", method="post", body=None, headers=None, query=None, content="ok"):
    return Endpoint(
        method=method,
        path=path,
        body=body,
        headers=headers or {},
        query=query or {},
        response_mode="incremental",
        responses=[
            EndpointResponse(
                status=200,
                content=content,
                content_type="text/plain",
                headers={},
                delay=0,
            )
        ],
    )


def test_body_is_optional_and_scores_when_present():
    plain = endpoint(content="plain")
    exact = endpoint(body="Foo", content="exact")
    assert score_endpoint(plain, headers={}, query={}, body="Bar") == 0
    assert score_endpoint(exact, headers={}, query={}, body="Foo") == 1
    assert score_endpoint(exact, headers={}, query={}, body="Bar") is None

    winner = find_best(
        [plain, exact],
        method="POST",
        path="/items",
        headers={},
        query={},
        body="Foo",
    )
    assert winner is exact


def test_tie_raises():
    foo = endpoint(query={"who": "foo"}, content="foo")
    bar = endpoint(headers={"x-whom": "bar"}, content="bar")
    with pytest.raises(MultipleMatchError) as caught:
        find_best(
            [foo, bar],
            method="post",
            path="/items",
            headers={"x-whom": "bar"},
            query={"who": "foo"},
            body="",
        )
    assert caught.value.best_score == 1
    assert len(caught.value.matches) == 2


def test_path_and_method_must_be_exact():
    short = endpoint(path="/hello", content="short")
    longer = endpoint(path="/hello-world", content="long")
    other_method = endpoint(path="/hello", method="get", content="get")
    found = find_best(
        [short, longer, other_method],
        method="post",
        path="/hello-world",
        headers={},
        query={},
        body="",
    )
    assert found is longer
    assert (
        find_best(
            [short],
            method="post",
            path="/hello-world",
            headers={},
            query={},
            body="",
        )
        is None
    )


def test_incremental_wraps_and_random_can_move():
    item = endpoint()
    item.responses.append(
        EndpointResponse(status=201, content="two", content_type="text/plain", headers={}, delay=0)
    )
    assert item.next_response(lambda _n: 0).content == "ok"
    assert item.next_response(lambda _n: 0).content == "two"
    assert item.next_response(lambda _n: 0).content == "ok"

    item.response_mode = "random"
    item.index = 0
    assert item.next_response(lambda _n: 1).content == "two"
