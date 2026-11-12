"""HTTP contract tests mirrored from the Node and .NET suites."""

import base64
import time

import pytest

from tests.conftest import record, register, register_user
from tests.endpoints import ENDPOINTS


def test_register_returns_credentials(session):
    response = register(session, "demo")
    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {"username", "password", "auth_token"}
    expected = base64.b64encode(
        f"{payload['username']}:{payload['password']}".encode("ascii")
    ).decode("ascii")
    assert payload["auth_token"] == expected
    assert response.headers["location"].endswith("/")


def test_register_action_is_case_insensitive(session):
    response = register(session, "demo", action="ReGiStEr")
    assert response.status_code == 201
    assert "username" in response.json()


def test_register_rejects_additional_properties(session):
    response = session.post(
        "/",
        headers={"X-FakeApi-Action": "register", "Content-Type": "application/json"},
        json={"broken": True},
    )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    body = response.json()
    assert body["message"] == "Request payload is invalid."
    assert body["errors"] == [
        {
            "keyword": "additionalProperties",
            "path": "",
            "message": "must not have additional property 'broken'",
        }
    ]


def test_register_defaults_missing_external_id(session):
    response = session.post(
        "/",
        headers={"X-FakeApi-Action": "register"},
        json={},
    )
    assert response.status_code == 201


def test_record_requires_auth(session):
    response = record(session, {}, ENDPOINTS["hello_world"])
    assert response.status_code == 401
    assert response.text == "Unauthorized"
    assert response.headers["www-authenticate"] == 'Basic realm="FakeAPI"'


def test_record_rejects_bad_password(session):
    _payload, auth = register_user(session)
    auth = {"Authorization": "Basic " + base64.b64encode(b"nope:nope").decode()}
    response = record(session, auth, ENDPOINTS["hello_world"])
    assert response.status_code == 401


def test_record_rejects_bad_path(session):
    _payload, auth = register_user(session)
    response = record(session, auth, ENDPOINTS["bad_hello_world"])
    assert response.status_code == 400
    assert response.json()["errors"] == [
        {
            "keyword": "pattern",
            "path": "/path",
            "message": "must match pattern ^/..*",
        }
    ]


def test_record_echoes_payload_and_applies_defaults(session):
    _payload, auth = register_user(session)
    endpoint = {
        "method": "get",
        "path": "/minimal",
        "responses": [{"status": 200, "content": "hi", "content_type": "text/plain"}],
    }
    response = record(session, auth, endpoint)
    assert response.status_code == 201
    echoed = response.json()
    assert echoed["response_mode"] == "incremental"
    assert echoed["responses"][0]["delay"] == 0
    assert echoed["path"] == "/minimal"


def test_record_duplicate_is_conflict_even_if_the_response_changes(session):
    _payload, auth = register_user(session)
    first = record(session, auth, ENDPOINTS["hello_world"])
    assert first.status_code == 201
    duplicate = dict(ENDPOINTS["hello_world"])
    duplicate["responses"] = [
        {"status": 200, "content": "different", "content_type": "text/plain", "delay": 0}
    ]
    second = record(session, auth, duplicate)
    assert second.status_code == 409
    assert second.text == "Conflict"


def test_record_allows_body_and_treats_it_as_identity(session):
    _payload, auth = register_user(session)
    with_body = dict(ENDPOINTS["post_hello_world_body"])
    without = {
        "method": "post",
        "path": "/hello-world-body",
        "responses": [
            {"status": 200, "content": "any", "content_type": "text/plain", "delay": 0}
        ],
    }
    assert record(session, auth, without).status_code == 201
    assert record(session, auth, with_body).status_code == 201
    again = record(session, auth, with_body)
    assert again.status_code == 409

    foo = session.post("/hello-world-body", headers=auth, content=b"Foo")
    assert foo.status_code == 201
    assert foo.text == "Hello World!"
    other = session.post("/hello-world-body", headers=auth, content=b"Bar")
    assert other.status_code == 200
    assert other.text == "any"


def test_playback_requires_auth_and_misses_unknown_paths(session):
    missing = session.get("/smoke")
    assert missing.status_code == 401
    assert missing.text == "Unauthorized"

    _payload, auth = register_user(session)
    response = session.get("/smoke", headers=auth)
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_unknown_action_is_playback(session):
    _payload, auth = register_user(session)
    response = session.post(
        "/not-an-endpoint",
        headers={**auth, "X-FakeApi-Action": "nope"},
    )
    assert response.status_code == 404


def test_register_only_matches_post_root(session):
    _payload, auth = register_user(session)
    put_register = session.put(
        "/",
        headers={**auth, "X-FakeApi-Action": "register"},
        json={"external_id": "nope"},
    )
    assert put_register.status_code == 404
    post_record = session.post(
        "/",
        headers={**auth, "X-FakeApi-Action": "record"},
        json=ENDPOINTS["hello_world"],
    )
    assert post_record.status_code == 404


def test_bearer_token_authenticates(session):
    payload, _auth = register_user(session)
    endpoint = ENDPOINTS["hello_world"]
    bearer = {"Authorization": f"Bearer {payload['auth_token']}"}
    assert record(session, bearer, endpoint).status_code == 201
    response = session.get("/hello-world", headers=bearer)
    assert response.status_code == 200
    assert response.text == "Hello World!"


def test_incremental_responses_wrap(session):
    _payload, auth = register_user(session)
    assert record(session, auth, ENDPOINTS["hello_incremental"]).status_code == 201
    bodies = [session.get("/hello-incremental", headers=auth).text for _ in range(3)]
    assert bodies == ["Hello Bob!", "Hello Bill!", "Hello Bob!"]


def test_random_responses_vary(session):
    _payload, auth = register_user(session)
    assert record(session, auth, ENDPOINTS["hello_random"]).status_code == 201
    seen = {session.get("/hello-random", headers=auth).text for _ in range(30)}
    assert len(seen) >= 2
    assert seen <= {f"Hello {name}!" for name in "ABCDE"}


def test_delay_is_honored(session):
    _payload, auth = register_user(session)
    assert record(session, auth, ENDPOINTS["hello_delayed"]).status_code == 201
    started = time.perf_counter()
    response = session.get("/hello-delayed", headers=auth)
    elapsed = time.perf_counter() - started
    assert response.text == "Hello delayed!"
    assert elapsed >= 0.15


def test_response_headers_are_played_back(session):
    _payload, auth = register_user(session)
    assert record(session, auth, ENDPOINTS["header_response"]).status_code == 201
    response = session.get("/respond-with-headers", headers=auth)
    assert response.status_code == 200
    assert response.text == "Now with headers!"
    assert response.headers["x-foo"] == "bar"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-request-id"]


def test_header_names_match_case_insensitively(session):
    _payload, auth = register_user(session)
    endpoint = {
        "method": "get",
        "path": "/cased",
        "headers": {"X-Whom": "bar"},
        "responses": [
            {"status": 200, "content": "cased", "content_type": "text/plain", "delay": 0}
        ],
    }
    assert record(session, auth, endpoint).status_code == 201
    response = session.get("/cased", headers={**auth, "x-whom": "bar"})
    assert response.status_code == 200
    assert response.text == "cased"


def test_specific_header_and_query_win_and_ties_are_409(session):
    _payload, auth = register_user(session)
    for name in ("hello_world", "hello_foo", "hello_foo_other", "hello_bar"):
        assert record(session, auth, ENDPOINTS[name]).status_code == 201

    plain = session.get("/hello-world", headers=auth)
    assert plain.text == "Hello World!"

    foo = session.get("/hello-world", headers=auth, params={"who": "foo"})
    assert foo.text == "Hello Foo!"

    other = session.get("/hello-world", headers=auth, params={"who": "foo", "other": "other"})
    assert other.text == "Hello Other Foo!"

    bar = session.get("/hello-world", headers={**auth, "x-whom": "bar"})
    assert bar.text == "Hello Bar!"

    tie = session.get(
        "/hello-world",
        headers={**auth, "x-whom": "bar"},
        params={"who": "foo"},
    )
    assert tie.status_code == 409
    assert tie.headers["content-type"] == "text/plain; charset=utf-8"
    assert tie.text == "Conflict"


def test_path_match_is_exact(session):
    _payload, auth = register_user(session)
    short = {
        "method": "get",
        "path": "/hello",
        "responses": [{"status": 200, "content": "short", "content_type": "text/plain"}],
    }
    longer = {
        "method": "get",
        "path": "/hello-world",
        "responses": [{"status": 200, "content": "long", "content_type": "text/plain"}],
    }
    assert record(session, auth, short).status_code == 201
    assert record(session, auth, longer).status_code == 201
    assert session.get("/hello", headers=auth).text == "short"
    assert session.get("/hello-world", headers=auth).text == "long"


def test_users_do_not_share_endpoints(session):
    _first, auth_a = register_user(session, "a")
    _second, auth_b = register_user(session, "b")
    assert record(session, auth_a, ENDPOINTS["hello_world"]).status_code == 201
    assert session.get("/hello-world", headers=auth_b).status_code == 404
    assert session.get("/hello-world", headers=auth_a).text == "Hello World!"


def test_schema_files_are_served_without_auth(session):
    schema = session.get("/fake-api-schema.json")
    assert schema.status_code == 200
    assert "UserInfo" in schema.text
    assert '"body"' in schema.text
    document = session.get("/fake-api.openapi.yaml")
    assert document.status_code == 200
    assert "openapi:" in document.text
    assert "body:" in document.text


def test_successful_auth_refreshes_idle_timer(session):
    payload, auth = register_user(session)
    user = session.app.state.fakeapi.store.get(payload["username"])
    user.last_active = 1
    assert record(session, auth, ENDPOINTS["hello_world"]).status_code == 201
    assert user.last_active > 1
    stale = user.last_active
    bad = {"Authorization": "Basic " + base64.b64encode(b"nope:nope").decode()}
    assert session.get("/hello-world", headers=bad).status_code == 401
    assert user.last_active == stale


@pytest.mark.parametrize(
    "name",
    [name for name in ENDPOINTS if not name.startswith("bad_") and name != "hello_random"],
)
def test_recorded_endpoint_plays_back(session, name):
    _payload, auth = register_user(session)
    endpoint = ENDPOINTS[name]
    assert record(session, auth, endpoint).status_code == 201
    headers = {**auth, **(endpoint.get("headers") or {})}
    kwargs = {"headers": headers}
    if endpoint.get("query_parameters"):
        kwargs["params"] = endpoint["query_parameters"]
    if endpoint.get("body") is not None:
        kwargs["content"] = endpoint["body"].encode()
    response = session.request(endpoint["method"], endpoint["path"], **kwargs)
    expected = endpoint["responses"][0]
    assert response.status_code == expected["status"]
    assert response.text == expected["content"]
    assert response.headers["content-type"].startswith(expected["content_type"])
