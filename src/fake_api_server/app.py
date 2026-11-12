"""FastAPI application factory for the fake API server."""

import asyncio
import base64
import binascii
import json
import os
import re
import secrets
import sys
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, unquote_plus

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from fake_api_server.matching import Endpoint, EndpointResponse, MultipleMatchError
from fake_api_server.redaction import (
    LogHook,
    RecordHook,
    apply_record_hook,
    resolve_hook,
    resolve_record_hook,
)
from fake_api_server.request_log import build_record, configure_request_logger, emit_record
from fake_api_server.store import User, UserStore, system_now_ms
from fake_api_server.validate import (
    PayloadError,
    invalid,
    load_text,
    parse_json_object,
    validate_entity,
)

_UNSET = object()
_NOW_MS: ContextVar[int | None] = ContextVar("fakeapi_now_ms", default=None)
_NOW_HEADER = re.compile(r"^-?(0|[1-9][0-9]*)$")

SCHEMA_FILES = {
    "/fake-api-schema.json": ("fake-api-schema.json", "application/json; charset=utf-8"),
    "/fake-api.openapi.yaml": ("fake-api.openapi.yaml", "application/yaml"),
}

HOP_BY_HOP = {"content-length", "transfer-encoding", "content-type"}
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"


def create_app(
    *,
    log_hook: LogHook | None | object = _UNSET,
    record_hook: RecordHook | None | object = _UNSET,
    log_stream: Any = _UNSET,
    capture: str | None | object = _UNSET,
    xml_path: str | None | object = _UNSET,
    max_idle: timedelta | None = None,
    purge_interval: timedelta | None = None,
    id_factory: Any = None,
) -> FastAPI:
    """Build the fake API app.

    ``log_hook`` overrides ``FAKEAPI_LOG_HOOK``. ``record_hook`` overrides
    ``FAKEAPI_LOG_RECORD_HOOK``. When both are omitted the hooks are identity.
    ``capture`` overrides ``FAKEAPI_LOG_CAPTURE`` (``none``, ``headers``, or
    ``all``). ``xml_path`` overrides ``FAKEAPI_LOG_XML_PATH``; unset disables
    the XML sink.

    ``FAKEAPI_MAX_IDLE_MS`` (default 7200000) and ``FAKEAPI_PURGE_INTERVAL_MS``
    (default 900000) override the idle limit and the sweep interval when the
    matching argument is omitted. ``FAKEAPI_CLOCK=manual`` disables the
    background sweep and reads ``X-FakeApi-Now`` (unix epoch milliseconds) as
    the clock for that request. The header is ignored unless the clock is manual.
    """
    hook = _resolve_log_hook(log_hook)
    objects = _resolve_record_hook(record_hook)
    stream = sys.stdout if log_stream is _UNSET else log_stream
    capture_mode = _resolve_capture(capture)
    xml = _resolve_xml_path(xml_path)
    manual_clock = _manual_clock()
    idle = _idle_from_env() if max_idle is None else max_idle
    interval = _purge_from_env() if purge_interval is None else purge_interval
    store = UserStore(
        max_idle=idle,
        purge_interval=interval,
        id_factory=id_factory,
        clock=_clock_ms,
        sweep=not manual_clock,
    )
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
        record_hook=objects,
        manual_clock=manual_clock,
    )
    store.start()

    @app.middleware("http")
    async def dispatch(request: Request, call_next):  # noqa: ARG001
        return await handle_request(request, app.state.fakeapi)

    return app


class _State:
    def __init__(
        self,
        store: UserStore,
        logger,
        capture: str,
        log_stream,
        xml_path: str | None,
        record_hook: RecordHook,
        manual_clock: bool,
    ):
        self.store = store
        self.logger = logger
        self.capture = capture
        self.log_stream = log_stream
        self.xml_path = xml_path
        self.record_hook = record_hook
        self.manual_clock = manual_clock


def _resolve_log_hook(log_hook: LogHook | None | object) -> LogHook:
    if log_hook is _UNSET or log_hook is None:
        return resolve_hook(os.environ.get("FAKEAPI_LOG_HOOK"))
    return log_hook  # type: ignore[return-value]


def _resolve_record_hook(record_hook: RecordHook | None | object) -> RecordHook:
    if record_hook is _UNSET or record_hook is None:
        return resolve_record_hook(os.environ.get("FAKEAPI_LOG_RECORD_HOOK"))
    return record_hook  # type: ignore[return-value]


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


def _millis_env(name: str, default: int, *, minimum: int) -> timedelta:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        text = raw.strip()
        if not text.isdigit():
            raise ValueError(f"{name} must be an integer number of milliseconds >= {minimum}")
        value = int(text)
    if value < minimum:
        raise ValueError(f"{name} must be an integer number of milliseconds >= {minimum}")
    try:
        return timedelta(milliseconds=value)
    except OverflowError as exc:
        raise ValueError(f"{name} is too large") from exc


def _idle_from_env() -> timedelta:
    return _millis_env("FAKEAPI_MAX_IDLE_MS", 7_200_000, minimum=0)


def _purge_from_env() -> timedelta:
    return _millis_env("FAKEAPI_PURGE_INTERVAL_MS", 900_000, minimum=1)


def _manual_clock() -> bool:
    raw = os.environ.get("FAKEAPI_CLOCK", "system").strip().lower()
    if raw in {"", "system"}:
        return False
    if raw == "manual":
        return True
    raise ValueError("FAKEAPI_CLOCK must be system or manual")


def _clock_ms() -> int:
    override = _NOW_MS.get()
    if override is not None:
        return override
    return system_now_ms()


def _parse_now_header(raw: str | None) -> int | None:
    """Return the injected timestamp, or None when the header is absent.

    A present but illegal value raises PayloadError.
    """
    if raw is None:
        return None
    text = raw.strip()
    if _NOW_HEADER.fullmatch(text) is None:
        raise invalid(
            [
                {
                    "keyword": "type",
                    "path": "",
                    "message": "X-FakeApi-Now must be an integer number of milliseconds.",
                }
            ]
        )
    return int(text)


async def handle_request(request: Request, state: _State) -> Response:
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    raw_body = await request.body()
    token = None
    try:
        if state.manual_clock:
            now = _parse_now_header(request.headers.get("x-fakeapi-now"))
            token = _NOW_MS.set(now)
            state.store.purge()
        response = await route_request(request, state, raw_body)
    except MultipleMatchError:
        response = text_response("Conflict", 409)
    except PayloadError as exc:
        response = json_response({"message": exc.message, "errors": exc.errors}, 400)
    except Exception as exc:
        response = json_response(
            {"message": "Internal Server Error", "errors": [{"message": str(exc)}]},
            500,
        )
    finally:
        if token is not None:
            _NOW_MS.reset(token)
    _apply_cors(request, response)
    response.headers["X-Request-Id"] = request_id
    _log_exchange(request, response, state, request_id, raw_body, started)
    return response


async def route_request(request: Request, state: _State, raw_body: bytes) -> Response:
    method = request.method.upper()
    path = request.url.path
    if _is_preflight(request):
        return _preflight(request)

    action = request.headers.get("x-fakeapi-action")
    action_key = action.casefold() if action else None

    if method == "GET" and path in SCHEMA_FILES:
        filename, media_type = SCHEMA_FILES[path]
        return Response(
            content=load_text(filename),
            status_code=200,
            headers={"content-type": media_type},
        )

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


def _is_preflight(request: Request) -> bool:
    return (
        request.method.upper() == "OPTIONS"
        and bool(request.headers.get("origin"))
        and bool(request.headers.get("access-control-request-method"))
    )


def _preflight(request: Request) -> Response:
    headers = {
        "access-control-allow-methods": request.headers["access-control-request-method"],
    }
    requested = request.headers.get("access-control-request-headers")
    if requested:
        headers["access-control-allow-headers"] = requested
    return Response(status_code=204, headers=headers)


def _apply_cors(request: Request, response: Response) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    response.headers["access-control-allow-origin"] = origin
    response.headers["access-control-allow-credentials"] = "true"
    existing = response.headers.get("vary")
    if not existing:
        response.headers["vary"] = "Origin"
        return
    parts = [part.strip().lower() for part in existing.split(",")]
    if "origin" not in parts:
        response.headers["vary"] = existing + ", Origin"


def _register(store: UserStore, raw_body: bytes, request: Request) -> Response:
    payload = parse_json_object(raw_body)
    validate_entity("UserInfo", payload)
    external_id = payload.get("external_id", "") if isinstance(payload, dict) else ""
    user = store.create("" if external_id is None else str(external_id))
    body = {
        "username": user.username,
        "password": user.password,
        "auth_token": user.auth_token,
    }
    return json_response(body, 201, {"Location": str(request.url)})


def _record(store: UserStore, user: User, raw_body: bytes, request: Request) -> Response:
    payload = parse_json_object(raw_body)
    validate_entity("Endpoint", payload)
    if not isinstance(payload, dict):
        raise invalid([{"keyword": "type", "path": "", "message": "must be object"}])
    endpoint = _endpoint_from_payload(payload)
    if not store.record(user, endpoint):
        return text_response("Conflict", 409)
    return json_response(payload, 201, {"Location": str(request.url)})


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
        return text_response("Not Found", 404)
    if playback.delay > 0:
        await asyncio.sleep(playback.delay / 1000)
    header_map = {
        key: value
        for key, value in playback.headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    header_map["content-type"] = playback.content_type
    return Response(content=playback.content, status_code=playback.status, headers=header_map)


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
    return _decode_userinfo(parameter.strip())


def _decode_userinfo(parameter: str) -> tuple[str, str] | None:
    try:
        decoded = base64.b64decode(parameter, validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    return username, password


def _basic_username(header: str | None) -> str | None:
    """Username for the log record. Bearer credentials do not populate ``user``."""
    if not header:
        return None
    scheme, _, parameter = header.partition(" ")
    if scheme.lower() != "basic" or not parameter:
        return None
    parsed = _decode_userinfo(parameter.strip())
    if parsed is None:
        return None
    return parsed[0]


def json_response(body: Any, status: int, headers: dict[str, str] | None = None) -> Response:
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    merged = {"content-type": JSON_CONTENT_TYPE}
    if headers:
        merged.update(headers)
        merged["content-type"] = JSON_CONTENT_TYPE
    return Response(content=payload, status_code=status, headers=merged)


def text_response(body: str, status: int, headers: dict[str, str] | None = None) -> Response:
    merged = {"content-type": TEXT_CONTENT_TYPE}
    if headers:
        merged.update(headers)
        merged["content-type"] = TEXT_CONTENT_TYPE
    return Response(content=body, status_code=status, headers=merged)


def _unauthorized() -> Response:
    return text_response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="FakeAPI"'})


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
        user=_basic_username(request.headers.get("authorization")),
        capture=state.capture,
        request_headers={key.lower(): value for key, value in request.headers.items()},
        request_content_type=request.headers.get("content-type", ""),
        request_body=raw_body,
        response_headers={key.lower(): value for key, value in response.headers.items()},
        response_content_type=response.headers.get("content-type", ""),
        response_body=response_body,
    )
    prepared = apply_record_hook(payload, state.record_hook)
    if prepared is None:
        return
    emit_record(state.logger, prepared)
