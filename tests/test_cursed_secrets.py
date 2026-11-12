"""Cursed-secrets corpus.

The fixture is copied verbatim. ``parts`` arrays are concatenated at runtime and
are never written into source as a single literal. The redaction test is
skipped unless ``FAKEAPI_CURSED=1``. It runs three times: string hook only,
object hook only, and both. Each run parses JSON lines and XML elements and
searches the decoded values, so a newline that was escaped as ``\\n`` or
``&#10;`` still counts as a leak. With the identity hooks it is expected to
fail. ``FAKEAPI_CURSED_STRICT=1`` promotes secret-ish misses to failures.
"""

import base64
import json
import os
import secrets
import uuid
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fake_api_server.redaction import identity, identity_record
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


def _walk_json(value, found: list[str]) -> None:
    if isinstance(value, str):
        found.append(value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            _walk_json(item, found)
        return
    if isinstance(value, list):
        for item in value:
            _walk_json(item, found)


def _walk_xml(element: ET.Element, found: list[str]) -> None:
    if element.text:
        found.append(element.text)
    if element.tail:
        found.append(element.tail)
    for key, value in element.attrib.items():
        found.append(str(key))
        found.append(str(value))
    for child in list(element):
        _walk_xml(child, found)


def _decoded_json(text: str) -> list[str] | None:
    found: list[str] = []
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            _walk_json(json.loads(line), found)
    except json.JSONDecodeError:
        return None
    return found


def _decoded_xml(text: str) -> list[str] | None:
    found: list[str] = []
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            _walk_xml(ET.fromstring(line), found)
    except ET.ParseError:
        return None
    return found


def _mode_kwargs(mode: str, string_spec: str | None, record_spec: str | None) -> dict:
    """Identity fills whichever hook this mode is not testing."""
    kwargs: dict = {}
    if mode == "string":
        kwargs["record_hook"] = identity_record
        if string_spec:
            kwargs["hook_from_env"] = True
        else:
            kwargs["log_hook"] = identity
    elif mode == "object":
        kwargs["log_hook"] = identity
        if not record_spec:
            kwargs["record_hook"] = identity_record
    else:
        if string_spec:
            kwargs["hook_from_env"] = True
        else:
            kwargs["log_hook"] = identity
        if not record_spec:
            kwargs["record_hook"] = identity_record
    return kwargs


@pytest.mark.skipif(
    os.environ.get("FAKEAPI_CURSED") != "1",
    reason="set FAKEAPI_CURSED=1 to run the redaction corpus; identity hooks are expected to fail",
)
def test_cursed_secrets_do_not_appear_in_logs(make_session, tmp_path, monkeypatch):
    data = _load()
    assembled = [(entry, _assemble(entry)) for entry in data["entries"]]
    string_spec = os.environ.get("FAKEAPI_LOG_HOOK")
    record_spec = os.environ.get("FAKEAPI_LOG_RECORD_HOOK")
    if string_spec:
        monkeypatch.setenv("FAKEAPI_LOG_HOOK", string_spec)
    if record_spec:
        monkeypatch.setenv("FAKEAPI_LOG_RECORD_HOOK", record_spec)

    strict = os.environ.get("FAKEAPI_CURSED_STRICT") == "1"
    report = ["cursed-secrets redaction report"]
    hard: list[str] = []
    soft: list[str] = []
    modes = (("string-hook", "string"), ("object-hook", "object"), ("both", "both"))

    for label, mode in modes:
        grouped = {"definitely": [], "likely": [], "secret-ish": []}
        xml_path = tmp_path / f"cursed-{mode}.xml"
        session = make_session(
            capture="all",
            xml_path=str(xml_path),
            log_stream=__import__("io").StringIO(),
            **_mode_kwargs(mode, string_spec, record_spec),
        )
        _exercise(session, assembled)
        json_values = _decoded_json(session.log.getvalue())
        xml_values = _decoded_xml(xml_path.read_text(encoding="utf-8"))
        if json_values is None or xml_values is None:
            line = (
                "FAIL logs were not parseable "
                f"(json={json_values is not None}, xml={xml_values is not None})"
            )
            hard.append(f"{label}: {line}")
            grouped["definitely"].append(line)
        else:
            for entry, secret in assembled:
                sensitivity = entry["sensitivity"]
                needles = _needles(entry, secret)
                if not needles:
                    grouped[sensitivity].append(
                        f"PASS {entry['id']} (empty value, requests above did not crash)"
                    )
                    continue
                hits = []
                if any(needle in value for needle in needles for value in json_values):
                    hits.append("json")
                if any(needle in value for needle in needles for value in xml_values):
                    hits.append("xml")
                if not hits:
                    grouped[sensitivity].append(f"PASS {entry['id']}")
                    continue
                line = f"FAIL {entry['id']} leaked into {', '.join(hits)}"
                if sensitivity == "secret-ish" and not strict:
                    line += " (soft)"
                    soft.append(f"{label}: {line}")
                else:
                    hard.append(f"{label}: {line}")
                grouped[sensitivity].append(line)

        report.append(f"mode: {label}")
        for name in ("definitely", "likely", "secret-ish"):
            report.append(name + ":")
            report.extend(f"  {line}" for line in grouped[name])

    if soft:
        report.append(
            f"soft failures: {len(soft)} (set FAKEAPI_CURSED_STRICT=1 to fail on these)"
        )
    rendered = "\n".join(report)
    if soft and not hard:
        warnings.warn(rendered, stacklevel=1)
    if hard:
        raise AssertionError(rendered)


def _exercise(session, assembled) -> None:
    response_body = "\n".join(secret for _entry, secret in assembled if secret)
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
