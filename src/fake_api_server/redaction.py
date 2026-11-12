"""Redaction hook seam.

A hook is ``(serialized: str, format: "json" | "xml") -> str``. It runs after a
request record is serialized and before that sink writes the string. The
default hook is identity.

``FAKEAPI_LOG_HOOK`` is either a dotted path (``package.module:function`` or
``package.module.function``) or the name of an entry point in the
``fakeapi.log_hooks`` group.
"""

import importlib
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Literal

LogFormat = Literal["json", "xml"]
LogHook = Callable[[str, LogFormat], str]

ENTRY_POINT_GROUP = "fakeapi.log_hooks"


def identity(serialized: str, format: LogFormat) -> str:
    """Return the serialized record unchanged."""
    return serialized


def resolve_hook(spec: str | None) -> LogHook:
    """Load a hook from a dotted path or an entry point name."""
    if spec is None or spec.strip() == "":
        return identity
    target = spec.strip()
    if ":" in target or "." in target:
        return _import_dotted(target)
    return _load_entry_point(target)


def _import_dotted(target: str) -> LogHook:
    if ":" in target:
        module_name, attr = target.split(":", 1)
    else:
        module_name, attr = target.rsplit(".", 1)
    module = importlib.import_module(module_name)
    hook = getattr(module, attr)
    if not callable(hook):
        raise TypeError(f"FAKEAPI_LOG_HOOK {target!r} is not callable")
    return hook


def _load_entry_point(name: str) -> LogHook:
    matches = [item for item in entry_points(group=ENTRY_POINT_GROUP) if item.name == name]
    if not matches:
        raise LookupError(
            f"FAKEAPI_LOG_HOOK {name!r} is not an entry point in group {ENTRY_POINT_GROUP}"
        )
    hook = matches[0].load()
    if not callable(hook):
        raise TypeError(f"FAKEAPI_LOG_HOOK entry point {name!r} is not callable")
    return hook
