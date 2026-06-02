from __future__ import annotations

import json
import logging
import re
from typing import Any

TELEGRAM_BOT_TOKEN_RE = re.compile(r"(api\.telegram\.org/bot)[^/\s'\")]+")
GENERIC_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
OWNER_TOKEN_RE = re.compile(r"(x-owner-token['\"=:\s]+)[^,'\"\s}]+", re.IGNORECASE)
SECRET_ASSIGNMENT_RE = re.compile(r"((?:api[_-]?key|secret|token|password)['\"=:\s]+)[^,'\"\s}]+", re.IGNORECASE)


def _configured_secret_values() -> list[str]:
    try:
        from core.config import settings
    except Exception:
        return []

    candidates = [
        getattr(settings, "TELEGRAM_BOT_TOKEN", ""),
        getattr(settings, "HTX_API_KEY", ""),
        getattr(settings, "HTX_API_SECRET", ""),
        getattr(settings, "OWNER_API_TOKEN", ""),
        getattr(settings, "JWT_SECRET", ""),
        getattr(settings, "OWNER_PASSWORD", ""),
        getattr(settings, "POSTGRES_PASSWORD", ""),
    ]
    return [str(value) for value in candidates if value and len(str(value)) >= 4]


def sanitize_log_value(value: Any) -> Any:
    """Redact secrets from values before they reach process logs."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): sanitize_log_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_log_value(item) for item in value]

    text = str(value)
    text = TELEGRAM_BOT_TOKEN_RE.sub(r"\1<redacted>", text)
    text = GENERIC_BOT_TOKEN_RE.sub("bot<redacted>", text)
    text = OWNER_TOKEN_RE.sub(r"\1<redacted>", text)
    text = SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", text)
    for secret in _configured_secret_values():
        text = text.replace(secret, "<redacted>")
    return text


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit a compact JSON log line for background workers and integrations."""
    payload = {
        "event": event,
        **{key: sanitize_log_value(value) for key, value in fields.items()},
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
