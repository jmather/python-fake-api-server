"""Reference hooks named by the conformance suite.

``prefix`` prepends ``[json]`` or ``[xml]`` to the serialized record, including
its trailing newline. ``stamp`` appends `` [stamped]`` to ``msg`` and leaves
every other field alone. Both are installed through the normal env vars:

``FAKEAPI_LOG_HOOK=fake_api_server.conformance_hooks:prefix``
``FAKEAPI_LOG_RECORD_HOOK=fake_api_server.conformance_hooks:stamp``
"""

from typing import Any


def prefix(serialized: str, format: str) -> str:
    return f"[{format}]{serialized}"


def stamp(record: dict[str, Any]) -> dict[str, Any]:
    updated = dict(record)
    updated["msg"] = f"{record.get('msg', '')} [stamped]"
    return updated
