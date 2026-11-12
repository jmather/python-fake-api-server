"""Validate register and record payloads against the shared JSON schema."""

import copy
import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft7Validator


class PayloadError(Exception):
    def __init__(self, message: str, errors: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors


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


def validate_entity(entity: str, instance: Any) -> None:
    """Apply defaults and raise PayloadError when the instance does not match."""
    root = load_schema()
    if isinstance(instance, (dict, list)):
        apply_defaults(instance, root["definitions"][entity], root)

    errors = sorted(
        _validator(entity).iter_errors(instance),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if not errors:
        return

    details = [
        {
            "message": error.message,
            "path": [str(part) for part in error.absolute_path],
            "validator": error.validator,
        }
        for error in errors
    ]
    message = "; ".join(detail["message"] for detail in details)
    raise PayloadError(message, details)


def parse_json_object(raw: bytes) -> Any:
    if not raw:
        raise PayloadError("Request payload is malformed.", [{"message": "Empty body"}])
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadError("Request payload is malformed.", [{"message": str(exc)}]) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PayloadError("Request payload is malformed.", [{"message": str(exc)}]) from exc
