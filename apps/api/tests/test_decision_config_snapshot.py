"""Сделка обязана записывать правила, по которым её приняли.

Без этого история неразборчива: видно, ЧТО получилось, и не видно, ПО КАКИМ
правилам. Именно поэтому 288 сделок с 12.06 по 28.07 нельзя разложить по
настройкам — config.py правился почти ежедневно, а в plan_json от него не
осталось ни одного ключа. Ответ на вопрос «при каких настройках система
зарабатывает» был недостижим не из-за нехватки данных, а из-за отсутствия
признака при наличии метки.
"""
from __future__ import annotations

from pathlib import Path

from core.config import settings
from services.decision_config import fingerprint, snapshot

API = Path(__file__).resolve().parents[1]


def test_snapshot_records_the_levers_that_decide_a_trade():
    cfg = snapshot(market_type="spot", fee_rate=0.002, leverage=1)

    # Каждый раздел отвечает за свой этап решения: пропустить, каким размером,
    # когда закрыть. Пропажа любого делает сделку несравнимой с другими.
    for section in ("market", "entry_gate", "anti_drain", "sizing", "exit", "guards", "ml"):
        assert section in cfg, f"в снимке нет раздела {section}"

    assert cfg["market"]["market_type"] == "spot"
    assert cfg["market"]["taker_fee"] == 0.002
    assert cfg["exit"]["breakeven_lock_effective_floor_pct"] is not None
    assert cfg["fingerprint"]


def test_exit_floor_follows_the_market_of_the_trade():
    """Пол замка выводится из ставки рынка сделки, а не из общей настройки.

    Спот стоит 0.42% round-trip, своп 0.12%. Пол, посчитанный по свопу для
    спотовой сделки, разрешает «безубыток» на уровне, который гарантированно
    закрывается в минус — это и был механизм, давший 45 срабатываний с
    винрейтом 2%.
    """
    spot = snapshot(market_type="spot", fee_rate=0.002, leverage=1)
    swap = snapshot(market_type="swap", fee_rate=0.0005, leverage=1)

    spot_floor = spot["exit"]["breakeven_lock_effective_floor_pct"]
    swap_floor = swap["exit"]["breakeven_lock_effective_floor_pct"]

    assert spot_floor > swap_floor, "спотовая сделка получила своповый пол замка"
    assert spot_floor >= spot["market"]["round_trip_pct"], (
        "пол замка ниже стоимости сделки — «безубыток» математически убыточен"
    )
    assert swap_floor >= swap["market"]["round_trip_pct"]


def test_fingerprint_separates_config_generations():
    a = snapshot(market_type="spot", fee_rate=0.002, leverage=1)
    b = snapshot(market_type="swap", fee_rate=0.0005, leverage=1)
    assert a["fingerprint"] != b["fingerprint"]

    again = snapshot(market_type="spot", fee_rate=0.002, leverage=1)
    assert a["fingerprint"] == again["fingerprint"], "одинаковые настройки дали разный отпечаток"


def test_fingerprint_reacts_to_a_threshold_change(monkeypatch):
    """Правка порога обязана порождать новое поколение конфига.

    Иначе сделки до и после правки сольются в одну выборку, и эффект правки
    снова будет неизмерим — ровно то, что произошло за июнь-июль.
    """
    before = snapshot(market_type="spot", fee_rate=0.002, leverage=1)
    monkeypatch.setattr(settings, "BREAKEVEN_LOCK_FLOOR_PCT", 0.99)
    after = snapshot(market_type="spot", fee_rate=0.002, leverage=1)

    assert before["fingerprint"] != after["fingerprint"]


def test_fingerprint_ignores_itself():
    cfg = snapshot(market_type="spot", fee_rate=0.002, leverage=1)
    assert fingerprint(cfg) == cfg["fingerprint"]


def test_scalp_and_trend_record_their_own_sizing_caps(monkeypatch):
    monkeypatch.setattr(settings, "MAX_POSITION_MARGIN_PCT", 0.13)
    monkeypatch.setattr(settings, "SCALP_MAX_POSITION_MARGIN_PCT", 0.20)

    trend = snapshot(market_type="spot", fee_rate=0.002, is_scalp=False)
    scalp = snapshot(market_type="spot", fee_rate=0.002, is_scalp=True)

    assert trend["sizing"]["max_position_margin_pct"] == 0.13
    assert scalp["sizing"]["max_position_margin_pct"] == 0.20


def test_robot_loop_writes_the_snapshot_into_every_signal():
    """Снимок должен попадать в сделку, а не оставаться утилитой."""
    src = (API / "workers" / "robot_loop.py").read_text(encoding="utf-8")

    assert "config_snapshot(" in src, "снимок конфига не вызывается в цикле"
    assert '"config": _decision_config' in src, "снимок не пишется в plan_json"

    # Снимок обязан сниматься после гейтов: иначе в нём не будет фактических
    # порогов грейда, по которым кандидат прошёл.
    assert src.index("production_decision = self.production_gate.check") < src.index("config_snapshot(")
