"""Run the language-agnostic conformance suite against a live server.

Launch with ``python -m fake_api_server.conformance_runner`` from an environment
that can import this package. The process under test is
``python -m fake_api_server --port <ephemeral>``. See ``conformance/README.md``.
"""

import base64
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import quote_plus, urlencode

import httpx

SERVER_NAME = "python"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
VAR_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
SEGMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_-]+)(?:@(?P<attr>[A-Za-z0-9_-]+)=(?P<value>[^#]+))?(?:#(?P<index>\d+))?$"
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def conformance_root() -> Path:
    return repo_root() / "conformance"


def main() -> int:
    failures = run_suite(conformance_root())
    if failures:
        sys.stderr.write("\n".join(failures) + "\n")
        return 1
    return 0


def run_suite(root: Path) -> list[str]:
    errors = verify_manifest(root)
    if errors:
        return errors
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    fixtures = sorted((root / "fixtures").glob("*.json"))
    failures: list[str] = []
    for path in fixtures:
        scenario = json.loads(path.read_text(encoding="utf-8"))
        failures.extend(run_scenario(root, scenario))
    if not failures:
        names = ", ".join(path.name for path in fixtures)
        print(f"conformance v{version}: {len(fixtures)} scenarios passed ({names})")
    return failures


def verify_manifest(root: Path) -> list[str]:
    manifest_path = root / "SHA256SUMS"
    if not manifest_path.is_file():
        return ["conformance/SHA256SUMS is missing"]
    listed: dict[str, str] = {}
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip() == "":
            continue
        digest, separator, rel = line.partition("  ")
        if separator != "  " or len(digest) != 64 or rel == "" or rel.startswith("./"):
            return [f"SHA256SUMS:{line_number} is not 'digest<two spaces>path'"]
        listed[rel] = digest
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in filenames:
            if name == "SHA256SUMS":
                continue
            rel = Path(dirpath, name).relative_to(root).as_posix()
            found.append(rel)
    errors: list[str] = []
    missing = sorted(set(listed) - set(found))
    extra = sorted(set(found) - set(listed))
    if missing:
        errors.append("SHA256SUMS lists missing files: " + ", ".join(missing))
    if extra:
        errors.append("files missing from SHA256SUMS: " + ", ".join(extra))
    for rel, digest in sorted(listed.items()):
        actual = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        if actual != digest:
            errors.append(f"checksum mismatch for {rel}")
    version = (root / "VERSION").read_bytes()
    if version != b"1\n":
        errors.append("conformance/VERSION must be the bytes 1\\n")
    return errors


def run_scenario(root: Path, scenario: dict) -> list[str]:
    scenario_id = scenario.get("id", "<missing id>")
    port = _free_port()
    xml_path = root.parent / ".pytest_cache" / f"conformance-{scenario_id}-{port}.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    if xml_path.exists():
        xml_path.unlink()
    env = _server_env(scenario, xml_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "fake_api_server", "--port", str(port)],
        cwd=repo_root(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout: Queue = Queue()
    stderr: Queue = Queue()
    threading.Thread(target=_pump, args=(proc.stdout, stdout), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, stderr), daemon=True).start()
    failures: list[str] = []
    try:
        if not _wait_until_listening(proc, port, stderr):
            return [f"{scenario_id}: server did not listen: {_drain(stderr)}"]
        variables = {"server": SERVER_NAME}
        http_index = 0
        prefix = (scenario.get("hooks") or {}).get("string") == "prefix"
        with httpx.Client(timeout=10, trust_env=False) as client:
            for step in scenario.get("steps", []):
                kind = step.get("kind")
                if kind is None and "request" in step:
                    kind = "http"
                step_id = step.get("id", kind or "?")
                label = f"{scenario_id}/{step_id}"
                if kind == "sleep":
                    time.sleep(int(step["ms"]) / 1000)
                    continue
                if kind != "http":
                    failures.append(f"{label}: unknown step kind {kind!r}")
                    break
                error = _http_step(
                    client,
                    root,
                    step,
                    port,
                    variables,
                    stdout,
                    prefix,
                    xml_path if scenario.get("xml_log") else None,
                    http_index,
                    label,
                )
                http_index += 1
                if error:
                    failures.append(error)
                    break
    finally:
        _stop(proc)
    return failures


def _server_env(scenario: dict, xml_path: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("FAKEAPI_")}
    env.update({str(key): str(value) for key, value in (scenario.get("env") or {}).items()})
    env["PYTHONUNBUFFERED"] = "1"
    if scenario.get("xml_log"):
        env["FAKEAPI_LOG_XML_PATH"] = str(xml_path)
    hooks = scenario.get("hooks") or {}
    string_hook = hooks.get("string", "identity")
    object_hook = hooks.get("object", "identity")
    if string_hook == "prefix":
        env["FAKEAPI_LOG_HOOK"] = "fake_api_server.conformance_hooks:prefix"
    elif string_hook != "identity":
        raise ValueError(f"unknown string hook {string_hook!r}")
    if object_hook == "stamp":
        env["FAKEAPI_LOG_RECORD_HOOK"] = "fake_api_server.conformance_hooks:stamp"
    elif object_hook != "identity":
        raise ValueError(f"unknown object hook {object_hook!r}")
    return env


def _http_step(
    client: httpx.Client,
    root: Path,
    step: dict,
    port: int,
    variables: dict[str, str],
    stdout: Queue,
    prefix: bool,
    xml_path: Path | None,
    http_index: int,
    label: str,
) -> str | None:
    request = step["request"]
    path = _interp(request["path"], variables)
    query = ""
    if "query_raw" in request:
        query = _interp(str(request["query_raw"]), variables)
    elif request.get("query"):
        query = _encode_query(request["query"], variables)
    url = f"http://127.0.0.1:{port}{path}"
    if query:
        url = url + "?" + query
    headers = {
        name: _interp(str(value), variables)
        for name, value in (request.get("headers") or {}).items()
    }
    body = _encode_body(request.get("body"), variables)
    started = time.perf_counter()
    try:
        response = client.request(request["method"], url, headers=headers, content=body)
    except httpx.HTTPError as exc:
        return f"{label}: request failed: {exc}"
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        raw_line, payload = _next_log_line(stdout, prefix)
    except TimeoutError:
        return f"{label}: no JSON log line within 5s"
    except ValueError as exc:
        return f"{label}: {exc}"
    problems: list[str] = []
    expect = step.get("expect") or {}
    if response.status_code != expect.get("status"):
        problems.append(
            f"status {response.status_code} != {expect.get('status')}: {response.text[:300]}"
        )
    minimum = expect.get("elapsed_ms_gte")
    if minimum is not None and elapsed_ms < float(minimum):
        problems.append(f"elapsed {elapsed_ms:.1f}ms < {minimum}ms")
    header_map = {key.lower(): value for key, value in response.headers.items()}
    for check in expect.get("headers") or []:
        problems.extend(_check_header(check, header_map, url, variables))
    if "body" in expect:
        problems.extend(_check_body(expect["body"], response, root, variables))
    log = step.get("log")
    if log:
        problems.extend(
            _check_log(
                log,
                raw_line,
                payload,
                header_map,
                variables,
                xml_path,
                http_index,
                prefix,
            )
        )
    if problems:
        return label + ":\n  " + "\n  ".join(problems)
    return None


def _check_header(
    check: dict,
    headers: dict[str, str],
    url: str,
    variables: dict[str, str],
) -> list[str]:
    name = check["name"].lower()
    match = check["match"]
    present = name in headers
    value = headers.get(name, "")
    if match == "absent":
        return [] if not present else [f"header {name} was present ({value!r})"]
    if match == "present":
        return [] if present else [f"header {name} was absent"]
    if not present:
        return [f"header {name} was absent"]
    if match == "exact":
        expected = _interp(check["value"], variables)
        return [] if value == expected else [f"header {name} {value!r} != {expected!r}"]
    if match == "prefix":
        expected = _interp(check["value"], variables)
        if value.startswith(expected):
            return []
        return [f"header {name} {value!r} does not start with {expected!r}"]
    if match == "contains":
        expected = _interp(check["value"], variables)
        if expected in value:
            return []
        return [f"header {name} {value!r} does not contain {expected!r}"]
    if match == "request_url":
        return [] if value == url else [f"header {name} {value!r} != request URL {url!r}"]
    if match == "uuid":
        if UUID_RE.fullmatch(value):
            return []
        return [f"header {name} {value!r} is not a lowercase uuid"]
    if match == "regex":
        if re.fullmatch(check["value"], value):
            return []
        return [f"header {name} {value!r} does not fullmatch {check['value']!r}"]
    return [f"unknown header match {match!r}"]


def _check_body(
    spec: dict,
    response: httpx.Response,
    root: Path,
    variables: dict[str, str],
) -> list[str]:
    match = spec["match"]
    if match == "empty":
        if response.content == b"":
            return []
        return [f"body was not empty: {response.content[:120]!r}"]
    if match == "exact":
        expected = _interp(spec["value"], variables)
        return [] if response.text == expected else [f"body {response.text!r} != {expected!r}"]
    if match == "regex":
        if re.fullmatch(spec["pattern"], response.text):
            return []
        return [f"body does not fullmatch {spec['pattern']!r}: {response.text[:120]!r}"]
    if match == "file":
        expected = (root / spec["path"]).read_bytes()
        if response.content == expected:
            return []
        return [
            f"body bytes differ from {spec['path']} ({len(response.content)} vs {len(expected)})"
        ]
    if match == "json":
        try:
            actual = response.json()
        except json.JSONDecodeError as exc:
            return [f"body is not JSON: {exc}"]
        return _compare_json(spec["value"], actual, variables, "$")
    return [f"unknown body match {match!r}"]


def _check_log(
    spec: dict,
    raw_line: str,
    payload: str,
    headers: dict[str, str],
    variables: dict[str, str],
    xml_path: Path | None,
    http_index: int,
    prefix: bool,
) -> list[str]:
    problems: list[str] = []
    if "raw_prefix" in spec and not raw_line.startswith(_interp(spec["raw_prefix"], variables)):
        problems.append(f"log line does not start with {spec['raw_prefix']!r}")
    for needle in spec.get("raw_excludes") or []:
        text = _interp(str(needle), variables)
        if text and text in raw_line:
            problems.append(f"log line contains excluded text {text!r}")
    if "json" in spec:
        try:
            actual = json.loads(payload)
        except json.JSONDecodeError as exc:
            return problems + [f"log line is not JSON: {exc}: {payload[:180]!r}"]
        value = spec["json"].get("value")
        if value is not None:
            problems.extend(_compare_json(value, actual, variables, "$.log", headers))
    if "xml" in spec:
        if xml_path is None:
            return problems + ["xml assertion requires scenario xml_log: true"]
        problems.extend(_check_xml(spec["xml"], xml_path, http_index, prefix, variables))
    return problems


def _check_xml(
    spec: dict,
    path: Path,
    index: int,
    prefix: bool,
    variables: dict[str, str],
) -> list[str]:
    if not path.is_file():
        return ["XML sink file was not written"]
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if index >= len(lines):
        return [f"XML sink has {len(lines)} records, wanted index {index}"]
    line = lines[index]
    if prefix:
        if not line.startswith("[xml]"):
            return [f"XML record does not start with [xml]: {line[:80]!r}"]
        line = line[len("[xml]") :]
    try:
        root = ET.fromstring(line)
    except ET.ParseError as exc:
        return [f"XML record does not parse: {exc}"]
    problems = []
    for pointer, expected in (spec.get("text") or {}).items():
        try:
            actual = _xml_text(root, pointer)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        wanted = _interp(str(expected), variables)
        if actual != wanted:
            problems.append(f"xml {pointer} {actual!r} != {wanted!r}")
    return problems


def _xml_text(root: ET.Element, pointer: str) -> str:
    nodes = [root]
    for segment in pointer.split("/"):
        matched = SEGMENT_RE.fullmatch(segment)
        if matched is None:
            raise ValueError(f"bad xml path segment {segment!r}")
        name = matched.group("name")
        attr = matched.group("attr")
        attr_value = matched.group("value")
        index = matched.group("index")
        found: list[ET.Element] = []
        for node in nodes:
            for child in list(node):
                if child.tag != name:
                    continue
                if attr is not None and child.get(attr) != attr_value:
                    continue
                found.append(child)
        if index is not None:
            chosen = int(index)
            if chosen >= len(found):
                raise ValueError(f"xml path {pointer} has no index {chosen}")
            found = [found[chosen]]
        nodes = found
    if len(nodes) != 1:
        raise ValueError(f"xml path {pointer} matched {len(nodes)} elements")
    return nodes[0].text or ""


def _compare_json(
    expected,
    actual,
    variables: dict[str, str],
    path: str,
    headers: dict[str, str] | None = None,
) -> list[str]:
    if isinstance(expected, dict) and isinstance(expected.get("match"), str):
        return _compare_matcher(expected, actual, variables, path, headers)
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path} expected object, got {actual!r}"]
        problems = []
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            if missing:
                problems.append(f"{path} missing keys {missing}")
            if extra:
                problems.append(f"{path} unexpected keys {extra}")
        for key, item in expected.items():
            if key in actual:
                problems.extend(
                    _compare_json(item, actual[key], variables, f"{path}.{key}", headers)
                )
        return problems
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [f"{path} expected list {expected!r}, got {actual!r}"]
        problems = []
        for index, item in enumerate(expected):
            problems.extend(
                _compare_json(item, actual[index], variables, f"{path}[{index}]", headers)
            )
        return problems
    if isinstance(expected, str):
        expected = _interp(expected, variables)
    if actual != expected:
        return [f"{path} {actual!r} != {expected!r}"]
    return []


def _compare_matcher(
    spec: dict,
    actual,
    variables: dict[str, str],
    path: str,
    headers: dict[str, str] | None,
) -> list[str]:
    match = spec["match"]
    if match == "object":
        if not isinstance(actual, dict):
            return [f"{path} expected object, got {actual!r}"]
        problems = []
        for key in spec.get("absent") or []:
            if key in actual:
                problems.append(f"{path}.{key} was present")
        for key, item in (spec.get("keys") or {}).items():
            if key not in actual:
                problems.append(f"{path} missing key {key!r}")
                continue
            problems.extend(_compare_json(item, actual[key], variables, f"{path}.{key}", headers))
        return problems
    if match == "uuid":
        if not isinstance(actual, str) or UUID_RE.fullmatch(actual) is None:
            return [f"{path} {actual!r} is not a lowercase uuid"]
    elif match == "non_empty":
        if not isinstance(actual, str) or actual == "":
            return [f"{path} {actual!r} is not a non-empty string"]
    elif match == "timestamp":
        if not isinstance(actual, str) or TIMESTAMP_RE.fullmatch(actual) is None:
            return [f"{path} {actual!r} is not an RFC3339 millisecond timestamp"]
    elif match == "number":
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return [f"{path} {actual!r} is not a number"]
        if "minimum" in spec and actual < spec["minimum"]:
            return [f"{path} {actual} < {spec['minimum']}"]
        if "maximum" in spec and actual > spec["maximum"]:
            return [f"{path} {actual} > {spec['maximum']}"]
    elif match == "basic_token":
        user = variables[spec["username"]]
        password = variables[spec["password"]]
        expected = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        if actual != expected:
            return [f"{path} is not base64(username:password)"]
    elif match == "var":
        expected = variables[spec["name"]]
        if actual != expected:
            return [f"{path} {actual!r} != captured {spec['name']}"]
    elif match == "regex":
        if not isinstance(actual, str) or re.fullmatch(spec["pattern"], actual) is None:
            return [f"{path} {actual!r} does not fullmatch {spec['pattern']!r}"]
    else:
        return [f"{path} unknown matcher {match!r}"]
    header_name = spec.get("equals_header")
    if header_name:
        if headers is None or headers.get(header_name.lower()) != actual:
            found = None if headers is None else headers.get(header_name.lower())
            return [f"{path} {actual!r} != response header {header_name} {found!r}"]
    if "capture" in spec:
        if not isinstance(actual, str):
            return [f"{path} cannot capture non-string {actual!r}"]
        variables[spec["capture"]] = actual
    return []


def _encode_body(body, variables: dict[str, str]) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, str):
        return _interp(body, variables).encode()
    if isinstance(body, dict) and "repeat" in body:
        return (str(body["repeat"]) * int(body["count"])).encode()
    if isinstance(body, dict) and "base64" in body:
        return base64.b64decode(body["base64"])
    raise ValueError(f"unsupported request body {body!r}")


def _encode_query(query: dict, variables: dict[str, str]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in query.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            pairs.append((str(key), _interp(str(item), variables)))
    return urlencode(pairs, doseq=True, quote_via=quote_plus)


def _interp(text: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in variables:
            raise KeyError(f"unknown capture {name}")
        return variables[name]

    return VAR_RE.sub(replace, text)


def _next_log_line(stdout: Queue, prefix: bool) -> tuple[str, str]:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            line = stdout.get(timeout=0.1)
        except Empty:
            continue
        if line is None:
            raise ValueError("server stdout closed")
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
        stripped = text.rstrip("\r\n")
        if prefix:
            if stripped.startswith("[json]"):
                return text, stripped[len("[json]") :]
            if stripped.startswith("{"):
                raise ValueError("JSON log line is missing the [json] prefix")
            continue
        if stripped.startswith("{"):
            return text, stripped
    raise TimeoutError


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_listening(proc: subprocess.Popen, port: int, stderr: Queue) -> bool:
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _pump(pipe, queue: Queue) -> None:
    for line in iter(pipe.readline, b""):
        queue.put(line)
    queue.put(None)


def _drain(stderr: Queue) -> str:
    chunks = []
    while True:
        try:
            line = stderr.get_nowait()
        except Empty:
            break
        if line is None:
            break
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
        chunks.append(text)
    return "".join(chunks)[-2000:]


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
