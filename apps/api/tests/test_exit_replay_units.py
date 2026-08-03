"""Единицы измерения в реплее выходов (#replay-units-2026-08-03).

Точки `traj` — ВАЛОВОЕ движение цены. `result_pct` — результат ПОСЛЕ издержек.
Пока ранний выход возвращал точку траектории, а базой сравнения служила сумма
`result_pct`, каждый ранний выход получал даром разницу единиц: на боевой
выборке это 0.1385–0.1386% на сделку (сверено на сигналах #345 и #347).

Смещение направленное: подарок доставался вариантам, которые СИЛЬНЕЕ меняют
поведение, потому что у них больше ранних выходов. Текущий конфиг чаще доходит
до `actual_close` и получал меньше всех. Инструмент, созданный ловить подгонку,
сам голосовал за правки — и тем громче, чем крупнее правка.
"""
from __future__ import annotations

import json

import pytest

from services import exit_replay as er

HOUR = 3600.0


def _row(*, cost=0.1, qty=1.0, entry=100.0, traj=None, result_pct=0.2):
    return {
        "closed_total_cost": cost,
        "qty": qty,
        "result_pct": result_pct,
        "lifecycle": {"entry_price": entry, "traj": traj or [[0, 0.0]]},
    }


# ── извлечение издержек ─────────────────────────────────────────────────────
def test_cost_pct_matches_the_live_trades():
    """Сверка с фактом: сигналы #345 и #347 дали 0.1385 и 0.1386."""
    # #345 SOL: qty 1.0, вход 72.5097, издержки 0.100439
    assert er._cost_pct(_row(cost=0.100439, qty=1.0, entry=72.5097)) == pytest.approx(
        0.1385, abs=1e-3
    )
    # #347 SOL: qty 0.5, вход 72.805, издержки 0.050448
    assert er._cost_pct(_row(cost=0.050448, qty=0.5, entry=72.805)) == pytest.approx(
        0.1386, abs=1e-3
    )


def test_cost_pct_never_returns_zero_when_data_is_missing():
    """Ноль означал бы возврат к сравнению валового с чистым — худший исход."""
    assert er._cost_pct({}) > 0
    assert er._cost_pct(_row(cost=0.0, qty=0.0, entry=0.0)) > 0


# ── ранний выход платит круг ────────────────────────────────────────────────
def test_early_exit_pays_the_round_trip():
    """Замок сработал по траектории → результат валовой и обязан заплатить."""
    traj = [[0, 0.0], [60, 1.0], [120, 0.4]]  # пик 1.0, отдали 60%
    pct, reason = er._replay_one(traj, final_pct=0.0, arm=0.5, give=0.5,
                                 ts_min=None, hard_mult=2.0, cost_pct=0.14)
    assert reason == "replay_breakeven_lock"
    assert pct == pytest.approx(0.4 - 0.14)


def test_holding_to_the_end_does_not_pay_twice():
    """`final_pct` уже чистый — вычитать из него издержки нельзя."""
    traj = [[0, 0.0], [60, 0.1], [120, 0.05]]  # ничего не взводится
    pct, reason = er._replay_one(traj, final_pct=0.2, arm=5.0, give=0.5,
                                 ts_min=None, hard_mult=2.0, cost_pct=0.14)
    assert reason == "actual_close"
    assert pct == pytest.approx(0.2)


def test_time_stop_exit_pays_the_round_trip():
    traj = [[0, 0.0], [50 * 60, -0.5]]
    pct, reason = er._replay_one(traj, final_pct=0.0, arm=5.0, give=0.5,
                                 ts_min=45.0, hard_mult=2.0, cost_pct=0.14)
    assert reason == "replay_time_stop"
    assert pct == pytest.approx(-0.5 - 0.14)


def test_trend_ladder_exits_pay_the_round_trip():
    traj = [[0, 0.0], [60, 1.2], [120, 0.5]]
    pct, reason = er._replay_trend_one(
        traj, final_pct=0.0, be_arm=0.3, be_floor=0.1,
        band_arm=0.4, band_give=0.25, band_floor=0.3,
        ride_arm=0.8, ride_trail=0.5, min_protective=0.4,
        cost_pct=0.14,
    )
    assert reason.startswith("replay_")
    assert pct == pytest.approx(0.5 - 0.14)


# ── направленность смещения ─────────────────────────────────────────────────
def test_build_trend_runs_end_to_end(tmp_path, monkeypatch):
    """Дымоход по всей сборке отчёта.

    (#band-corridor-2026-08-03) Когда ride_arm стал осью перебора, ссылка на
    него осталась висеть в блоке `fixed` за пределами цикла. Локально это не
    падало: без данных `build_trend` выходит раньше по ветке no_data, и до
    строки исполнение не доходит. NameError вылез в проде.

    Тест кормит сборку минимальными данными, чтобы дойти до самого конца.
    """
    row = {
        "trade_mode": "trend",
        "result_pct": 0.20,
        "qty": 1.0,
        "closed_total_cost": 0.10,
        "lifecycle": {
            "entry_price": 100.0,
            "mfe_pct": 1.0,
            "traj": [[0, 0.0], [60, 1.0], [120, 0.4]],
        },
    }
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps(row) for _ in range(6)), encoding="utf-8")

    class _Logger:
        def __init__(self):
            self.path = str(path)

    monkeypatch.setattr("services.ml_trade_logger.MLTradeLogger", _Logger)

    out = er.build_trend(limit=100)

    assert out["status"] == "ok"
    assert out["trades_replayed"] == 6
    # Правая граница коридора обязана быть осью, а не константой.
    assert "ride_arm_pct" not in out["fixed"]
    assert out["axes"]["ride_arm_pct"]
    assert "band_corridor_width" in out["best"]


def test_the_bias_favoured_whichever_variant_changed_more():
    """Суть бага: подарок пропорционален числу ранних выходов.

    Вариант, который трогает поведение чаще, получал больше даром — то есть
    инструмент против подгонки был встроенно смещён В СТОРОНУ правок.
    """
    traj = [[0, 0.0], [60, 1.0], [120, 0.4]]
    biased, _ = er._replay_one(traj, final_pct=0.0, arm=0.5, give=0.5,
                               ts_min=None, hard_mult=2.0, cost_pct=0.0)
    honest, _ = er._replay_one(traj, final_pct=0.0, arm=0.5, give=0.5,
                               ts_min=None, hard_mult=2.0, cost_pct=0.14)
    assert biased - honest == pytest.approx(0.14)

    # А вариант, который до раннего выхода не дошёл, подарка не получал вовсе.
    quiet_biased, r1 = er._replay_one(traj, final_pct=0.2, arm=9.0, give=0.5,
                                      ts_min=None, hard_mult=2.0, cost_pct=0.0)
    quiet_honest, r2 = er._replay_one(traj, final_pct=0.2, arm=9.0, give=0.5,
                                      ts_min=None, hard_mult=2.0, cost_pct=0.14)
    assert r1 == r2 == "actual_close"
    assert quiet_biased == quiet_honest
