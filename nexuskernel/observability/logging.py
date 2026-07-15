"""Structured logging setup with conservative recursive redaction."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from loguru import Record


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|token|secret|password|credential|prompt|"
    r"raw[_-]?(?:request|response)|model[_-]?response|connection[_-]?url)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")  # security-pattern
_CREDENTIAL_URL = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@",
    re.IGNORECASE,
)


def _redact_text(value: str) -> str:
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _OPENAI_KEY.sub("[REDACTED]", value)
    return _CREDENTIAL_URL.sub(r"\g<scheme>[REDACTED]@", value)


def redact_sensitive(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_sensitive(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _patch_record(record: Record) -> None:
    record["message"] = _redact_text(str(record["message"]))
    record["extra"] = redact_sensitive(record["extra"])


def configure_logging(
    *, level: str = "INFO", serialize: bool = True, enqueue: bool = True
) -> int:
    """Configure the process-wide Loguru sink and return its handler id."""

    logger.remove()
    logger.configure(patcher=_patch_record)
    return logger.add(sys.stdout, level=level, serialize=serialize, enqueue=enqueue)
