"""Request logging: JSON lines on stdout, optional XML file, pre-write hook.

Both sinks go through the standard library logging module. The app runs the
object hook first. A Formatter then serializes that record. The Handler calls
the string hook with ``(serialized, "json"|"xml")`` and writes whatever it
returns.
"""

import json
import logging
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from typing import IO, Any

from fake_api_server.redaction import LogFormat, identity

LogHook = Callable[[str, LogFormat], str]

BODY_LIMIT = 64 * 1024
TRUNCATED = "…[truncated]"

# XML 1.0 legal character ranges, excluding the controls we escape ourselves.
def _xml_legal(code: int) -> bool:
    if code in (0x9, 0xA, 0xD, 0xFFFD):
        return True
    return 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF


def _xml_safe(text: str) -> str:
    return "".join(char if _xml_legal(ord(char)) else "\ufffd" for char in text)


def rfc3339_millis(moment: datetime | None = None) -> str:
    current = moment or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    millis = current.microsecond // 1000
    return current.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def cap_text(raw: bytes) -> str:
    """Decode a raw body and cap the logged form at 64 KiB."""
    if len(raw) <= BODY_LIMIT:
        return raw.decode("utf-8", errors="replace")
    clipped = raw[:BODY_LIMIT].decode("utf-8", errors="ignore")
    return clipped + TRUNCATED


def level_for_status(status: int) -> tuple[int, str]:
    if status >= 500:
        return logging.ERROR, "error"
    if status >= 400:
        return logging.WARNING, "warn"
    return logging.INFO, "info"


class JsonLineFormatter(logging.Formatter):
    """Serialize the request record as one JSON object, plus a trailing newline."""

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "fakeapi_record", None)
        if not isinstance(payload, dict):
            return super().format(record) + "\n"
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return line + "\n"


class XmlFormatter(logging.Formatter):
    """Serialize the request record as one compact <request-log> element."""

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "fakeapi_record", None)
        if not isinstance(payload, dict):
            return super().format(record) + "\n"
        return record_to_xml(payload) + "\n"


def _append_text(parent: ET.Element, tag: str, value: Any) -> None:
    element = ET.SubElement(parent, tag)
    if value is None:
        return
    element.text = _xml_safe(str(value))


def _append_headers(parent: ET.Element, headers: dict[str, Any]) -> None:
    container = ET.SubElement(parent, "headers")
    for name, value in headers.items():
        header = ET.SubElement(container, "header", {"name": str(name)})
        header.text = _xml_safe("" if value is None else str(value))


def _append_capture(parent: ET.Element, tag: str, capture: dict[str, Any]) -> None:
    container = ET.SubElement(parent, tag)
    _append_headers(container, capture.get("headers") or {})
    content_type = ET.SubElement(container, "content_type")
    content_type.text = _xml_safe(str(capture.get("content_type", "")))
    if "body" in capture:
        body = ET.SubElement(container, "body")
        body.text = _xml_safe(str(capture.get("body", "")))


def record_to_xml(payload: dict[str, Any]) -> str:
    root = ET.Element("request-log")
    for tag in ("timestamp", "level", "msg", "server", "request_id", "method", "path"):
        _append_text(root, tag, payload.get(tag))

    query = ET.SubElement(root, "query")
    for name, value in (payload.get("query") or {}).items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            param = ET.SubElement(query, "param", {"name": str(name)})
            param.text = _xml_safe("" if item is None else str(item))

    _append_text(root, "status", payload.get("status"))
    _append_text(root, "duration_ms", payload.get("duration_ms"))
    _append_text(root, "action", payload.get("action"))
    _append_text(root, "user", payload.get("user"))

    if "request" in payload:
        _append_capture(root, "request", payload["request"])
    if "response" in payload:
        _append_capture(root, "response", payload["response"])

    xml = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    # Keep the sink newline-delimited even when a body contains line breaks.
    # Character references are restored by an XML parser.
    return (
        xml.replace("\r\n", "&#13;&#10;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )


class _HookWriter:
    def __init__(self, hook: LogHook, format_name: LogFormat) -> None:
        self.hook = hook
        self.format_name = format_name

    def render(self, record: logging.LogRecord, formatter: logging.Formatter) -> str:
        serialized = formatter.format(record)
        return self.hook(serialized, self.format_name)


class RedactingStreamHandler(logging.StreamHandler):
    """Write hook(json_line, "json") to a stream. The return value is written as-is."""

    def __init__(self, stream: IO[str], hook: LogHook) -> None:
        super().__init__(stream)
        self._writer = _HookWriter(hook, "json")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            output = self._writer.render(record, self.formatter or JsonLineFormatter())
        except Exception:
            self.handleError(record)
            return
        self.acquire()
        try:
            self.stream.write(output)
            self.flush()
        except Exception:
            self.handleError(record)
        finally:
            self.release()


class RedactingFileHandler(logging.FileHandler):
    """Append hook(xml, "xml") to the XML sink file."""

    def __init__(self, filename: str, hook: LogHook) -> None:
        super().__init__(filename, mode="a", encoding="utf-8")
        self._writer = _HookWriter(hook, "xml")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            output = self._writer.render(record, self.formatter or XmlFormatter())
        except Exception:
            self.handleError(record)
            return
        self.acquire()
        try:
            # uvicorn's dictConfig calls logging.shutdown(), which closes every
            # handler created so far and sets this stream to None. Reopen it.
            if self.stream is None:
                self.stream = self._open()
                self._closed = False
            self.stream.write(output)
            self.flush()
        except Exception:
            self.handleError(record)
        finally:
            self.release()


def configure_request_logger(
    *,
    hook: LogHook | None = None,
    stream: IO[str],
    xml_path: str | None,
) -> logging.Logger:
    active_hook = hook or identity
    logger = logging.Logger(f"fakeapi.request.{uuid.uuid4().hex}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    json_handler = RedactingStreamHandler(stream, active_hook)
    json_handler.setFormatter(JsonLineFormatter())
    logger.addHandler(json_handler)

    if xml_path:
        xml_handler = RedactingFileHandler(xml_path, active_hook)
        xml_handler.setFormatter(XmlFormatter())
        logger.addHandler(xml_handler)

    return logger


def build_record(
    *,
    request_id: str,
    method: str,
    path: str,
    query: dict[str, Any],
    status: int,
    duration_ms: float,
    action: str | None,
    user: str | None,
    capture: str,
    request_headers: dict[str, str],
    request_content_type: str,
    request_body: bytes,
    response_headers: dict[str, str],
    response_content_type: str,
    response_body: bytes,
) -> dict[str, Any]:
    level_name = level_for_status(status)[1]
    payload: dict[str, Any] = {
        "timestamp": rfc3339_millis(),
        "level": level_name,
        "msg": "request completed",
        "server": "python",
        "request_id": request_id,
        "method": method.upper(),
        "path": path,
        "query": query,
        "status": status,
        "duration_ms": round(duration_ms, 3),
        "action": action,
        "user": user,
    }
    if capture in {"headers", "all"}:
        request_capture: dict[str, Any] = {
            "headers": request_headers,
            "content_type": request_content_type,
        }
        response_capture: dict[str, Any] = {
            "headers": response_headers,
            "content_type": response_content_type,
        }
        if capture == "all":
            request_capture["body"] = cap_text(request_body)
            response_capture["body"] = cap_text(response_body)
        payload["request"] = request_capture
        payload["response"] = response_capture
    return payload


def emit_record(logger: logging.Logger, payload: dict[str, Any]) -> None:
    level = level_for_status(int(payload["status"]))[0]
    logger.log(level, "request completed", extra={"fakeapi_record": payload})
