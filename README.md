# Fake API Server

Python port of [Jacob Mather](https://github.com/jmather)'s fake API. It records arbitrary endpoints in memory and plays the canned responses back, so a test can stand in for a remote service.

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
- Any other request is playback. Missing or bad credentials return `401`. No match returns `404`. Two matches with the same score return `500`.
- Matching is exact method and path, then a score of the required headers, the required query parameters, and the optional raw `body`. A higher score wins. Extra headers or query parameters do not count. Header names are compared case-insensitively. The body is the raw text the client sent, not a parsed document. Omit `body` to skip body scoring.
- `response_mode` is `incremental` (walk the list and wrap) or `random`. Each response has `status`, `content`, `content_type`, `headers`, and `delay` (milliseconds).
- Users live in memory. An idle user is dropped after 2 hours. A sweep runs every 15 minutes. A successful authentication refreshes that user's idle timer.
- Payloads are checked against [`src/fake_api_server/data/fake-api-schema.json`](src/fake_api_server/data/fake-api-schema.json). Defaults (`response_mode`, `delay`, …) are filled in before required fields are checked. The OpenAPI document is [`src/fake_api_server/data/fake-api.openapi.yaml`](src/fake_api_server/data/fake-api.openapi.yaml). Both are served, without auth, at `/fake-api-schema.json` and `/fake-api.openapi.yaml`.

`401` and `404` bodies are `Unauthorized` and `Not Found`. `409` is `Conflict`. A tie is JSON: `{"message": "Multiple possible matches were found.", "data": {...}}`. Every response includes `X-Request-Id`.

## Logging and the redaction hook

Every request writes one record through the standard library `logging` module.

1. A `logging.Formatter` emits one JSON object per line on stdout.
2. A `logging.Handler` appends the same record as XML, one `<request-log>` element per line. It is off unless configured.

After a sink serializes the record, and before it writes, it calls:

```python
def hook(serialized: str, format: Literal["json", "xml"]) -> str:
    return serialized
```

The sink writes the return value unchanged, including the trailing newline. The default hook is identity.

Pass the hook into the app factory, or point `FAKEAPI_LOG_HOOK` at it:

- `package.module:function` or `package.module.function` when the value contains `:` or `.`
- otherwise an entry point name in the group `fakeapi.log_hooks` (this package publishes `identity`)

An explicit `create_app(log_hook=...)` argument wins over the environment variable.

```python
def redact(serialized: str, format: str) -> str:
    return serialized.replace("secret", "[redacted]")

app = create_app(log_hook=redact)
```

### Record

Field names are snake_case: `timestamp` (RFC 3339 UTC, milliseconds), `level` (`info`, `warn` for 4xx, `error` for 5xx), `msg` (`request completed`), `server` (`python`), `request_id`, `method`, `path`, `query`, `status`, `duration_ms`, `action`, `user`.

`user` is the username from the Authorization header, never the password. `query` uses string values; a repeated key is an array of the raw values. `action` is the `X-FakeApi-Action` header, or null.

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

`tests/fixtures/cursed-secrets.json` is the shared corpus. Entries that use `parts` keep those strings split; tests join them in memory. The corpus test is skipped unless `FAKEAPI_CURSED=1`. It installs the identity hook unless `FAKEAPI_LOG_HOOK` names another one, sends every value in the location the fixture describes (including canned response bodies), and fails when an assembled value still appears in the JSON or XML log. `secret-ish` misses are reported and do not fail the test unless `FAKEAPI_CURSED_STRICT=1`.

```bash
FAKEAPI_CURSED=1 uv run pytest tests/test_cursed_secrets.py -q
```

That run is expected to fail until a real redactor is passed as `FAKEAPI_LOG_HOOK`.

## Decisions where the Node and .NET servers differ

- A tie is `500`, as in .NET and the shared contract. Node returns `400`.
- Optional `body` is part of scoring and of duplicate detection, as in .NET. It was added to the OpenAPI document so the published schema and the validator agree. Body comparison is exact raw text.
- Only `POST /` plus `register`, and `PUT /` plus `record`, are meta actions. Other combinations are playback. Node would still treat `PUT /` plus `register` as registration.
- Method and path must be equal. Both reference servers use a `startsWith` hash, so a recorded `/hello` also becomes a candidate for `/hello-world`.
- Header names match case-insensitively. .NET lowercases the incoming headers and asks you to record lowercase names; this port accepts either.
- `401` / `404` / `409` use the Express status text (`Unauthorized`, `Not Found`, `Conflict`). The .NET `409` body is `Matching endpoint already recorded.` The `WWW-Authenticate` realm is `FakeAPI`. Node sends the realm `FakseAPI`.
- Idle time refreshes on every successful authentication. Node does this. The .NET authentication handler does not.
- `delay` waits the full recorded duration, as .NET's `Task.Delay` does. Node subtracts elapsed time and an 8ms allowance.
- Schema files are served as static documents, as in Node. The .NET app does not serve them.
- The .NET CORS policy is not implemented. Node does not implement it either.
- Validation messages come from `jsonschema`, not from Ajv, so the wording of a `400` differs from Node.
- Query matching uses decoded values, joined with `,` when a key is repeated (ASP.NET `StringValues`). The log keeps the raw query text so percent-encoding stays visible.
- `FAKEAPI_LOG_CAPTURE=headers` includes headers and content types and omits bodies.
- Register's JSON body is exactly the three credential fields. .NET also serializes an `id`.
- A `201` includes a `Location` header, as ASP.NET `Created` does.
- Bearer is accepted when the token is `base64(username:password)`. Both servers decode the Authorization parameter without checking the scheme, and the OpenAPI document lists bearer auth.
- Endpoint `content_type` (a .NET model field used by its test client when sending) is not a match field and is not in the schema. `content-length` and `transfer-encoding` on a canned response are not copied onto the playback response.
