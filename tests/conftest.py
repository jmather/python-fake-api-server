"""Shared httpx client for the in-process fake API."""

import io
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from fake_api_server.app import create_app
from fake_api_server.redaction import identity


@dataclass
class Session:
    http: TestClient
    app: object
    log: io.StringIO

    def __getattr__(self, name):
        return getattr(self.http, name)


@pytest.fixture
def make_session():
    opened: list[Session] = []

    def factory(**kwargs):
        log = kwargs.pop("log_stream", None)
        if log is None:
            log = io.StringIO()
        kwargs["log_stream"] = log
        kwargs.setdefault("capture", "none")
        kwargs.setdefault("xml_path", None)
        if kwargs.pop("hook_from_env", False):
            kwargs.pop("log_hook", None)
        else:
            kwargs.setdefault("log_hook", identity)
        app = create_app(**kwargs)
        # TestClient is an httpx.Client with an ASGI transport.
        http = TestClient(app, base_url="http://testserver")
        session = Session(http=http, app=app, log=log)
        opened.append(session)
        return session

    yield factory

    for session in opened:
        session.http.close()
        session.app.state.fakeapi.store.stop()


@pytest.fixture
def session(make_session):
    return make_session()


def register(session, external_id="test", action="register"):
    response = session.post(
        "/",
        headers={"X-FakeApi-Action": action, "Content-Type": "application/json"},
        json={"external_id": external_id},
    )
    return response


def auth_header(payload):
    return {"Authorization": f"Basic {payload['auth_token']}"}


def register_user(session, external_id="test", action="register"):
    response = register(session, external_id=external_id, action=action)
    assert response.status_code == 201, response.text
    payload = response.json()
    return payload, auth_header(payload)


def record(session, auth, endpoint, action="record"):
    headers = {"X-FakeApi-Action": action, "Content-Type": "application/json", **auth}
    return session.put("/", headers=headers, json=endpoint)
