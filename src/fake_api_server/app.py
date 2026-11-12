"""FastAPI application factory for the fake API server."""

import asyncio
import base64
import binascii
import os
import secrets
import sys
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, unquote_plus

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fake_api_server.matching import Endpoint, EndpointResponse, MultipleMatchError
from fake_api_server.redaction import LogHook, resolve_hook
from fake_api_server.request_log import build_record, configure_request_logger, emit_record
from fake_api_server.store import User, UserStore
from fake_api_server.validate import PayloadError, load_text, parse_json_object, validate_entity

_UNSET = object()

SCHEMA_FILES = {
    "/fake-api-schema.json": ("fake-api-schema.json", "application/json"),
    "/fake-api.openapi.yaml": ("fake-api.openapi.yaml", "application/yaml"),
}

HOP_BY_HOP = {"content-length", "transfer-encoding", "content-type"}


def create_app(
    *,
    log_hook: LogHook | None | object = _UNSET,
    log_stream: Any = _UNSET,
    capture: str | None | object = _UNSET,
    xml_path: str | None | object = _UNSET,
    max_idle: timedelta = timedelta(hours=2),
    purge_interval: timedelta = timedelta(minutes=15),
    id_factory: Any = None,
) -> FastAPI:
    """Build the fake API app.

    ``log_hook`` overrides ``FAKEAPI_LOG_HOOK``. When both are omitted the hook
    is identity. ``capture`` overrides ``FAKEAPI_LOG_CAPTURE`` (``none``,
    ``headers``, or ``all``). ``xml_path`` overrides ``FAKEAPI_LOG_XML_PATH``;
    unset disables the XML sink.
    """
    hook = _resolve_log_hook(log_hook)
    stream = sys.stdout if log_stream is _UNSET else log_stream
    capture_mode = _resolve_capture(capture)
    xml = _resolve_xml_path(xml_path)
    store = UserStore(max_idle=max_idle, purge_interval=purge_interval, id_factory=id_factory)
    logger = configure_request_logger(hook=hook, stream=stream, xml_path=xml)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.fakeapi.store.start()
        try:
            yield
        finally:
            app.state.fakeapi.store.stop()

    app = FastAPI(
        title="Fake API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.fakeapi = _State(
        store=store,
        logger=logger,
        capture=capture_mode,
        log_stream=stream,
        xml_path=xml,
    )
    store.start()

    @app.middleware("http")
    async def dispatch(request: Request, call_next):  # noqa: ARG001
        return await handle_request(request, app.state.fakeapi)

    return app


class _State:
    def __init__(self, store: UserStore, logger, capture: str, log_stream, xml_path: str | None):
        self.store = store
        self.logger = logger
        self.capture = capture
        self.log_stream = log_stream
        self.xml_path = xml_path


def _resolve_log_hook(log_hook: LogHook | None | object) -> LogHook:
    if log_hook is _UNSET or log_hook is None:
        return resolve_hook(os.environ.get("FAKEAPI_LOG_HOOK"))
    return log_hook  # type: ignore[return-value]


def _resolve_capture(capture: str | None | object) -> str:
    if capture is _UNSET or capture is None:
        raw = os.environ.get("FAKEAPI_LOG_CAPTURE", "none")
    else:
        raw = str(capture)
    mode = raw.strip().lower()
    if mode not in {"none", "headers", "all"}:
        raise ValueError("FAKEAPI_LOG_CAPTURE must be none, headers, or all")
    return mode


def _resolve_xml_path(xml_path: str | None | object) -> str | None:
    if xml_path is _UNSET:
        raw = os.environ.get("FAKEAPI_LOG_XML_PATH")
    else:
        raw = None if xml_path is None else str(xml_path)
    if raw is None or raw.strip() == "":
        return None
    return raw


async def handle_request(request: Request, state: _State) -> Response:
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    raw_body = await request.body()
    try:
        response = await route_request(request, state, raw_body)
    except MultipleMatchError as exc:
        response = JSONResponse(
            {
                "message": "Multiple possible matches were found.",
                "data": {
                    "best_match_count": exc.best_score,
                    "best_matches": [
                        {"method": item.method, "path": item.path, "score": exc.best_score}
                        for item in exc.matches
                    ],
                },
            },
            status_code=500,
        )
    except Exception as exc:
        response = JSONResponse(
            {"message": "Internal Server Error", "errors": [{"message": str(exc)}]},
            status_code=500,
        )
    response.headers["X-Request-Id"] = request_id
    _log_exchange(request, response, state, request_id, raw_body, started)
    return response


async def route_request(request: Request, state: _State, raw_body: bytes) -> Response:
    method = request.method.upper()
    path = request.url.path
    action = request.headers.get("x-fakeapi-action")
    action_key = action.casefold() if action else None

    if method == "GET" and path in SCHEMA_FILES:
        filename, media_type = SCHEMA_FILES[path]
        return Response(content=load_text(filename), media_type=media_type, status_code=200)

    if method == "POST" and path == "/" and action_key == "register":
        return _register(state.store, raw_body, request)

    user = _authenticate(state.store, request.headers.get("authorization"))
    if method == "PUT" and path == "/" and action_key == "record":
        if user is None:
            return _unauthorized()
        return _record(state.store, user, raw_body, request)

    if user is None:
        return _unauthorized()
    return await _playback(state.store, user, request, raw_body)


def _register(store: UserStore, raw_body: bytes, request: Request) -> Response:
    try:
        payload = parse_json_object(raw_body)
        validate_entity("UserInfo", payload)
    except PayloadError as exc:
        return _bad_request(exc)
    external_id = payload.get("external_id", "") if isinstance(payload, dict) else ""
    user = store.create("" if external_id is None else str(external_id))
    body = {
        "username": user.username,
        "password": user.password,
        "auth_token": user.auth_token,
    }
    return JSONResponse(body, status_code=201, headers={"Location": str(request.url)})


def _record(store: UserStore, user: User, raw_body: bytes, request: Request) -> Response:
    try:
        payload = parse_json_object(raw_body)
        validate_entity("Endpoint", payload)
    except PayloadError as exc:
        return _bad_request(exc)
    if not isinstance(payload, dict):
        return _bad_request(
            PayloadError("Request payload is malformed.", [{"message": "Expected object"}])
        )
    endpoint = _endpoint_from_payload(payload)
    if not store.record(user, endpoint):
        return Response(content="Conflict", status_code=409, media_type="text/plain")
    return JSONResponse(payload, status_code=201, headers={"Location": str(request.url)})


def _endpoint_from_payload(payload: dict[str, Any]) -> Endpoint:
    responses = [
        EndpointResponse(
            status=int(item["status"]),
            content="" if item.get("content") is None else str(item.get("content")),
            content_type=str(item["content_type"]),
            headers={str(key): str(value) for key, value in (item.get("headers") or {}).items()},
            delay=int(item.get("delay") or 0),
        )
        for item in payload["responses"]
    ]
    headers = {
        str(key).lower(): str(value) for key, value in (payload.get("headers") or {}).items()
    }
    query = {
        str(key): str(value) for key, value in (payload.get("query_parameters") or {}).items()
    }
    body = payload["body"] if "body" in payload else None
    return Endpoint(
        method=str(payload["method"]),
        path=str(payload["path"]),
        body=None if body is None else str(body),
        headers=headers,
        query=query,
        response_mode=str(payload.get("response_mode") or "incremental"),
        responses=responses,
    )


async def _playback(store: UserStore, user: User, request: Request, raw_body: bytes) -> Response:
    playback = store.playback(
        user,
        method=request.method,
        path=request.url.path,
        headers=_match_headers(request.headers),
        query=_match_query(request.scope.get("query_string", b"")),
        body=_decode_body(raw_body),
        randbelow=secrets.randbelow,
    )
    if playback is None:
        return Response(content="Not Found", status_code=404, media_type="text/plain")
    if playback.delay > 0:
        await asyncio.sleep(playback.delay / 1000)
    header_map = {
        key: value
        for key, value in playback.headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    return Response(
        content=playback.content,
        status_code=playback.status,
        media_type=playback.content_type,
        headers=header_map,
    )


def _authenticate(store: UserStore, header: str | None) -> User | None:
    parsed = _parse_credentials(header)
    if parsed is None:
        return None
    username, password = parsed
    return store.authenticate(username, password)


def _parse_credentials(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    scheme, _, parameter = header.partition(" ")
    if scheme.lower() not in {"basic", "bearer"} or not parameter:
        return None
    try:
        decoded = base64.b64decode(parameter.strip(), validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


def _claimed_username(header: str | None) -> str | None:
    parsed = _parse_credentials(header)
    if parsed is None:
        return None
    return parsed[0]


def _bad_request(exc: PayloadError) -> JSONResponse:
    return JSONResponse({"message": exc.message, "errors": exc.errors}, status_code=400)


def _unauthorized() -> Response:
    return Response(
        content="Unauthorized",
        status_code=401,
        media_type="text/plain",
        headers={"WWW-Authenticate": 'Basic realm="FakeAPI"'},
    )


def _match_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def _match_query(raw_query: bytes | str) -> dict[str, str]:
    text = raw_query.decode("latin-1") if isinstance(raw_query, bytes) else raw_query
    if not text:
        return {}
    parsed = parse_qs(text, keep_blank_values=True, strict_parsing=False)
    matched: dict[str, str] = {}
    for key, values in parsed.items():
        matched[key] = values[0] if len(values) == 1 else ",".join(values)
    return matched


def _log_query(raw_query: bytes | str) -> dict[str, Any]:
    """Log query values as sent. Repeated keys become an array. Values stay raw."""
    text = raw_query.decode("latin-1") if isinstance(raw_query, bytes) else raw_query
    if not text:
        return {}
    buckets: dict[str, list[str]] = {}
    for part in text.split("&"):
        if part == "":
            continue
        if "=" in part:
            key, value = part.split("=", 1)
        else:
            key, value = part, ""
        buckets.setdefault(unquote_plus(key), []).append(value)
    logged: dict[str, Any] = {}
    for key, values in buckets.items():
        logged[key] = values[0] if len(values) == 1 else values
    return logged


def _decode_body(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _log_exchange(
    request: Request,
    response: Response,
    state: _State,
    request_id: str,
    raw_body: bytes,
    started: float,
) -> None:
    response_body = bytes(response.body or b"")
    payload = build_record(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        query=_log_query(request.scope.get("query_string", b"")),
        status=response.status_code,
        duration_ms=(time.perf_counter() - started) * 1000,
        action=request.headers.get("x-fakeapi-action"),
        user=_claimed_username(request.headers.get("authorization")),
        capture=state.capture,
        request_headers={key.lower(): value for key, value in request.headers.items()},
        request_content_type=request.headers.get("content-type", ""),
        request_body=raw_body,
        response_headers={key.lower(): value for key, value in response.headers.items()},
        response_content_type=response.headers.get("content-type", ""),
        response_body=response_body,
    )
    emit_record(state.logger, payload)
