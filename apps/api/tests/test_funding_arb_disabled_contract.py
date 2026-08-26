"""Контракт «арбитраж выключен» между бэкендом и страницей
(#funding-arb-off-2026-08-26).

Страница `/funding` определяет состояние контура НЕ из хардкода в вёрстке, а из
ответа бэкенда: плашка «торговля выкл» показывается, когда все снимки приходят
со `status == "disabled"`. Значит переименование этого статуса молча погасит
предупреждение, и интерфейс начнёт показывать выключенный контур как живой.

Ровно на этом обожглись 26.08 с `TZ_USE_DYNAMIC_ATR_STOPS`: настройка стояла
`true`, страница конфига показывала её действующей, а входа у неё не было и
адаптивный стоп не работал ни разу. Интерфейс, утверждающий состояние
независимо от кода, — это не косметика.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services import capital_envelopes as ce


def test_disabled_status_is_the_string_the_page_reads(monkeypatch):
    """Имя статуса — часть контракта с фронтом, а не деталь реализации."""
    import inspect

    from services import funding_arbitrage

    src = inspect.getsource(funding_arbitrage)
    assert 'status = "disabled"' in src
    assert '"funding_arb_disabled"' in src


def test_page_keys_on_the_same_string():
    """Обе стороны сверяются с одной строкой."""
    from pathlib import Path

    page = Path(__file__).resolve().parents[3] / "apps" / "web" / "app" / "funding" / "page.tsx"
    text = page.read_text(encoding="utf-8")

    assert 'item.status === "disabled"' in text, \
        "страница перестала читать статус — плашка погаснет незаметно"
    assert "tradingOff" in text


# ── освободившийся конверт ──────────────────────────────────────────────────
def test_disabled_and_empty_arb_releases_its_envelope(monkeypatch):
    """20% арбитража уходят направленным, когда он выключен и пуст.

    Это и было причиной вопроса: при включённом, но никогда не срабатывающем
    контуре ~190 USDT из 950 стояли мёртвым грузом — доля отдаётся по ФЛАГУ,
    а не по факту простоя.
    """
    monkeypatch.setattr(settings, "ENABLE_FUNDING_ARB", False, raising=False)
    monkeypatch.setattr(ce, "_arb_holds", lambda db: False)
    monkeypatch.setattr(ce, "_grid_holds", lambda: False)
    monkeypatch.setattr(settings, "GRID_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "GRID_KILL_SWITCH_ENABLED", False, raising=False)

    shares = ce.effective_shares(db=None)

    assert shares[ce.ARB] == 0.0
    assert shares[ce.DIRECTIONAL] == pytest.approx(
        float(settings.CAPITAL_ENVELOPE_DIRECTIONAL_PCT)
        + float(settings.CAPITAL_ENVELOPE_ARB_PCT))
    assert shares["_released_pct"] == pytest.approx(
        float(settings.CAPITAL_ENVELOPE_ARB_PCT))


def test_open_hedges_keep_the_envelope_even_when_disabled(monkeypatch):
    """Выключен, но держит позиции — доля остаётся за ним.

    Иначе направленные заняли бы капитал, который физически стоит в хедже, и
    общий учёт маржи снова разъехался бы.
    """
    monkeypatch.setattr(settings, "ENABLE_FUNDING_ARB", False, raising=False)
    monkeypatch.setattr(ce, "_arb_holds", lambda db: True)
    monkeypatch.setattr(ce, "_grid_holds", lambda: False)

    shares = ce.effective_shares(db=None)

    assert shares[ce.ARB] == pytest.approx(float(settings.CAPITAL_ENVELOPE_ARB_PCT))
    assert "держит позиции" in shares["_detail"][ce.ARB]


def test_total_never_exceeds_one_hundred(monkeypatch):
    """Передача доли не должна создавать капитал из воздуха."""
    monkeypatch.setattr(settings, "ENABLE_FUNDING_ARB", False, raising=False)
    monkeypatch.setattr(ce, "_arb_holds", lambda db: False)
    monkeypatch.setattr(ce, "_grid_holds", lambda: False)
    monkeypatch.setattr(settings, "GRID_ENABLED", False, raising=False)

    shares = ce.effective_shares(db=None)
    total = sum(v for k, v in shares.items() if not k.startswith("_"))

    assert total <= 100.0 + 1e-9
    assert total == pytest.approx(sum(ce.configured_shares().values()))
