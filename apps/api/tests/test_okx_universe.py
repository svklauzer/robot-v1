"""Вселенная следует за биржей исполнения (#okx-universe-2026-09-12).

У каждой биржи свой листинг и своя ликвидность. Переключение ACTIVE_EXCHANGE
само переключает список символов; пустой OKX_SYMBOLS — прежнее поведение
(HTX_SYMBOLS). Открытые сделки ведутся из базы, а не из вселенной.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from core.config import settings

ROOT = Path(__file__).resolve().parents[3]


def _set(monkeypatch, exchange, htx, okx):
    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", exchange, raising=False)
    monkeypatch.setattr(settings, "HTX_SYMBOLS", htx, raising=False)
    monkeypatch.setattr(settings, "OKX_SYMBOLS", okx, raising=False)


def test_okx_uses_its_own_universe(monkeypatch):
    _set(monkeypatch, "okx", "BTC/USDT,TRX/USDT", "BTC/USDT, CHIP/USDT ,PI/USDT")
    assert settings.symbols == ["BTC/USDT", "CHIP/USDT", "PI/USDT"]
    assert settings.universe_source == "OKX_SYMBOLS"


def test_htx_keeps_its_universe(monkeypatch):
    _set(monkeypatch, "htx", "BTC/USDT,TRX/USDT", "CHIP/USDT")
    assert settings.symbols == ["BTC/USDT", "TRX/USDT"]
    assert settings.universe_source == "HTX_SYMBOLS"


def test_an_empty_okx_list_falls_back_to_the_old_one(monkeypatch):
    _set(monkeypatch, "okx", "BTC/USDT,TRX/USDT", "  ")
    assert settings.symbols == ["BTC/USDT", "TRX/USDT"]
    assert settings.universe_source == "HTX_SYMBOLS"


def test_each_exchange_can_be_asked_directly(monkeypatch):
    _set(monkeypatch, "htx", "BTC/USDT", "PI/USDT")
    assert settings.symbols_for("okx") == ["PI/USDT"]
    assert settings.symbols_for("htx") == ["BTC/USDT"]


def test_the_blueprint_declares_the_okx_universe():
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "- key: OKX_SYMBOLS" in blueprint


def test_open_trades_are_managed_from_the_database_not_the_universe():
    """Символ, убранный из списка, доживает до закрытия."""
    from services.signal_lifecycle import SignalLifecycleManager

    src = inspect.getsource(SignalLifecycleManager.process_open_signals)
    assert "Signal.status.in_" in src
    assert "symbols" not in src.split("Signal.status.in_", 1)[0]


def test_the_depth_feed_also_covers_open_trades():
    main_src = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    assert "open_syms" in main_src and "ob_symbols + [s for s in open_syms if s]" in main_src


def test_health_says_which_universe_is_live():
    main_src = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    page = (ROOT / "apps/web/app/health/page.tsx").read_text(encoding="utf-8")
    assert 'out["universe"]' in main_src
    assert "health?.universe?.source" in page



def test_the_owner_chosen_okx_universe_is_live_in_both_places():
    """Решение 12.09: ядро по ликвидности + DOGE, HYPE; CHIP и PI — эксперимент."""
    chosen = "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,HYPE/USDT,LINK/USDT,LTC/USDT,CHIP/USDT,PI/USDT"
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert f"key: OKX_SYMBOLS\n        value: {chosen}" in blueprint
    assert settings.OKX_SYMBOLS == chosen
