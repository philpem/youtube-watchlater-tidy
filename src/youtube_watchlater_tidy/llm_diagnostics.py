from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

_REDACTED = "<redacted>"
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "apikey",
}
_ACTIVE_LOG: "LlmDiagnosticLog | None" = None
_ACTIVE_LOCK = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_sensitive_header(name: str) -> bool:
    folded = name.casefold()
    if folded in _SENSITIVE_HEADER_NAMES:
        return True
    return any(token in folded for token in ("api-key", "apikey", "token", "secret"))


def _redact(value: Any, secrets: tuple[str, ...], *, in_headers: bool = False) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for child_key, child_value in value.items():
            key = str(child_key)
            if in_headers and _is_sensitive_header(key):
                result[key] = _REDACTED
            else:
                result[key] = _redact(
                    child_value,
                    secrets,
                    in_headers=(key == "headers"),
                )
        return result
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, _REDACTED)
        return redacted
    return value


class LlmDiagnosticLog:
    """Append redacted provider diagnostics as one JSON object per line."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def write(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        secrets: tuple[str, ...] = (),
    ) -> None:
        record = {
            "timestamp": _utc_now(),
            "event": event,
            **payload,
        }
        record = _redact(record, secrets)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()


def new_request_id() -> str:
    return uuid4().hex


def active_diagnostic_log() -> LlmDiagnosticLog | None:
    with _ACTIVE_LOCK:
        return _ACTIVE_LOG


@contextmanager
def use_diagnostic_log(path: str | Path | None) -> Iterator[LlmDiagnosticLog | None]:
    global _ACTIVE_LOG
    if path is None:
        yield None
        return

    current = LlmDiagnosticLog(path)
    with _ACTIVE_LOCK:
        previous = _ACTIVE_LOG
        _ACTIVE_LOG = current
    try:
        yield current
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_LOG = previous
