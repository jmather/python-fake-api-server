"""JSON lines, the XML sink, capture modes, and the redaction hook."""

import json
import logging
import xml.etree.ElementTree as ET

import pytest

from fake_api_server.app import create_app
from fake_api_server.redaction import identity, resolve_hook
from fake_api_server.request_log import (
    BODY_LIMIT,
    TRUNCATED,
    JsonLineFormatter,
    XmlFormatter,
    build_record,
    configure_request_logger,
    emit_record,
)
from tests.conftest import record, register_user

BASE_FIELDS = {
    "timestamp",
    "level",
    "msg",
    "server",
    "request_id",
    "method",
    "path",
    "query",
    "status",
    "duration_ms",
    "action",
    "user",
}


def _records(session):
    lines = [line for line in session.log.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_capture_none_has_exact_fields_and_omits_bodies(session):
    payload, auth = register_user(session, "demo")
    password = payload["password"]
    response = record(
        session,
        auth,
        {
            "method": "get",
            "path": "/logged",
            "responses": [{"status": 200, "content": "ok", "content_type": "text/plain"}],
        },
    )
    assert response.status_code == 201
    played = session.get("/logged", headers=auth)
    assert played.headers["x-request-id"]

    rows = _records(session)
    assert rows
    completed = rows[-1]
    assert set(completed) == BASE_FIELDS
    assert completed["msg"] == "request completed"
    assert completed["server"] == "python"
    assert completed["method"] == "GET"
    assert completed["path"] == "/logged"
    assert completed["status"] == 200
    assert completed["level"] == "info"
    assert completed["action"] is None
    assert completed["user"] == payload["username"]
    assert completed["user"] != password
    assert password not in session.log.getvalue()
    assert completed["request_id"] == played.headers["x-request-id"]
    assert completed["timestamp"].endswith("Z")
    assert completed["timestamp"][19] == "."
    assert len(completed["timestamp"].split(".")[1]) == 4  # 123Z
    assert isinstance(completed["duration_ms"], (int, float))
    assert "request" not in completed
    assert "response" not in completed


def test_levels_follow_status(make_session):
    session = make_session()
    _payload, auth = register_user(session)
    session.get("/missing", headers=auth)
    warn = _records(session)[-1]
    assert warn["status"] == 404
    assert warn["level"] == "warn"

    record(
        session,
        auth,
        {
            "method": "get",
            "path": "/tie-a",
            "headers": {"x-a": "1"},
            "responses": [{"status": 200, "content": "a", "content_type": "text/plain"}],
        },
    )
    record(
        session,
        auth,
        {
            "method": "get",
            "path": "/tie-a",
            "query_parameters": {"q": "1"},
            "responses": [{"status": 200, "content": "b", "content_type": "text/plain"}],
        },
    )
    # Different paths would not tie. Record two constraints on one path.
    session.log.truncate(0)
    session.log.seek(0)
    tied = session.get("/hello-tie", headers=auth)
    assert tied.status_code == 404

    record(
        session,
        auth,
        {
            "method": "get",
            "path": "/hello-tie",
            "headers": {"x-a": "1"},
            "responses": [{"status": 200, "content": "a", "content_type": "text/plain"}],
        },
    )
    record(
        session,
        auth,
        {
            "method": "get",
            "path": "/hello-tie",
            "query_parameters": {"q": "1"},
            "responses": [{"status": 200, "content": "b", "content_type": "text/plain"}],
        },
    )
    tied = session.get("/hello-tie", headers={**auth, "x-a": "1"}, params={"q": "1"})
    assert tied.status_code == 409
    error = _records(session)[-1]
    assert error["status"] == 409
    assert error["level"] == "warn"


def test_capture_headers_omits_bodies_and_all_keeps_raw_text(make_session):
    headers_session = make_session(capture="headers")
    payload, auth = register_user(headers_session)
    record(
        headers_session,
        auth,
        {
            "method": "post",
            "path": "/raw",
            "responses": [{"status": 200, "content": "pong", "content_type": "text/plain"}],
        },
    )
    headers_session.post(
        "/raw",
        headers={**auth, "Content-Type": "text/plain", "X-Trace": "abc"},
        content=b"not json <raw>",
    )
    captured = _records(headers_session)[-1]
    assert set(captured["request"]) == {"headers", "content_type"}
    assert set(captured["response"]) == {"headers", "content_type"}
    assert captured["request"]["headers"]["x-trace"] == "abc"
    assert captured["request"]["content_type"].startswith("text/plain")
    assert "body" not in captured["request"]

    full = make_session(capture="all")
    payload, auth = register_user(full)
    record(
        full,
        auth,
        {
            "method": "post",
            "path": "/raw",
            "responses": [{"status": 200, "content": "pong", "content_type": "text/plain"}],
        },
    )
    full.post("/raw?a=1&a=2&pwd=p%40ss", headers=auth, content=b"not json <raw>")
    row = _records(full)[-1]
    assert row["request"]["body"] == "not json <raw>"
    assert row["response"]["body"] == "pong"
    assert row["query"]["a"] == ["1", "2"]
    assert row["query"]["pwd"] == "p%40ss"
    assert row["user"] == payload["username"]
    assert "password" not in row


def test_bodies_are_capped(make_session):
    session = make_session(capture="all")
    _payload, auth = register_user(session)
    record(
        session,
        auth,
        {
            "method": "post",
            "path": "/big",
            "responses": [{"status": 200, "content": "ok", "content_type": "text/plain"}],
        },
    )
    raw = b"a" * (BODY_LIMIT + 50)
    session.post("/big", headers=auth, content=raw)
    body = _records(session)[-1]["request"]["body"]
    assert body.endswith(TRUNCATED)
    assert "a" * (BODY_LIMIT + 50) not in body
    assert len(body.encode("utf-8")) <= BODY_LIMIT + len(TRUNCATED.encode("utf-8")) + 4


def test_xml_sink_is_off_by_default_and_hook_replaces_output(make_session, tmp_path, monkeypatch):
    monkeypatch.delenv("FAKEAPI_LOG_XML_PATH", raising=False)
    monkeypatch.delenv("FAKEAPI_LOG_CAPTURE", raising=False)
    monkeypatch.delenv("FAKEAPI_LOG_HOOK", raising=False)
    plain = create_app()
    assert len(plain.state.fakeapi.logger.handlers) == 1
    plain.state.fakeapi.store.stop()

    calls = []

    def hook(serialized, format):
        calls.append((format, serialized))
        return f"[{format}]{serialized}"

    xml_path = tmp_path / "requests.xml"
    session = make_session(capture="all", xml_path=str(xml_path), log_hook=hook)
    register_user(session, "hook")
    assert [item[0] for item in calls] == ["json", "xml"]
    json_serialized, xml_serialized = calls[0][1], calls[1][1]
    assert json_serialized.startswith("{")
    assert json_serialized.endswith("\n")
    assert xml_serialized.startswith("<request-log")
    assert xml_serialized.endswith("\n")
    assert session.log.getvalue() == f"[json]{json_serialized}"
    assert xml_path.read_text(encoding="utf-8") == f"[xml]{xml_serialized}"


def test_xml_record_round_trips_special_characters(tmp_path):
    payload = build_record(
        request_id="req-1",
        method="post",
        path="/xml",
        query={"q": ["one", "two"]},
        status=201,
        duration_ms=1.5,
        action=None,
        user=None,
        capture="all",
        request_headers={"content-type": "text/plain"},
        request_content_type="text/plain",
        request_body=b"a < b & c\nline",
        response_headers={"content-type": "text/plain"},
        response_content_type="text/plain",
        response_body=b"ok",
    )
    stream = __import__("io").StringIO()
    logger = configure_request_logger(
        hook=identity,
        stream=stream,
        xml_path=str(tmp_path / "log.xml"),
    )
    emit_record(logger, payload)
    xml_line = (tmp_path / "log.xml").read_text(encoding="utf-8").strip()
    assert "\n" not in xml_line
    root = ET.fromstring(xml_line)
    assert root.findtext("action") in (None, "")
    assert root.findtext("user") in (None, "")
    assert root.findtext("server") == "python"
    params = [(item.get("name"), item.text) for item in root.find("query")]
    assert params == [("q", "one"), ("q", "two")]
    assert root.find("request/body").text == "a < b & c\nline"
    parsed = json.loads(stream.getvalue())
    assert parsed["request"]["body"] == "a < b & c\nline"
    assert parsed["query"]["q"] == ["one", "two"]


def test_env_capture_xml_path_and_dotted_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKEAPI_LOG_CAPTURE", "headers")
    xml_path = tmp_path / "env.xml"
    monkeypatch.setenv("FAKEAPI_LOG_XML_PATH", str(xml_path))
    monkeypatch.setenv("FAKEAPI_LOG_HOOK", "fake_api_server.redaction:identity")
    app = create_app(log_stream=__import__("io").StringIO())
    try:
        assert app.state.fakeapi.capture == "headers"
        assert app.state.fakeapi.xml_path == str(xml_path)
        assert resolve_hook("identity") is identity or callable(resolve_hook("identity"))
        loaded = resolve_hook("fake_api_server.redaction.identity")
        assert loaded("abc", "json") == "abc"
        entry = resolve_hook("identity")
        assert entry("abc", "xml") == "abc"
    finally:
        app.state.fakeapi.store.stop()


def test_factory_hook_wins_over_env(monkeypatch):
    monkeypatch.setenv("FAKEAPI_LOG_HOOK", "fake_api_server.redaction:identity")

    def hook(serialized, format):
        return f"{format}:{serialized}"

    app = create_app(log_hook=hook, log_stream=__import__("io").StringIO(), capture="none")
    try:
        handler = app.state.fakeapi.logger.handlers[0]
        record = logging.LogRecord(
            "fakeapi",
            logging.INFO,
            __file__,
            1,
            "request completed",
            (),
            None,
        )
        record.fakeapi_record = build_record(
            request_id="id",
            method="GET",
            path="/",
            query={},
            status=200,
            duration_ms=0,
            action=None,
            user=None,
            capture="none",
            request_headers={},
            request_content_type="",
            request_body=b"",
            response_headers={},
            response_content_type="",
            response_body=b"",
        )
        handler.emit(record)
        written = app.state.fakeapi.log_stream.getvalue()
        assert written.startswith("json:")
    finally:
        app.state.fakeapi.store.stop()


def test_xml_handler_reopens_after_logging_shutdown(tmp_path):
    payload = build_record(
        request_id="req-reopen",
        method="GET",
        path="/reopen",
        query={},
        status=200,
        duration_ms=0.1,
        action=None,
        user=None,
        capture="none",
        request_headers={},
        request_content_type="",
        request_body=b"",
        response_headers={},
        response_content_type="",
        response_body=b"",
    )
    path = tmp_path / "reopen.xml"
    stream = __import__("io").StringIO()
    logger = configure_request_logger(hook=identity, stream=stream, xml_path=str(path))
    xml_handler = logger.handlers[1]
    xml_handler.close()
    assert xml_handler.stream is None
    emit_record(logger, payload)
    text = path.read_text(encoding="utf-8")
    assert "<path>/reopen</path>" in text
    assert "<request_id>req-reopen</request_id>" in text


def test_formatters_are_logging_formatters():
    assert issubclass(JsonLineFormatter, logging.Formatter)
    assert issubclass(XmlFormatter, logging.Formatter)


def test_invalid_capture_is_rejected():
    with pytest.raises(ValueError):
        create_app(capture="bodies")


def test_object_hook_runs_before_the_string_hook(make_session, tmp_path):
    events = []

    def record_hook(record):
        events.append("object")
        updated = dict(record)
        updated["msg"] = "request completed [stamped]"
        return updated

    def string_hook(serialized, format):
        events.append((format, "request completed [stamped]" in serialized))
        return serialized

    xml_path = tmp_path / "hook.xml"
    session = make_session(
        capture="none",
        xml_path=str(xml_path),
        log_hook=string_hook,
        record_hook=record_hook,
    )
    register_user(session, "hooks")
    assert events[0] == "object"
    assert events[1] == ("json", True)
    assert events[2] == ("xml", True)
    row = _records(session)[-1]
    assert row["msg"] == "request completed [stamped]"
    assert "<msg>request completed [stamped]</msg>" in xml_path.read_text(encoding="utf-8")


def test_bearer_does_not_populate_log_user(make_session):
    session = make_session()
    payload, _auth = register_user(session)
    record(
        session,
        {"Authorization": f"Basic {payload['auth_token']}"},
        {
            "method": "get",
            "path": "/bearer-user",
            "responses": [{"status": 200, "content": "ok", "content_type": "text/plain"}],
        },
    )
    played = session.get(
        "/bearer-user",
        headers={"Authorization": f"Bearer {payload['auth_token']}"},
    )
    assert played.status_code == 200
    assert _records(session)[-1]["user"] is None


def test_record_hook_from_env(monkeypatch):
    monkeypatch.setenv(
        "FAKEAPI_LOG_RECORD_HOOK",
        "fake_api_server.conformance_hooks:stamp",
    )
    app = create_app(log_stream=__import__("io").StringIO(), capture="none")
    try:
        stamped = app.state.fakeapi.record_hook({"msg": "request completed", "status": 200})
        assert stamped["msg"] == "request completed [stamped]"
        assert stamped["status"] == 200
    finally:
        app.state.fakeapi.store.stop()
