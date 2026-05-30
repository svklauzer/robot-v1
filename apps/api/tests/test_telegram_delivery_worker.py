from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from services.telegram_delivery_log import TelegramDeliveryLog
from services.telegram_delivery_worker import TelegramDeliveryWorker


class FlakySender(TelegramDeliveryWorker):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def _send_telegram_http(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("telegram timeout")


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


import pytest


@pytest.mark.anyio
async def test_delivery_worker_retries_retryable_delivery_until_sent():
    db = make_session()
    log = TelegramDeliveryLog()
    delivery = log.queue(
        db,
        chat_id="123",
        text_value="VIP signal",
        message_type="vip_full_signal",
        max_attempts=2,
    )
    db.commit()

    worker = FlakySender()

    first = await worker.process_once(db)
    db.commit()
    db.refresh(delivery)

    assert first["processed"] == 1
    assert first["failed_retryable"] == 1
    assert delivery.status == "failed_retryable"
    assert delivery.attempts == 1
    assert delivery.next_retry_at is not None

    delivery.next_retry_at = datetime.now(timezone.utc)
    db.commit()

    second = await worker.process_once(db)
    db.commit()
    db.refresh(delivery)

    assert second["processed"] == 1
    assert second["sent"] == 1
    assert delivery.status == "sent"
    assert delivery.sent_at is not None


def test_summary_counts_retryable_and_final_failures_as_failed():
    db = make_session()
    log = TelegramDeliveryLog()
    retryable = log.queue(db, "1", "retry me", "vip_full_signal", max_attempts=2)
    final = log.queue(db, "2", "final", "owner_alert", max_attempts=1)
    sent = log.queue(db, "3", "sent", "free_teaser", max_attempts=1)

    retryable.status = "failed_retryable"
    retryable.attempts = 1
    final.status = "failed_final"
    final.attempts = 1
    final.error = "HTTPStatusError: Client error '403 Forbidden' for url 'https://api.telegram.org/bot123:ABC/sendMessage'"
    sent.status = "sent"
    sent.attempts = 1
    db.commit()

    summary = log.summary(db)

    assert summary["sent"] == 1
    assert summary["failed"] == 2
    assert summary["retryable"] == 1
    assert summary["failed_final"] == 1
    assert summary["by_status"]["failed_retryable"] == 1
    assert summary["by_status"]["failed_final"] == 1
    assert "bot<redacted>" in summary["last_error"]
    assert "123:ABC" not in summary["last_error"]


class ForbiddenSender(TelegramDeliveryWorker):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def _send_telegram_http(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        self.calls += 1
        raise RuntimeError("403 Forbidden for url 'https://api.telegram.org/bot123:ABC/sendMessage'")


class CountingSender(TelegramDeliveryWorker):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def _send_telegram_http(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        self.calls += 1


@pytest.mark.anyio
async def test_delivery_worker_marks_telegram_403_as_final_and_redacts_token():
    db = make_session()
    log = TelegramDeliveryLog()
    delivery = log.queue(
        db,
        chat_id="123",
        text_value="blocked bot",
        message_type="owner_alert",
        max_attempts=3,
    )
    db.commit()

    worker = ForbiddenSender()
    result = await worker.process_once(db)
    db.commit()
    db.refresh(delivery)

    assert result["processed"] == 1
    assert result["failed_final"] == 1
    assert result["failed_retryable"] == 0
    assert delivery.status == "failed_final"
    assert delivery.next_retry_at is None
    assert "bot<redacted>" in delivery.error
    assert "123:ABC" not in delivery.error


@pytest.mark.anyio
async def test_delivery_worker_does_not_retry_existing_non_retryable_error():
    db = make_session()
    log = TelegramDeliveryLog()
    delivery = log.queue(db, "123", "old forbidden", "owner_alert", max_attempts=3)
    delivery.status = "failed_retryable"
    delivery.attempts = 1
    delivery.error = "HTTPStatusError: Client error '403 Forbidden' for url 'https://api.telegram.org/bot123:ABC/sendMessage'"
    delivery.next_retry_at = datetime.now(timezone.utc)
    db.commit()

    worker = CountingSender()
    result = await worker.process_once(db)
    db.commit()
    db.refresh(delivery)

    assert worker.calls == 0
    assert result["processed"] == 1
    assert result["failed_final"] == 1
    assert delivery.status == "failed_final"
    assert delivery.next_retry_at is None
    assert "bot<redacted>" in delivery.error
    assert "123:ABC" not in delivery.error
