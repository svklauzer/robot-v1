"""Фид книги следует за биржей ордеров (#depth-venue-2026-09-07).

До правки фид был жёстко на HTX, а торговля с 02.09 ушла на OKX. Книга чужой
биржи при этом не наблюдалась, а работала: `OB_GATE_ENTRIES` блокировал по ней
входы, `entry_depth.*` уходил в план сделки и дальше в форензику стопов как
признак входа. Решение о входе принималось по одной площадке, сделка жила на
другой, а разбор считал это одним и тем же.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from core.config import settings
from services import orderbook_feed as feed


# ── выбор площадки ──────────────────────────────────────────────────────────

def test_feed_follows_the_exchange_that_executes_orders(monkeypatch):
    monkeypatch.setattr(settings, "OB_EXCHANGE", "", raising=False)
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    assert feed.feed_exchange() == "okx"

    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    assert feed.feed_exchange() == "htx"


def test_an_explicit_pin_keeps_the_feed_where_it_is(monkeypatch):
    """Перекрытие нужно не для удобства: переезд фида меняет СМЫСЛ признаков
    входа (у OKX books5 пять уровней против полной глубины HTX), и возможность
    не двигать его при переключении торговли — единственный способ сохранить
    сравнимость разбора.
    """
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    monkeypatch.setattr(settings, "OB_EXCHANGE", "htx", raising=False)

    assert feed.feed_exchange() == "htx"


def test_an_unknown_venue_falls_back_instead_of_exploding(monkeypatch):
    monkeypatch.setattr(settings, "OB_EXCHANGE", "kraken", raising=False)
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "kraken", raising=False)

    assert feed.feed_exchange() == "htx"


# ── символы OKX ─────────────────────────────────────────────────────────────

def test_okx_instrument_id_follows_the_market_type():
    assert feed._okx_inst_id("BTC/USDT", "spot") == "BTC-USDT"
    assert feed._okx_inst_id("BTC/USDT", "swap") == "BTC-USDT-SWAP"


def test_okx_instrument_id_drops_the_ccxt_settlement_suffix():
    """Направленный движок ходит символом `ETH/USDT:USDT`. Если суффикс доедет
    до instId, подписка молча не сработает, фид будет «жив» и пуст, а гейт уйдёт
    в fail-open без единого следа.
    """
    assert feed._okx_inst_id("ETH/USDT:USDT", "swap") == "ETH-USDT-SWAP"


# ── диспетчер ───────────────────────────────────────────────────────────────

def test_dispatcher_starts_the_venue_that_matches(monkeypatch):
    started: list[str] = []

    async def fake_okx(symbols, enabled_fn, store=None, *, shadow=False):
        started.append("okx-shadow" if shadow else "okx")

    async def fake_htx(symbols, enabled_fn, store=None):
        started.append("htx")

    monkeypatch.setattr(feed, "run_okx_orderbook_feed", fake_okx)
    monkeypatch.setattr(feed, "run_htx_orderbook_feed", fake_htx)
    monkeypatch.setattr(settings, "OB_EXCHANGE", "", raising=False)
    monkeypatch.setattr(settings, "OB_OKX_SHADOW_ENABLED", True, raising=False)

    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    asyncio.run(feed.run_orderbook_feed(["BTC/USDT"], lambda: False))

    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "htx", raising=False)
    asyncio.run(feed.run_orderbook_feed(["BTC/USDT"], lambda: False))

    # (#okx-depth-2026-09-12) Рядом с рабочим HTX идёт тень OKX для сравнения.
    assert started == ["okx", "htx", "okx-shadow"]

    started.clear()
    monkeypatch.setattr(settings, "OB_OKX_SHADOW_ENABLED", False, raising=False)
    asyncio.run(feed.run_orderbook_feed(["BTC/USDT"], lambda: False))
    assert started == ["htx"]


# ── протокольные различия, на которых легко обжечься ────────────────────────

def test_okx_branch_answers_silence_itself():
    """OKX не пингует клиента. Если ждать пинга, как от HTX, спокойный рынок
    неотличим от мёртвого сокета — фид либо висит, либо реконнектится вечно.
    """
    source = inspect.getsource(feed.run_okx_orderbook_feed)

    assert '"ping"' in source, "клиент не шлёт ping сам"
    assert '"pong"' in source, "ответ на ping не разбирается"
    assert "awaiting_pong" in source, "реконнект по первой же тишине"


def test_okx_branch_does_not_gunzip():
    """HTX шлёт gzip, OKX — обычный текст. Перепутать значит получить пустой
    фид без ошибок: кадры не распакуются и молча уйдут в continue.
    """
    assert "gzip" not in inspect.getsource(feed.run_okx_orderbook_feed)


def test_a_refused_subscription_is_not_swallowed():
    """Фид, чья подписка отклонена, выглядит живым и пустым, а depth-гейт при
    отсутствии данных уходит в fail-open — вход без подтверждения потоком и без
    единого следа в телеметрии.
    """
    source = inspect.getsource(feed.run_okx_orderbook_feed)

    assert "ob_feed_subscribe_error" in source
