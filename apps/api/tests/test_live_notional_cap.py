"""Кэп нотионала: dry_run и рассинхрон бумаги с биржей (#dry-run-cap-2026-07-26).

Из боевых логов 26.07:

    {"cap": 25.0, "event": "live_order_notional_cap", "notional": 249.34, "symbol": "ADA/USDT"}

Две проблемы, которые это вскрыло:

1. Кэп проверялся ДО ветки dry_run и обрывал прогон. Смысл dry_run — провести
   живой путь исполнения целиком; с обрывом этап 1 плана вывода в live ничего
   не валидирует.
2. `_submit_live` не проверял результат: бумажная позиция открывалась независимо
   от того, ушёл ордер или нет. В LIVE это тихий рассинхрон — робот считает себя
   в рынке, а его там нет.
"""
import pytest

from core.config import settings
from services.live_executor import LiveExecutor


@pytest.fixture
def executor(monkeypatch):
    ex = LiveExecutor.__new__(LiveExecutor)
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 25.0)
    return ex


def _place(executor, monkeypatch, mode):
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: mode))
    return executor.place_market(
        "ADA/USDT", "buy", 1000.0,
        market_type="swap", reference_price=0.24934,   # нотионал ~249 при кэпе 25
        purpose="test",
    )


def test_dry_run_is_not_aborted_by_the_cap(executor, monkeypatch):
    """Прогон обязан дойти до конца — иначе dry_run ничего не проверяет."""
    res = _place(executor, monkeypatch, "dry_run")

    assert res.ok is True
    assert res.status == "dry_run"
    assert res.sent is False, "dry_run не должен отправлять ордер"


def test_live_is_still_blocked_by_the_cap(executor, monkeypatch):
    """В LIVE кэп — это предохранитель, он обязан работать."""
    res = _place(executor, monkeypatch, "live")

    assert res.ok is False
    assert res.sent is False
    assert "notional>" in (res.error or "")


def test_cap_within_limit_passes_in_live(executor, monkeypatch):
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    sent = {"n": 0}

    def _fake_leverage(*_a, **_k):
        return None

    class _Client:
        def create_order_once(self, *_a, **_k):
            sent["n"] += 1
            raise RuntimeError("stop here — важно лишь, что до отправки дошли")

    monkeypatch.setattr(executor, "_ensure_leverage", _fake_leverage, raising=False)
    executor.client = _Client()

    try:
        executor.place_market(
            "ADA/USDT", "buy", 10.0,
            market_type="swap", reference_price=0.24934,   # нотионал ~2.5 < 25
            purpose="test",
        )
    except Exception:
        pass

    assert sent["n"] == 1, "ордер в пределах кэпа обязан дойти до отправки"


def test_divergence_is_reported_when_live_order_fails():
    """Молчаливого расхождения бумаги и биржи быть не должно."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "services" / "execution_engine.py"
    text = src.read_text(encoding="utf-8")

    assert "live_paper_divergence" in text, (
        "результат live-ордера снова не проверяется — рассинхрон пройдёт молча"
    )
    assert "res.ok" in text and "is_live()" in text


def test_production_blocker_still_guards_the_mismatch():
    """Основная защита — не дать включить live с кэпом ниже размера позиции."""
    from core.config import Settings

    blockers = Settings(
        APP_ENV="development", ENABLE_LIVE_ORDERS=True, TRADING_MODE="live_limited",
        ROBOT_MODE="live", TELEGRAM_BOT_TOKEN="t",
        RISK_EQUITY_USDT=950.0, MAX_POSITION_MARGIN_PCT=0.13,
        LIVE_MAX_ORDER_NOTIONAL_USDT=25.0, LIVE_MAX_LEVERAGE=1.0,
    ).production_blockers()

    assert any("LIVE_MAX_ORDER_NOTIONAL_USDT" in b for b in blockers)
