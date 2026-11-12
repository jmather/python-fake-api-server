"""Validate register and record payloads against the shared JSON schema.

Schema failures become the shared 400 body. Messages are derived from the
keyword and the instance, not from a validator library's English text, so the
.NET and Node servers can emit the same bytes of meaning.
"""

import copy
import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft7Validator


class PayloadError(Exception):
    def __init__(self, message: str, errors: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors


def malformed(message: str) -> PayloadError:
    return PayloadError(
        "Request payload is malformed.",
        [{"keyword": "parse", "path": "", "message": message}],
    )


def invalid(errors: list[dict[str, str]]) -> PayloadError:
    ordered = sorted(errors, key=lambda item: (item["path"], item["keyword"], item["message"]))
    return PayloadError("Request payload is invalid.", ordered)


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    raw = files("fake_api_server").joinpath("data/fake-api-schema.json").read_text(encoding="utf-8")
    return json.loads(raw)


def load_text(name: str) -> str:
    return files("fake_api_server").joinpath(f"data/{name}").read_text(encoding="utf-8")


def apply_defaults(instance: Any, schema: dict[str, Any], root: dict[str, Any]) -> None:
    """Fill JSON Schema defaults before validation, as ajv useDefaults does."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        apply_defaults(instance, root["definitions"][name], root)
        return

    schema_type = schema.get("type")
    if schema_type == "object" and isinstance(instance, dict):
        properties = schema.get("properties", {})
        for key, subschema in properties.items():
            if key not in instance and isinstance(subschema, dict) and "default" in subschema:
                instance[key] = copy.deepcopy(subschema["default"])
        for key, subschema in properties.items():
            if key in instance and isinstance(subschema, dict):
                apply_defaults(instance[key], subschema, root)
    elif schema_type == "array" and isinstance(instance, list):
        item_schema = schema.get("items", {})
        if isinstance(item_schema, dict):
            for item in instance:
                apply_defaults(item, item_schema, root)


def _validator(entity: str) -> Draft7Validator:
    root = load_schema()
    schema = {
        "$schema": root["$schema"],
        "definitions": root["definitions"],
        "$ref": f"#/definitions/{entity}",
    }
    return Draft7Validator(schema, format_checker=Draft7Validator.FORMAT_CHECKER)


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _pointer(parts: list[Any]) -> str:
    if not parts:
        return ""
    return "/" + "/".join(_escape(str(part)) for part in parts)


def _child(parent: str, name: str) -> str:
    return parent + "/" + _escape(name)


def _type_name(expected: Any) -> str:
    if isinstance(expected, list):
        return " or ".join(str(item) for item in expected)
    return str(expected)


def _enum_list(values: list[Any]) -> str:
    return ", ".join(json.dumps(item, ensure_ascii=False) for item in values)


def _additional_names(error: Any) -> list[str]:
    schema = error.schema if isinstance(error.schema, dict) else {}
    properties = set(schema.get("properties", {}))
    patterns = [re.compile(pattern) for pattern in schema.get("patternProperties", {})]
    instance = error.instance
    if not isinstance(instance, dict):
        return []
    names = []
    for key in instance:
        if key in properties:
            continue
        if any(pattern.search(key) for pattern in patterns):
            continue
        names.append(str(key))
    return sorted(names)


def _required_name(error: Any) -> str:
    message = str(error.message)
    if message.startswith("'") and "' is a required property" in message:
        return message[1 : message.index("' is a required property")]
    missing = [
        name
        for name in error.validator_value
        if isinstance(error.instance, dict) and name not in error.instance
    ]
    return str(missing[0]) if missing else "unknown"


def _canonical(error: Any) -> list[dict[str, str]]:
    keyword = str(error.validator)
    path = _pointer(list(error.absolute_path))
    schema_path = [str(part) for part in error.schema_path]

    if keyword == "pattern" and "propertyNames" in schema_path:
        name = error.instance if isinstance(error.instance, str) else str(error.instance)
        return [
            {
                "keyword": "propertyNames",
                "path": _child(path, name),
                "message": f"property name '{name}' is not allowed",
            }
        ]

    if keyword == "additionalProperties":
        return [
            {
                "keyword": "additionalProperties",
                "path": path,
                "message": f"must not have additional property '{name}'",
            }
            for name in _additional_names(error)
        ]

    if keyword == "required":
        name = _required_name(error)
        return [
            {
                "keyword": "required",
                "path": path,
                "message": f"must have required property '{name}'",
            }
        ]

    if keyword == "type":
        message = f"must be {_type_name(error.validator_value)}"
    elif keyword == "enum":
        message = f"must be one of: {_enum_list(list(error.validator_value))}"
    elif keyword == "pattern":
        message = f"must match pattern {error.validator_value}"
    elif keyword == "format":
        message = f"must be a valid {error.validator_value}"
    elif keyword == "minimum":
        message = f"must be >= {error.validator_value}"
    elif keyword == "maximum":
        message = f"must be <= {error.validator_value}"
    elif keyword == "minItems":
        message = f"must have at least {error.validator_value} items"
    elif keyword == "maxItems":
        message = f"must have at most {error.validator_value} items"
    elif keyword == "minLength":
        message = f"must be at least {error.validator_value} characters"
    elif keyword == "maxLength":
        message = f"must be at most {error.validator_value} characters"
    else:
        message = str(error.message)

    return [{"keyword": keyword, "path": path, "message": message}]


def validate_entity(entity: str, instance: Any) -> None:
    """Apply defaults and raise PayloadError when the instance does not match."""
    root = load_schema()
    if isinstance(instance, (dict, list)):
        apply_defaults(instance, root["definitions"][entity], root)

    found: list[dict[str, str]] = []
    for error in _validator(entity).iter_errors(instance):
        found.extend(_canonical(error))
    if found:
        raise invalid(found)


def parse_json_object(raw: bytes) -> Any:
    if not raw:
        raise malformed("Request body is empty.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise malformed("Request body is not valid UTF-8.") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise malformed("Request body is not valid JSON.") from exc
