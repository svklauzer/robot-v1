"""Постраничная выдача подписчиков (#subscribers-paging-2026-09-07).

Раньше `/subscribers` отдавал всю таблицу одним куском, а фильтры и сводка
считались на клиенте. Пагинация сама по себе сломала бы и то, и другое
НЕЗАМЕТНО: фильтр начал бы отбирать внутри страницы, а карточки — показывать
размер страницы вместо размера базы. Поэтому фильтры и счётчики переехали в SQL
вместе с пагинацией, а не после неё.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db import Base
from models.subscriber import Subscriber
import routers.subscribers as subs


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Subscriber.__table__])
    session = sessionmaker(bind=engine)()
    monkeypatch.setattr(subs, "SessionLocal", lambda: session)
    # Роутер закрывает сессию в finally — в тесте это оборвало бы следующий вызов.
    monkeypatch.setattr(session, "close", lambda: None)
    yield session
    session.__class__.close(session)


def _add(db, n=1, *, status="active", plan="vip", trial=False, days=30, name=None):
    now = datetime.now(timezone.utc)
    for i in range(n):
        db.add(Subscriber(
            telegram_user_id=f"tg{db.query(Subscriber).count() + i}",
            username=name or f"user{i}", full_name=name or f"Name {i}",
            plan=plan, status=status, is_trial=trial,
            starts_at=now,
            expires_at=(now + timedelta(days=days)) if days is not None else None,
        ))
    db.commit()


# ── страница ────────────────────────────────────────────────────────────────

def test_the_page_is_a_page_and_says_how_many_there_are(db):
    _add(db, 12)

    out = subs.list_subscribers(limit=5, offset=0)

    assert len(out["items"]) == 5
    assert out["total"] == 12, "total обязан считать всю выборку, а не страницу"
    assert out["limit"] == 5 and out["offset"] == 0


def test_offset_moves_the_window(db):
    _add(db, 7)

    first = [r["id"] for r in subs.list_subscribers(limit=3, offset=0)["items"]]
    second = [r["id"] for r in subs.list_subscribers(limit=3, offset=3)["items"]]

    assert first and second and not set(first) & set(second), "окна пересеклись"


def test_the_limit_is_clamped(db):
    _add(db, 3)

    assert subs.list_subscribers(limit=10_000)["limit"] == 500
    assert subs.list_subscribers(limit=0)["limit"] == 1
    assert subs.list_subscribers(offset=-5)["offset"] == 0


# ── фильтры отбирают по базе, а не по странице ──────────────────────────────

def test_filters_search_the_whole_table_not_the_loaded_page(db):
    """Ради этого фильтры и переехали в SQL. Искомый подписчик лежит ЗА первой
    страницей: клиентский фильтр после пагинации его бы не нашёл и ответил
    «ничего не найдено» — самый убедительный вид неправды.
    """
    _add(db, 150)
    _add(db, 1, name="Иванов")

    out = subs.list_subscribers(limit=10, q="Иванов")

    assert out["total"] == 1
    assert out["items"][0]["full_name"] == "Иванов"


def test_status_and_trial_filters_narrow_the_count(db):
    _add(db, 4, status="active")
    _add(db, 3, status="blocked")
    _add(db, 2, status="active", trial=True)

    assert subs.list_subscribers(status="blocked")["total"] == 3
    assert subs.list_subscribers(trial="trial")["total"] == 2
    assert subs.list_subscribers(trial="paid")["total"] == 7


# ── сводка считается по всей базе ───────────────────────────────────────────

def test_the_overview_counts_the_base_not_the_page(db):
    _add(db, 200, status="active")
    _add(db, 5, status="blocked")

    out = subs.list_subscribers(limit=10)

    assert len(out["items"]) == 10
    assert out["overview"]["total"] == 205, "сводка съехала на размер страницы"
    assert out["overview"]["blocked"] == 5


def test_the_overview_ignores_the_filter(db):
    """Карточки — обзор всей базы. Если бы они считались по фильтру, «Всего»
    менялось бы от нажатия на «Blocked», и понять размер базы стало бы нечем.
    """
    _add(db, 6, status="active")
    _add(db, 2, status="blocked")

    out = subs.list_subscribers(status="blocked")

    assert out["total"] == 2
    assert out["overview"]["total"] == 8


def test_expiring_soon_counts_only_the_near_deadline(db):
    _add(db, 1, days=1)      # истекает завтра
    _add(db, 1, days=90)     # не скоро

    assert subs.list_subscribers()["overview"]["expiring_soon"] == 1


# ── инвариант схемы ─────────────────────────────────────────────────────────

def test_a_subscriber_cannot_exist_without_a_deadline(db):
    """Проверялось как гипотеза «бессрочная подписка показывается нулём дней» —
    и оказалось, что такого состояния не бывает: `expires_at` объявлена NOT NULL.
    Ветка на None в сериализаторе осталась страховкой на случай смены схемы, а
    тест закрепляет, ПОЧЕМУ она сегодня недостижима.
    """
    import sqlalchemy.exc

    now = datetime.now(timezone.utc)
    db.add(Subscriber(telegram_user_id="tg-no-exp", plan="vip", status="active",
                      starts_at=now, expires_at=None))

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db.commit()
    db.rollback()
