"""Locked decisions: validation shape, CORS, and the manual test clock."""

from fake_api_server.app import create_app
from fake_api_server.validate import PayloadError, parse_json_object, validate_entity
from tests.conftest import register_user


def test_validation_messages_are_stable(session):
    _payload, auth = register_user(session)
    cases = [
        (
            "post",
            "/",
            {"X-FakeApi-Action": "register"},
            b"",
            "Request payload is malformed.",
            [{"keyword": "parse", "path": "", "message": "Request body is empty."}],
        ),
        (
            "post",
            "/",
            {"X-FakeApi-Action": "register"},
            b"{",
            "Request payload is malformed.",
            [{"keyword": "parse", "path": "", "message": "Request body is not valid JSON."}],
        ),
        (
            "post",
            "/",
            {"X-FakeApi-Action": "register"},
            b"\xff",
            "Request payload is malformed.",
            [{"keyword": "parse", "path": "", "message": "Request body is not valid UTF-8."}],
        ),
        (
            "post",
            "/",
            {"X-FakeApi-Action": "register", "Content-Type": "application/json"},
            b"[]",
            "Request payload is invalid.",
            [{"keyword": "type", "path": "", "message": "must be object"}],
        ),
        (
            "post",
            "/",
            {"X-FakeApi-Action": "register", "Content-Type": "application/json"},
            b'{"external_id":1}',
            "Request payload is invalid.",
            [{"keyword": "type", "path": "/external_id", "message": "must be string"}],
        ),
    ]
    for method, path, headers, body, message, errors in cases:
        response = session.request(method, path, headers=headers, content=body)
        assert response.status_code == 400, response.text
        assert response.json() == {"message": message, "errors": errors}

    record_headers = {**auth, "X-FakeApi-Action": "record", "Content-Type": "application/json"}
    record_cases = [
        (
            b'{"method":"GET","path":"/ok","responses":[{"status":200,"content":"x","content_type":"text/plain"}]}',
            [
                {
                    "keyword": "enum",
                    "path": "/method",
                    "message": (
                        'must be one of: "get", "post", "put", "patch", "delete", "head", "options"'
                    ),
                }
            ],
        ),
        (
            b'{"method":"get","path":"/ok","responses":[]}',
            [
                {
                    "keyword": "minItems",
                    "path": "/responses",
                    "message": "must have at least 1 items",
                }
            ],
        ),
        (
            b'{"method":"get","path":"/ok","responses":[{"status":99,"content":"x","content_type":"text/plain"}]}',
            [{"keyword": "minimum", "path": "/responses/0/status", "message": "must be >= 100"}],
        ),
        (
            b'{"method":"get","path":"/ok","headers":{"X-FakeApi-Trace":"1"},"responses":[{"status":200,"content":"x","content_type":"text/plain"}]}',
            [
                {
                    "keyword": "propertyNames",
                    "path": "/headers/X-FakeApi-Trace",
                    "message": "property name 'X-FakeApi-Trace' is not allowed",
                }
            ],
        ),
    ]
    for body, errors in record_cases:
        response = session.put("/", headers=record_headers, content=body)
        assert response.status_code == 400, response.text
        assert response.json()["errors"] == errors


def test_two_additional_properties_are_sorted():
    try:
        validate_entity("UserInfo", {"broken": True, "also": 1})
    except PayloadError as exc:
        assert exc.errors == [
            {
                "keyword": "additionalProperties",
                "path": "",
                "message": "must not have additional property 'also'",
            },
            {
                "keyword": "additionalProperties",
                "path": "",
                "message": "must not have additional property 'broken'",
            },
        ]
    else:
        raise AssertionError("expected a validation error")
    parse_json_object(b"{}")


def test_cors_reflects_origin_and_preflight_is_unauthenticated(session):
    rejected = session.get("/nope", headers={"Origin": "https://app.example"})
    assert rejected.status_code == 401
    assert rejected.headers["access-control-allow-origin"] == "https://app.example"
    assert rejected.headers["access-control-allow-credentials"] == "true"
    assert rejected.headers["vary"] == "Origin"
    assert rejected.text == "Unauthorized"

    preflight = session.options(
        "/nope",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Trace, X-Other",
        },
    )
    assert preflight.status_code == 204
    assert preflight.content == b""
    assert preflight.headers["access-control-allow-origin"] == "https://app.example"
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert preflight.headers["access-control-allow-methods"] == "GET"
    assert preflight.headers["access-control-allow-headers"] == "X-Trace, X-Other"
    assert preflight.headers["vary"] == "Origin"
    assert "www-authenticate" not in preflight.headers
    assert "content-type" not in preflight.headers

    plain = session.options("/nope")
    assert plain.status_code == 401
    assert "access-control-allow-origin" not in plain.headers


def test_manual_clock_refreshes_and_expires(make_session, monkeypatch):
    monkeypatch.setenv("FAKEAPI_CLOCK", "manual")
    session = make_session()
    registered = session.post(
        "/",
        headers={"X-FakeApi-Action": "register", "X-FakeApi-Now": "1700000000000"},
        json={"external_id": "clock"},
    )
    assert registered.status_code == 201, registered.text
    payload = registered.json()
    auth = {
        "Authorization": f"Basic {payload['auth_token']}",
        "X-FakeApi-Action": "record",
        "X-FakeApi-Now": "1700000000000",
    }
    recorded = session.put(
        "/",
        headers=auth,
        json={
            "method": "get",
            "path": "/tick",
            "responses": [{"status": 200, "content": "ok", "content_type": "text/plain"}],
        },
    )
    assert recorded.status_code == 201, recorded.text

    exactly = session.get(
        "/tick",
        headers={"Authorization": auth["Authorization"], "X-FakeApi-Now": "1700007200000"},
    )
    assert exactly.status_code == 200
    still = session.get(
        "/tick",
        headers={"Authorization": auth["Authorization"], "X-FakeApi-Now": "1700007200001"},
    )
    assert still.status_code == 200

    other = session.post(
        "/",
        headers={"X-FakeApi-Action": "register", "X-FakeApi-Now": "1800000000000"},
        json={"external_id": "other"},
    )
    token = other.json()["auth_token"]
    denied = session.get(
        "/missing",
        headers={
            "Authorization": "Basic bm90LXZhbGlk",
            "X-FakeApi-Now": "1800000001000",
        },
    )
    assert denied.status_code == 401
    expired = session.get(
        "/tick",
        headers={"Authorization": f"Basic {token}", "X-FakeApi-Now": "1800007200001"},
    )
    assert expired.status_code == 401

    broken = session.get("/tick", headers={"X-FakeApi-Now": "12.5"})
    assert broken.status_code == 400
    assert broken.json()["errors"][0]["message"].startswith("X-FakeApi-Now")


def test_clock_env_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("FAKEAPI_CLOCK", "frozen")
    try:
        create_app()
    except ValueError as exc:
        assert "FAKEAPI_CLOCK" in str(exc)
    else:
        raise AssertionError("expected ValueError")
