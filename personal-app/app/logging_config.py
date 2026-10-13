"""Structured, secret-safe logging helpers for the personal application.

The logging boundary is deliberately conservative: structured fields are
redacted by key, raw payload fields are removed entirely, and exception output
contains only safe error metadata rather than provider response bodies.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from logging import Logger
from typing import Any, TextIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
RAW_PAYLOAD_REDACTED = "[RAW_PAYLOAD_REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?:api[-_]?key|authorization|proxy[-_]?authorization|cookie|set[-_]?cookie|"
    r"password|passwd|secret|private[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token|"
    r"bearer[-_]?token|helius[-_]?key|^token$)",
    re.IGNORECASE,
)
_RAW_PAYLOAD_KEY = re.compile(r"^(?:raw[-_]?payload|payload[-_]?json)$", re.IGNORECASE)
_QUERY_SECRET_KEY = re.compile(
    r"(?:api[-_]?key|access[-_]?token|refresh[-_]?token|auth(?:orization)?|token|secret|password)$",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(\bBearer\s+)[^\s,;]+", re.IGNORECASE)
_KEY_VALUE = re.compile(
    r"(?i)(\b(?:api[-_]?key|access[-_]?token|refresh[-_]?token|authorization|secret|password)\s*[=:]\s*)"
    r"([^\s,;&]+)"
)
_CORRELATION_ID: ContextVar[str | None] = ContextVar("quantumorb_correlation_id", default=None)


@dataclass(frozen=True)
class SafeErrorInfo:
    """The only error fields safe to put into a log or API diagnostic."""

    code: str
    message: str
    retryable: bool = False


class SafeError(Exception):
    """An application error whose public representation contains no raw cause."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        if not code or not message:
            raise ValueError("safe errors require a non-empty code and message")
        self.info = SafeErrorInfo(code=code, message=message, retryable=retryable)
        super().__init__(message)

    @property
    def code(self) -> str:
        return self.info.code

    @property
    def safe_message(self) -> str:
        return self.info.message

    @property
    def retryable(self) -> bool:
        return self.info.retryable

    def as_log_fields(self) -> dict[str, str | bool]:
        return {
            "error_code": self.code,
            "error_message": self.safe_message,
            "retryable": self.retryable,
        }

    @classmethod
    def from_exception(
        cls,
        code: str,
        exception: BaseException,
        *,
        message: str = "operation failed",
        retryable: bool = False,
    ) -> SafeError:
        """Create a safe error without retaining or exposing the original text."""
        del exception
        return cls(code, message, retryable=retryable)


def new_correlation_id() -> str:
    """Return a fresh opaque correlation identifier."""
    return str(uuid.uuid4())


def get_correlation_id() -> str | None:
    return _CORRELATION_ID.get()


@contextmanager
def correlation_id(value: str | None = None) -> Iterator[str]:
    """Set a correlation ID for the current async/task context and restore it."""
    resolved = value or new_correlation_id()
    token = _CORRELATION_ID.set(resolved)
    try:
        yield resolved
    finally:
        _CORRELATION_ID.reset(token)


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    username = parsed.username
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if username is not None:
        netloc = f"{username}:{REDACTED}@{netloc}"
    query = urlencode(
        [
            (key, REDACTED if _QUERY_SECRET_KEY.search(key) else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _redact_string(value: str, secrets: Sequence[str]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    redacted = _redact_url(redacted)
    redacted = _BEARER.sub(rf"\1{REDACTED}", redacted)
    return _KEY_VALUE.sub(rf"\1{REDACTED}", redacted)


def redact(value: Any, *, secrets: Sequence[str] = (), key: str | None = None) -> Any:
    """Recursively redact sensitive values while preserving safe JSON shapes."""
    if key is not None and _RAW_PAYLOAD_KEY.match(key):
        return RAW_PAYLOAD_REDACTED
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item, secrets=secrets, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return _redact_string(value, secrets)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_string(str(value), secrets)


class RedactingJsonFormatter(logging.Formatter):
    """Serialize log records as JSON after applying the redaction policy."""

    def __init__(self, *, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "log_fields", {})
        if not isinstance(fields, Mapping):
            fields = {"details": fields}
        output: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage(), secrets=self._secrets),
            "correlation_id": get_correlation_id(),
        }
        output.update(redact(dict(fields), secrets=self._secrets))
        if record.exc_info:
            output["exception_type"] = (
                record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            )
        return json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def log_event(
    logger: Logger, level: int, event: str, *, fields: Mapping[str, Any] | None = None
) -> None:
    """Emit a structured event without allowing arbitrary raw record extras."""
    logger.log(level, event, extra={"log_fields": dict(fields or {})})


def configure_logging(
    *,
    level: int | str = logging.INFO,
    secrets: Sequence[str] = (),
    logger: Logger | None = None,
    stream: TextIO | None = None,
) -> Logger:
    """Install one JSON handler on the selected logger and return it."""
    target = logger or logging.getLogger()
    target.setLevel(level)
    target.handlers.clear()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter(secrets=secrets))
    target.addHandler(handler)
    target.propagate = False
    return target
