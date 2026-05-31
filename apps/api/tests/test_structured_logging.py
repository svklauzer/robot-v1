import json
import logging

from core.logging import get_logger, log_event, sanitize_log_value


def test_sanitize_log_value_redacts_telegram_bot_tokens():
    text = "https://api.telegram.org/bot123456:ABC_secret/sendMessage bot123456:ABC_secret"

    sanitized = sanitize_log_value(text)

    assert "ABC_secret" not in sanitized
    assert "api.telegram.org/bot<redacted>" in sanitized
    assert "bot<redacted>" in sanitized


def test_log_event_emits_json_and_redacts_secrets(caplog):
    logger = get_logger("tests.structured_logging")

    with caplog.at_level(logging.WARNING, logger="tests.structured_logging"):
        log_event(
            logger,
            logging.WARNING,
            "telegram_send_error",
            error="HTTP 403 https://api.telegram.org/bot999:SECRET/sendMessage",
            chat_id="1832004802",
        )

    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "telegram_send_error"
    assert payload["chat_id"] == "1832004802"
    assert "SECRET" not in payload["error"]
