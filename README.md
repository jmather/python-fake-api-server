# Fake API Server

[![CI](https://github.com/jmather/python-fake-api-server/actions/workflows/ci.yml/badge.svg)](https://github.com/jmather/python-fake-api-server/actions/workflows/ci.yml)

Python port of [Jacob Mather](https://github.com/jmather)'s fake API. It records arbitrary endpoints in memory and plays the canned responses back, so a test can stand in for a remote service. Source: [jmather/python-fake-api-server](https://github.com/jmather/python-fake-api-server).

The HTTP contract matches [node-fake-api-server](https://github.com/jmather/node-fake-api-server) and [FakeApi.Server.AspNetCore](https://github.com/jmather/FakeApi.Server.AspNetCore). JSON is snake_case. This process is the one the Python redaction libraries call.

## Run

Requires Python 3.12 or newer.

```bash
uv sync
uv run fake-api-server
```

The process listens on `PORT`, or port 3000 when `PORT` is unset. `--port` overrides `PORT`. It binds `0.0.0.0` and prints `Listening at http://127.0.0.1:<port>`.

```bash
curl -sS -X POST http://127.0.0.1:3000/ \
  -H 'Content-Type: application/json' \
  -H 'X-FakeApi-Action: register' \
  -d '{"external_id":"demo"}'

curl -sS -X PUT http://127.0.0.1:3000/ \
  -u 'USERNAME:PASSWORD' \
  -H 'Content-Type: application/json' \
  -H 'X-FakeApi-Action: record' \
  -d '{"method":"get","path":"/hello","responses":[{"status":200,"content":"Hello","content_type":"text/plain"}]}'

curl -sS -u 'USERNAME:PASSWORD' http://127.0.0.1:3000/hello
```

Use `username` and `password` from the register response. `auth_token` is `base64(username:password)` and is itself a Basic (or Bearer) credential.

From Python:

```python
from fake_api_server import create_app
import uvicorn

uvicorn.run(create_app(), host="127.0.0.1", port=3000)
```

Docker:

```bash
docker build -t fake-api-server .
docker run --rm -p 3000:3000 fake-api-server
```

## Contract

- `POST /` with `X-FakeApi-Action: register` and `{"external_id": "..."}` returns `201` and `{"username", "password", "auth_token"}`. The action value is matched without regard to case. `{}` is valid and stores an empty `external_id`.
- `PUT /` with `X-FakeApi-Action: record`, Basic or Bearer auth, and an endpoint returns `201`, or `409` when the same method, path, body, headers, and query were already recorded.
- Any other request is playback. Missing or bad credentials return `401`. No match returns `404`. Two matches with the same score return `409` with body `Conflict`.
- Matching is exact method and path, then a score of the required headers, the required query parameters, and the optional raw `body`. A higher score wins. Extra headers or query parameters do not count. Header names are compared case-insensitively on the recording and on the request. The body is the raw text the client sent, not a parsed document. Omit `body` to skip body scoring.
- `response_mode` is `incremental` (walk the list and wrap) or `random`. Each response has `status`, `content`, `content_type`, `headers`, and `delay` (milliseconds, waited in full).
- Users live in memory. An idle user is dropped after 2 hours (`last_active < now - limit`; the exact limit is still valid). A sweep runs every 15 minutes. A successful authentication refreshes that user's idle timer. A failed one does not.
- Payloads are checked against [`src/fake_api_server/data/fake-api-schema.json`](src/fake_api_server/data/fake-api-schema.json). Defaults (`response_mode`, `delay`, …) are filled in before required fields are checked. The OpenAPI document is [`src/fake_api_server/data/fake-api.openapi.yaml`](src/fake_api_server/data/fake-api.openapi.yaml). Both are served, without auth, at `/fake-api-schema.json` and `/fake-api.openapi.yaml`.
- A request `Origin` is reflected (`Access-Control-Allow-Credentials: true`). Preflight (`OPTIONS` with `Origin` and `Access-Control-Request-Method`) is `204` and does not require auth.

`401`, `404`, and `409` bodies are `Unauthorized`, `Not Found`, and `Conflict` (`text/plain; charset=utf-8`). A validation failure is `400` with `{"message", "errors": [{"keyword", "path", "message"}]}`. Every response includes `X-Request-Id`.

The shared contract, including the choices that used to differ across the three servers, is [`conformance/STANDARD.md`](conformance/STANDARD.md). [`conformance/`](conformance/) is the suite. `pytest` runs it against a live process.

## Logging and the redaction hook

Every request writes one record through the standard library `logging` module.

1. A `logging.Formatter` emits one JSON object per line on stdout.
2. A `logging.Handler` appends the same record as XML, one `<request-log>` element per line. It is off unless configured.

Two hooks run, both identity unless replaced. The object hook sees the record dict and returns the dict that will be serialized. Each sink then serializes that dict and calls the string hook:

```python
def redact_record(record: dict) -> dict:
    return record

def hook(serialized: str, format: Literal["json", "xml"]) -> str:
    return serialized
```

Order: object hook once, serialize, string hook. The sink writes the string hook's return value unchanged, including the trailing newline.

Pass the hooks into the app factory, or point `FAKEAPI_LOG_HOOK` and `FAKEAPI_LOG_RECORD_HOOK` at them:

- `package.module:function` or `package.module.function` when the value contains `:` or `.`
- otherwise an entry point name. String hooks use the group `fakeapi.log_hooks`. Object hooks use `fakeapi.log_record_hooks`. This package publishes `identity` in both.

An explicit `create_app(log_hook=..., record_hook=...)` argument wins over the environment variable.

```python
def redact(serialized: str, format: str) -> str:
    return serialized.replace("secret", "[redacted]")

def redact_record(record: dict) -> dict:
    return record

app = create_app(log_hook=redact, record_hook=redact_record)
```

### Record

Field names are snake_case: `timestamp` (RFC 3339 UTC, milliseconds), `level` (`info`, `warn` for 4xx, `error` for 5xx), `msg` (`request completed`), `server` (`python`), `request_id`, `method`, `path`, `query`, `status`, `duration_ms`, `action`, `user`.

`user` is the username from a Basic `Authorization` header, never the password, and null for Bearer. `query` uses string values; a repeated key is an array of the raw values (percent-encoding kept). `action` is the `X-FakeApi-Action` header as sent, or null.

`FAKEAPI_LOG_CAPTURE`:

| value | what is logged |
| --- | --- |
| `none` (default) | the fields above only |
| `headers` | plus `request` and `response` objects with lowercased `headers` and `content_type` |
| `all` | those objects, plus the raw request and response bodies |

Bodies are stored as the original text, not re-parsed. They are cut at 64 KiB and the cut form ends with `…[truncated]`.

`FAKEAPI_LOG_XML_PATH` is the XML file. Unset means the XML handler is not installed. With nothing set, logs contain no headers and no bodies.

## Tests

```bash
uv sync --all-groups
uv run pytest
uv run ruff check src tests
```

`tests/fixtures/cursed-secrets.json` is the shared corpus. Entries that use `parts` keep those strings split; tests join them in memory. The corpus test is skipped unless `FAKEAPI_CURSED=1`. It runs three times (string hook only, object hook only, both), parses each JSON line and XML element, and searches the decoded values so an escaped newline still counts. The hooks are identity unless `FAKEAPI_LOG_HOOK` or `FAKEAPI_LOG_RECORD_HOOK` names another one. `secret-ish` misses are reported and do not fail the test unless `FAKEAPI_CURSED_STRICT=1`.

```bash
FAKEAPI_CURSED=1 uv run pytest tests/test_cursed_secrets.py -q
```

That run is expected to fail until a real redactor is passed as `FAKEAPI_LOG_HOOK`, `FAKEAPI_LOG_RECORD_HOOK`, or both.

## Conformance

[`conformance/STANDARD.md`](conformance/STANDARD.md) is the behavior standard with the open points filled in (tie status, 400 body, test clock, CORS, object hook). [`conformance/README.md`](conformance/README.md) is the fixture format and the runner contract. The Python runner is `python -m fake_api_server.conformance_runner`.

Test-only controls, ignored unless set:

| variable | effect |
| --- | --- |
| `FAKEAPI_CLOCK=manual` | honor `X-FakeApi-Now` (unix epoch milliseconds) and purge at the start of each request |
| `FAKEAPI_MAX_IDLE_MS` | idle limit, default `7200000` |
| `FAKEAPI_PURGE_INTERVAL_MS` | sweep interval, default `900000` |

Playback copies response headers except `content-length`, `transfer-encoding`, and `content-type`. `Content-Type` is the recorded `content_type` string, with no charset appended. Query matching uses decoded values joined with `,` when a key is repeated. The log keeps the raw query text.
