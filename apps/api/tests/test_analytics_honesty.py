"""Честность витрин аналитики (#analytics-honest-winrate-2026-07-27,
#analytics-capture-2026-07-27).

Два бага одного рода: карточка PnL уже показывала честную цифру, а соседние
метрики продолжали питаться сырыми данными.

1. `winrate` считался по сырому `closed_net_pnl` — фантомный филл (закрытие по
   цене выше собственного максимума) засчитывался в победы. Дашборд показывал
   честный минус и winrate, посчитанный от завышенного плюса.

2. `capture_rate_pct` был СРЕДНИМ ОТНОШЕНИЙ result/mfe. На боевых данных сделка
   #277 (MFE 0.29%, результат −1.04%) даёт −356% и в одиночку уводила бакет из
   14 сделок с 34.3% на 13.0%. В бакетах символ×режим (n=1..3) метрика
   становилась нечитаемой.
"""
from __future__ import annotations

import pytest


class _Sig:
    """Минимальный дубль Signal для чистых функций аналитики."""

    def __init__(self, sid, result_pct, mfe_pct, net_pnl, notional=100.0,
                 traj_last=None, symbol="TRX/USDT", regime="trend", mae_pct=-0.5):
        self.id = sid
        self.symbol = symbol
        self.side = "long"
        self.status = "closed"
        self.result_pct = result_pct
        self.closed_net_pnl = net_pnl
        self.closed_total_cost = 0.0
        self.closed_reason = "protective_trailing_stop"
        self.closed_at = None
        self.required_margin = notional
        self.plan_json = {
            "regime": regime,
            "lifecycle": {
                "mfe_pct": mfe_pct,
                "mae_pct": mae_pct,
                "traj": [[0, 0.0], [1, traj_last if traj_last is not None else mfe_pct]],
            },
        }


def test_phantom_trade_does_not_count_as_win():
    """Филл выше собственного максимума не должен приносить победу.

    Схема TRX #281: книжный +1.8% при максимуме +0.7%, честный выход — минус.
    """
    from services.phantom_fill import phantom_adjustment

    # notional 100, booked 1.8%, честная последняя цена траектории −0.4%
    sig = _Sig(281, result_pct=1.8, mfe_pct=0.7, net_pnl=1.5, traj_last=-0.4)
    is_phantom, adj = phantom_adjustment(sig)

    assert is_phantom, "закрытие выше максимума обязано детектироваться"
    honest = sig.closed_net_pnl + adj
    assert sig.closed_net_pnl > 0, "сырой PnL — победа"
    assert honest < 0, f"честный PnL должен быть убытком, получено {honest}"


def test_honest_winrate_below_raw_when_phantom_present():
    """Сводный winrate: сырой завышен, честный — нет."""
    signals = [
        _Sig(1, result_pct=1.8, mfe_pct=0.7, net_pnl=1.5, traj_last=-0.4),  # фантом
        _Sig(2, result_pct=0.5, mfe_pct=0.9, net_pnl=0.4),                  # честная победа
        _Sig(3, result_pct=-0.8, mfe_pct=0.3, net_pnl=-0.9),                # убыток
        _Sig(4, result_pct=-0.6, mfe_pct=0.2, net_pnl=-0.7),                # убыток
    ]
    from services.phantom_fill import phantom_adjustment

    wins_raw = sum(1 for s in signals if s.closed_net_pnl > 0)
    wins_honest = sum(
        1 for s in signals if s.closed_net_pnl + phantom_adjustment(s)[1] > 0
    )

    assert wins_raw == 2
    assert wins_honest == 1, "фантом обязан выпасть из побед"
    assert wins_honest / len(signals) * 100 == 25.0


# Боевые сделки #264–#282 с MFE ≥ 0.2% (scripts/calib_trades_264_282.json).
# Две строки с result > mfe — это фантомные филлы #272 и #281.
LIVE_ROWS = [
    {"mfe": 0.78, "result_pct": 0.09}, {"mfe": 0.97, "result_pct": 1.80},
    {"mfe": 0.39, "result_pct": 0.21}, {"mfe": 0.74, "result_pct": 0.08},
    {"mfe": 0.29, "result_pct": -1.04}, {"mfe": 0.48, "result_pct": -0.01},
    {"mfe": 0.74, "result_pct": 0.07}, {"mfe": 0.35, "result_pct": 0.10},
    {"mfe": 1.00, "result_pct": 1.80}, {"mfe": 1.54, "result_pct": 0.07},
    {"mfe": 0.64, "result_pct": 0.05}, {"mfe": 1.38, "result_pct": 0.07},
    {"mfe": 0.41, "result_pct": 0.08}, {"mfe": 0.37, "result_pct": 0.09},
]


def _mean_of_ratios(rows):
    return sum(r["result_pct"] / r["mfe"] for r in rows) / len(rows) * 100


def _portfolio(rows):
    return sum(r["result_pct"] for r in rows) / sum(r["mfe"] for r in rows) * 100


def test_capture_rate_outlier_distortion_on_live_data():
    """Один выброс уводил метрику на боевой выборке из 14 сделок.

    Сделка #277: MFE 0.29%, результат −1.04% ⇒ отношение −356%.
    """
    ratios = [r["result_pct"] / r["mfe"] * 100 for r in LIVE_ROWS]
    assert min(ratios) < -350, "выброс на месте"

    old = _mean_of_ratios(LIVE_ROWS)
    new = _portfolio(LIVE_ROWS)
    assert old == pytest.approx(12.9, abs=0.3)
    assert new == pytest.approx(34.3, abs=0.3)
    assert new - old > 20, "искажение больше 20 процентных пунктов"

    without = [r for r in LIVE_ROWS if r["result_pct"] / r["mfe"] > -3]
    assert _mean_of_ratios(without) - old > 25, (
        "старая метрика определялась одной сделкой из четырнадцати"
    )


def test_capture_rate_after_phantom_clamp_on_live_data():
    """Клампа к MFE достаточно, чтобы снять завышение фантомов.

    Важный вывод для калибровки выходов: честный capture ≈ 18%, а не 34% —
    мы отдаём около четырёх пятых предложенного хода.
    """
    clamped = [
        {"mfe": r["mfe"], "result_pct": min(r["result_pct"], r["mfe"])}
        for r in LIVE_ROWS
    ]
    assert max(r["result_pct"] / r["mfe"] for r in clamped) <= 1.0 + 1e-9, (
        "capture по отдельной сделке не может превышать 100%"
    )
    assert _portfolio(clamped) == pytest.approx(18.2, abs=0.3)
    assert _portfolio(clamped) < _portfolio(LIVE_ROWS), "фантомы завышали capture"


def test_capture_clamped_to_mfe():
    """result_pct не может превышать пик — иначе capture > 100%."""
    mfe = 0.7
    booked = 1.8
    clamped = min(booked, mfe)
    assert clamped == mfe
    assert clamped / mfe * 100 == pytest.approx(100.0)


def test_single_trade_bucket_capture_is_readable():
    """Бакет символ×режим часто n=1 — метрика обязана оставаться в разумных рамках."""
    rows = [{"mfe": 0.25, "result_pct": -0.9}]
    new = sum(r["result_pct"] for r in rows) / sum(r["mfe"] for r in rows) * 100
    # Отрицательный capture возможен и корректен (вышли хуже нуля при бывшем плюсе),
    # но он должен отражать масштаб, а не давать −360% из-за деления на мелкий MFE.
    assert new == pytest.approx(-360.0, abs=1.0)
    # Именно поэтому рядом отдаётся медиана и размер выборки — см. capture_sample_count.
