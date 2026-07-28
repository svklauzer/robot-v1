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
        # Своп-рынок HTX принимает объём в контрактах, поэтому двойник обязан
        # знать размер контракта — иначе ядро откажет в отправке, и тест
        # проверял бы отказ, а не прохождение кэпа.
        def contract_size(self, _symbol):
            return 10.0

        def amount_to_precision(self, _symbol, amount):
            return amount

        def create_order_once(self, *_a, **_k):
            sent["n"] += 1
            raise RuntimeError("stop here — важно лишь, что до отправки дошли")

    monkeypatch.setattr(executor, "_ensure_leverage", _fake_leverage, raising=False)
    executor.client = _Client()

    try:
        executor.place_market(
            "ADA/USDT:USDT", "buy", 10.0,
            market_type="swap", reference_price=0.24934,   # нотионал ~2.5 < 25
            purpose="test",
        )
    except Exception:
        pass

    assert sent["n"] == 1, "ордер в пределах кэпа обязан дойти до отправки"


def test_swap_order_is_submitted_in_contracts(monkeypatch):
    """Объём для linear-свопа переводится монеты → контракты.

    1 контракт ADA-USDT = 10 ADA. Позиция 1000 ADA — это 100 контрактов;
    отправленные как есть 1000 открыли бы позицию в десять раз больше.
    """
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 100000.0)

    captured = {}

    class _Client:
        def contract_size(self, _symbol):
            return 10.0

        def amount_to_precision(self, _symbol, amount):
            return amount

        def create_order_once(self, _symbol, _type, _side, amount, *_a, **_k):
            captured["amount"] = amount
            raise RuntimeError("до отправки дошли — дальше не нужно")

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.client = _Client()
    executor._leverage_set = set()
    monkeypatch.setattr(executor, "_ensure_leverage", lambda *_a, **_k: None, raising=False)

    executor.place_market(
        "ADA/USDT:USDT", "buy", 1000.0,
        market_type="swap", reference_price=0.25, purpose="test",
    )

    assert captured["amount"] == 100.0, "объём ушёл в монетах вместо контрактов"


def test_swap_order_is_refused_when_contract_size_unknown(monkeypatch):
    """Угадывать размер контракта нельзя: ошибка кратна, а не процентна."""
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 100000.0)

    class _Client:
        def contract_size(self, _symbol):
            return None

        def create_order_once(self, *_a, **_k):
            raise AssertionError("ордер не должен был уйти")

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.client = _Client()
    executor._leverage_set = set()
    monkeypatch.setattr(executor, "_ensure_leverage", lambda *_a, **_k: None, raising=False)

    result = executor.place_market(
        "ADA/USDT:USDT", "buy", 1000.0,
        market_type="swap", reference_price=0.25, purpose="test",
    )

    assert result.ok is False
    assert result.sent is False
    assert "contract_size_unknown" in (result.error or "")


def test_spot_order_keeps_base_units(monkeypatch):
    """На споте объём — это монеты, никакого пересчёта быть не должно."""
    monkeypatch.setattr(LiveExecutor, "effective_mode", classmethod(lambda cls: "live"))
    monkeypatch.setattr(settings, "LIVE_MAX_ORDER_NOTIONAL_USDT", 100000.0)

    captured = {}

    class _Client:
        def contract_size(self, _symbol):
            raise AssertionError("спот не должен спрашивать размер контракта")

        def create_order_once(self, _symbol, _type, _side, amount, *_a, **_k):
            captured["amount"] = amount
            raise RuntimeError("до отправки дошли")

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.client = _Client()
    executor._leverage_set = set()
    monkeypatch.setattr(executor, "_ensure_leverage", lambda *_a, **_k: None, raising=False)

    executor.place_market(
        "ADA/USDT", "buy", 1000.0,
        market_type="spot", reference_price=0.25, purpose="test",
    )

    assert captured["amount"] == 1000.0


@pytest.mark.anyio
async def test_rejected_live_order_does_not_create_a_position(monkeypatch):
    """Биржа отказала — позиции быть не должно НИ ЗДЕСЬ, ни там.

    Логировать расхождение недостаточно: пока позиция заводится, каждая
    следующая сделка увеличивает разрыв, а reduceOnly-выходы уходят в пустоту.
    """
    from types import SimpleNamespace

    from services.execution_engine import ExecutionEngine

    class _DB:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        def flush(self):
            pass

        def query(self, *_a, **_k):
            return self

        def filter(self, *_a, **_k):
            return self

        def first(self):
            return None

    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.db = _DB()
    engine.client = SimpleNamespace(amount_to_precision=lambda _s, a: a)
    engine.telegram = SimpleNamespace(owner_alert=_noop_async)
    engine.plan_builder = None

    signal = SimpleNamespace(
        id=1, bot_id=1, symbol="ADA/USDT", side="long",
        stop_price=0.24, tp_json={"tp1": 0.26, "tp2": 0.27}, plan_json={},
        qty=1000.0, required_margin=100.0,
        net_pnl_tp1=1.0, net_pnl_tp2=2.0, net_pnl_stop=-1.0,
        net_rr_tp1=0.8, net_rr_tp2=1.4,
    )

    monkeypatch.setattr(
        ExecutionEngine, "_submit_live",
        lambda *_a, **_k: {"mode": "live", "ok": False, "status": "error", "error": "notional>25"},
    )

    result = await engine.open_paper_position(
        bot=SimpleNamespace(id=1), signal=signal, entry_price=0.25,
    )

    assert result["status"] == "live_rejected"
    assert result["position"] is None, "позиция заведена вопреки отказу биржи"
    assert result["order"] is None
    assert engine.db.added == [], "в БД не должно попасть ничего"


async def _noop_async(*_a, **_k):
    return None


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
