"""Redaction hooks.

String hook: ``(serialized: str, format: "json" | "xml") -> str``. It runs after
a request record is serialized and before that sink writes the string.

Object hook: ``(record: dict) -> dict``. It runs once, on the record structure,
before either sink serializes. Order for each request: object hook, serialize,
string hook.

Both default to identity. ``FAKEAPI_LOG_HOOK`` and ``FAKEAPI_LOG_RECORD_HOOK``
are either a dotted path (``package.module:function`` or
``package.module.function``) or the name of an entry point. String hooks use
the ``fakeapi.log_hooks`` group. Object hooks use ``fakeapi.log_record_hooks``.
"""

import copy
import importlib
import logging
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any, Literal

LogFormat = Literal["json", "xml"]
LogHook = Callable[[str, LogFormat], str]
RecordHook = Callable[[dict[str, Any]], dict[str, Any]]

ENTRY_POINT_GROUP = "fakeapi.log_hooks"
RECORD_ENTRY_POINT_GROUP = "fakeapi.log_record_hooks"


def identity(serialized: str, format: LogFormat) -> str:
    """Return the serialized record unchanged."""
    return serialized


def identity_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return the record unchanged."""
    return record


def resolve_hook(spec: str | None) -> LogHook:
    """Load a string hook from a dotted path or an entry point name."""
    return _resolve(spec, group=ENTRY_POINT_GROUP, default=identity, env_name="FAKEAPI_LOG_HOOK")


def resolve_record_hook(spec: str | None) -> RecordHook:
    """Load an object hook from a dotted path or an entry point name."""
    return _resolve(
        spec,
        group=RECORD_ENTRY_POINT_GROUP,
        default=identity_record,
        env_name="FAKEAPI_LOG_RECORD_HOOK",
    )


def apply_record_hook(payload: dict[str, Any], hook: RecordHook) -> dict[str, Any] | None:
    """Return the object hook's dict, or None when the hook cannot be used.

    The hook receives a deep copy. Exceptions and non-dict returns skip both
    sinks and do not fail the HTTP response.
    """
    try:
        updated = hook(copy.deepcopy(payload))
    except Exception:
        logging.getLogger("fakeapi.redaction").debug("record hook failed", exc_info=True)
        return None
    if not isinstance(updated, dict):
        return None
    return updated


def _resolve(spec: str | None, *, group: str, default: Callable, env_name: str) -> Callable:
    if spec is None or spec.strip() == "":
        return default
    target = spec.strip()
    if ":" in target or "." in target:
        return _import_dotted(target, env_name=env_name)
    return _load_entry_point(target, group=group, env_name=env_name)


def _import_dotted(target: str, *, env_name: str) -> Callable:
    if ":" in target:
        module_name, attr = target.split(":", 1)
    else:
        module_name, attr = target.rsplit(".", 1)
    module = importlib.import_module(module_name)
    hook = getattr(module, attr)
    if not callable(hook):
        raise TypeError(f"{env_name} {target!r} is not callable")
    return hook


def _load_entry_point(name: str, *, group: str, env_name: str) -> Callable:
    matches = [item for item in entry_points(group=group) if item.name == name]
    if not matches:
        raise LookupError(f"{env_name} {name!r} is not an entry point in group {group}")
    hook = matches[0].load()
    if not callable(hook):
        raise TypeError(f"{env_name} entry point {name!r} is not callable")
    return hook
