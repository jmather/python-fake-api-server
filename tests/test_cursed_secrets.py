"""Cursed-secrets corpus.

The fixture is copied verbatim. ``parts`` arrays are concatenated at runtime and
are never written into source as a single literal. The redaction test is
skipped unless ``FAKEAPI_CURSED=1``. With the identity hook it is expected to
fail. ``FAKEAPI_CURSED_STRICT=1`` promotes secret-ish misses to failures.
"""

import base64
import json
import os
import secrets
import uuid
import warnings
from pathlib import Path

import pytest

from tests.conftest import register_user

FIXTURE = Path(__file__).parent / "fixtures" / "cursed-secrets.json"
ROOT = Path(__file__).resolve().parents[1]
SKIPPED = {".venv", ".git", "uploads", "__pycache__", ".ruff_cache", ".pytest_cache"}
SOURCE_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".toml", ".txt", ".json"}


def _load():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _assemble(entry):
    if "parts" not in entry:
        return entry.get("value", "")
    parts = entry["parts"]
    if isinstance(parts, list):
        return "".join(parts)
    note = str(parts)
    body = note.split(":", 1)[1].strip() if note.lower().startswith("note:") else note
    if body.startswith("base64(") and body.endswith(")"):
        return base64.b64encode(body[len("base64(") : -1].encode("utf-8")).decode("ascii")
    marker = "base64 of "
    if body.lower().startswith(marker):
        return base64.b64encode(body[len(marker) :].encode("utf-8")).decode("ascii")
    lowered = body.lower()
    if "cyrillic" in lowered:
        return entry["key"]
    if "response" in lowered:
        return "refresh_" + secrets.token_hex(8)
    if "uuid" in lowered or "false-positive" in lowered:
        return str(uuid.uuid4())
    if "collides" in lowered:
        return entry["key"]
    if "empty" in lowered:
        return ""
    if "unicode" in lowered:
        return chr(0x1F510) + chr(0x1F511)
    raise AssertionError(f"unsupported parts note for {entry['id']}")


def _is_response_entry(entry) -> bool:
    parts = entry.get("parts")
    return isinstance(parts, str) and "RESPONSE" in parts


def _needles(entry, secret):
    if not secret:
        return []
    found = [secret]
    if entry.get("location") == "query" and "&" in secret:
        for part in secret.split("&"):
            value = part.split("=", 1)[-1]
            if value and value not in found:
                found.append(value)
    return found


def _source_text():
    chunks = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIPPED for part in path.parts):
            continue
        if path.resolve() == FIXTURE.resolve():
            continue
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


def test_fixture_keeps_split_parts_out_of_source():
    data = _load()
    assert data["version"] == 1
    corpus = _source_text()
    split = 0
    for entry in data["entries"]:
        parts = entry.get("parts")
        if not isinstance(parts, list):
            continue
        split += 1
        assert len(parts) >= 2
        assert "value" not in entry
        needle = "".join(parts)
        if needle and needle in corpus:
            raise AssertionError(f"{entry['id']} is assembled in source")
    assert split >= 8


@pytest.mark.skipif(
    os.environ.get("FAKEAPI_CURSED") != "1",
    reason="set FAKEAPI_CURSED=1 to run the redaction corpus; identity hook is expected to fail",
)
def test_cursed_secrets_do_not_appear_in_logs(make_session, tmp_path, monkeypatch):
    data = _load()
    assembled = [(entry, _assemble(entry)) for entry in data["entries"]]
    response_body = "\n".join(secret for _entry, secret in assembled if secret)
    xml_path = tmp_path / "cursed.xml"

    # The placeholder hook does nothing. A real redactor is whatever
    # FAKEAPI_LOG_HOOK points at when the operator sets it.
    hook_spec = os.environ.get("FAKEAPI_LOG_HOOK")
    kwargs = {
        "capture": "all",
        "xml_path": str(xml_path),
        "log_stream": __import__("io").StringIO(),
        "hook_from_env": bool(hook_spec),
    }
    if not hook_spec:
        from fake_api_server.redaction import identity

        kwargs["log_hook"] = identity
    else:
        monkeypatch.setenv("FAKEAPI_LOG_HOOK", hook_spec)

    session = make_session(**kwargs)
    _payload, auth = register_user(session, "cursed")

    canned = {
        "status": 200,
        "content": response_body,
        "content_type": "text/plain",
        "delay": 0,
    }
    paths = {
        "header": ("GET", "/c/h"),
        "query": ("GET", "/c/q"),
        "json_body": ("POST", "/c/json"),
        "xml_body": ("POST", "/c/xml"),
        "yaml_body": ("POST", "/c/yaml"),
        "text_body": ("POST", "/c/text"),
        "form_body": ("POST", "/c/form"),
        "response": ("GET", "/c/out"),
    }
    for method, path in paths.values():
        recorded = session.put(
            "/",
            headers={**auth, "X-FakeApi-Action": "record", "Content-Type": "application/json"},
            json={
                "method": method.lower(),
                "path": path,
                "responses": [canned],
            },
        )
        assert recorded.status_code == 201, recorded.text

    for entry, secret in assembled:
        _fire(session, auth, entry, secret)

    session.get("/c/out", headers=auth)
    json_log = session.log.getvalue()
    xml_log = xml_path.read_text(encoding="utf-8")
    grouped = {"definitely": [], "likely": [], "secret-ish": []}
    hard = []
    soft = []
    strict = os.environ.get("FAKEAPI_CURSED_STRICT") == "1"

    for entry, secret in assembled:
        sensitivity = entry["sensitivity"]
        needles = _needles(entry, secret)
        if not needles:
            line = f"PASS {entry['id']} (empty value, no crash check beyond the requests above)"
            grouped.setdefault(sensitivity, []).append(line)
            continue
        hits = []
        if any(needle in json_log for needle in needles):
            hits.append("json")
        if any(needle in xml_log for needle in needles):
            hits.append("xml")
        if hits:
            line = f"FAIL {entry['id']} leaked into {', '.join(hits)}"
            if sensitivity == "secret-ish" and not strict:
                line += " (soft)"
                soft.append(line)
            else:
                hard.append(line)
        else:
            line = f"PASS {entry['id']}"
        grouped.setdefault(sensitivity, []).append(line)

    report = ["cursed-secrets redaction report"]
    for name in ("definitely", "likely", "secret-ish"):
        report.append(name + ":")
        report.extend(f"  {line}" for line in grouped.get(name, []))
    if soft:
        report.append(f"soft failures: {len(soft)} (set FAKEAPI_CURSED_STRICT=1 to fail on these)")
    rendered = "\n".join(report)
    if soft and not hard:
        warnings.warn(rendered, stacklevel=1)
    if hard:
        raise AssertionError(rendered)


def _fire(session, auth, entry, secret):
    if _is_response_entry(entry):
        return
    location = entry["location"]
    key = entry["key"]
    headers = dict(auth)
    if location == "header":
        header_value = secret
        if key.lower() == "authorization" and not secret.lower().startswith(("basic ", "bearer ")):
            header_value = "Basic " + secret
            headers = {key: header_value}
        else:
            headers[key] = header_value
        session.get("/c/h", headers=headers)
        return
    if location == "query":
        session.get(f"/c/q?{key}={secret}", headers=headers)
        return
    if location == "json_body":
        body = '{"' + key + '":"' + secret + '"}'
        headers["Content-Type"] = "application/json"
        session.post("/c/json", headers=headers, content=body.encode("utf-8"))
        return
    if location == "xml_body":
        if key.startswith("@"):
            body = f'<root {key[1:]}="{secret}"/>'
        elif key in {"<!--", "#"}:
            body = secret
        else:
            body = f"<root><{key}>{secret}</{key}></root>"
        headers["Content-Type"] = "application/xml"
        session.post("/c/xml", headers=headers, content=body.encode("utf-8"))
        return
    if location == "yaml_body":
        body = secret if key == "#" else f"{key}: {secret}\n"
        headers["Content-Type"] = "application/yaml"
        session.post("/c/yaml", headers=headers, content=body.encode("utf-8"))
        return
    if location == "text_body":
        headers["Content-Type"] = "text/plain"
        session.post("/c/text", headers=headers, content=secret.encode("utf-8"))
        return
    if location == "form_body":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        session.post("/c/form", headers=headers, content=f"{key}={secret}".encode())
        return
    raise AssertionError(f"unknown location {location} for {entry['id']}")
