"""Контракт фронт↔бэк: коды, которые отдаёт API, должны иметь ярлык в UI.

Мотивация (#fe-sync-2026-07-25): каждый раунд правок добавлял новые
`close_reason` / `decision`, а фронт узнавал о них вручную — и показывал сырой
код (`trend_capture_band`, `reentry_adverse_price`). Ошибка тихая: страница не
падает, просто владелец видит машинный идентификатор вместо смысла.

Тест дешёвый и статический — читает исходники, ничего не запускает.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[1]   # apps/api
WEB = API.parent / "web"                    # apps/web

# Причины, которые НЕ попадают в Signal.closed_reason и ярлыка не требуют.
NOT_A_CLOSE_REASON = {
    "tp1_partial",   # причина частичного закрытия позиции, а не закрытия сделки
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _backend_close_reasons() -> set[str]:
    """Все строковые литералы reason=... из exit-контура."""
    reasons: set[str] = set()
    for name in ("services/exit_policy.py", "services/signal_lifecycle.py"):
        src = _read(API / name)
        reasons |= set(re.findall(r'reason="([a-z0-9_]+)"', src))
        # динамические: reason = "a" if cond else "b"
        for line in re.findall(r'reason = "([a-z0-9_]+)" if .* else "([a-z0-9_]+)"', src):
            reasons |= set(line)
    return reasons - NOT_A_CLOSE_REASON


def _frontend_labels(page: str, dict_name: str) -> set[str]:
    path = WEB / page
    if not path.exists():
        pytest.skip(f"нет фронтенда по пути {path}")
    src = _read(path)
    start = src.index(dict_name)
    body = src[start : src.index("};", start)]
    return set(re.findall(r"^\s{2,}([a-z0-9_]+):", body, re.M))


def test_every_close_reason_has_a_ui_label():
    backend = _backend_close_reasons()
    frontend = _frontend_labels("app/signals/page.tsx", "CLOSE_REASON_LABELS")

    missing = sorted(backend - frontend)
    assert not missing, (
        "бэкенд отдаёт close_reason без ярлыка в UI — владелец увидит сырой код: "
        f"{missing}"
    )


def test_new_exit_and_guard_codes_are_labelled_in_intelligence_feed():
    """Лента решений показывает decisionLabel(); новые коды раунда 25.07
    должны быть в карте, иначе в ленте будет машинный идентификатор."""
    src = _read(WEB / "app/intelligence/page.tsx") if (WEB / "app/intelligence/page.tsx").exists() else None
    if src is None:
        pytest.skip("нет фронтенда")

    for code in ("trend_capture_band", "reentry_adverse_price", "reentry_cooldown_active"):
        assert f"{code}:" in src, f"decisionLabel не знает код {code}"


def test_backend_exposes_honest_pnl_fields_the_ui_reads():
    """UI показывает честный PnL и счётчик фантомов — поля обязаны существовать.

    Раньше дашборд читал только `total_net_pnl_usdt`, завышенный фантомными
    филлами: главная карточка показывала прибыль, которой не было.
    """
    analytics = _read(API / "routers/analytics.py")
    gates = _read(API / "services/validation_gates.py")

    assert "total_net_pnl_honest_usdt" in analytics
    assert "phantom_fill_count" in analytics or "summarize_phantom" in analytics
    assert "net_pnl_honest_usdt" in gates
    assert "no_phantom_fills_in_sample" in gates

    web_analytics = WEB / "app/analytics/page.tsx"
    if web_analytics.exists():
        ui = _read(web_analytics)
        assert "total_net_pnl_honest_usdt" in ui, "дашборд всё ещё показывает сырой PnL"
        assert "net_pnl_honest_usdt" in ui, "Profit gates показывают не ту цифру, по которой судит гейт"
        assert "phantom_fill_count" in ui


def test_live_safety_trade_counter_is_surfaced():
    """Новый предохранитель MAX_TRADES_PER_DAY должен быть виден на Health."""
    safety = _read(API / "services/live_safety.py")
    assert "trade_count_blocked" in safety and "trades_today" in safety

    health = WEB / "app/health/page.tsx"
    if health.exists():
        ui = _read(health)
        assert "trades_today" in ui, "счётчик сделок за сутки не выведен на Health"
        assert "trade_count_blocked" in ui
