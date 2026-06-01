from __future__ import annotations

import re

import httpx

TELEGRAM_TOKEN_RE = re.compile(r"(api\.telegram\.org/bot)[^/\s'\")]+")
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}


def sanitize_telegram_error(error: str | None) -> str | None:
    if error is None:
        return None
    return TELEGRAM_TOKEN_RE.sub(r"\1<redacted>", str(error))


def is_retryable_telegram_error(exc: Exception | str | None) -> bool:
    if exc is None:
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in NON_RETRYABLE_STATUS_CODES:
            return False
        if status_code == 429 or (status_code is not None and status_code >= 500):
            return True

    text = str(exc)
    lowered = text.lower()
    non_retryable_markers = [
        "403 forbidden",
        "401 unauthorized",
        "400 bad request",
        "404 not found",
        "bot was blocked",
        "bot can't initiate conversation",
        "chat not found",
        "user is deactivated",
        "not enough rights",
    ]
    if any(marker in lowered for marker in non_retryable_markers):
        return False

    return True
