"""Вердикты гейтов должны попадать в выгрузку (#gates-in-export-2026-08-03).

Гейты входа (tz_shadow, trend_trigger, setup_reach) сделаны в режиме
НАБЛЮДЕНИЯ: они считают и записывают, но ничего не блокируют, чтобы потом
измерить, что каждое условие отсекло бы и чем те сделки закончились.

Смысл этого режима держится ровно на одном: что записанное доживает до
выгрузки. Оно не доживало. Вердикты писались в `signal.plan_json` в базе, а
`MLTradeLogger` копировал в журнал только regime / trade_mode / radar_state /
entry_depth / lifecycle / labels. Обнаружилось при первой попытке калибровки:
scripts/tz_calibrate.py нашёл 0 записей на 101 строке.

То есть режим наблюдения работал вхолостую: ждать накопления можно было
бесконечно, данные не появились бы никогда.
"""
from __future__ import annotations

import json

import pytest

from services.ml_trade_logger import MLTradeLogger


class _Signal:
    """Закрытый сигнал с заполненным планом."""

    id = 348
    bot_id = 1
    symbol = "SOL/USDT"
    side = "short"
    grade = "A"
    confidence = 70.85
    rationale = "intelligence_mtf_trend_down"
    status = "closed"
    qty = 1.0
    required_margin = 72.684
    stop_price = 73.3479
    result_pct = -1.1033
    closed_exit_price = 73.384574
    closed_net_pnl = -0.801943
    closed_total_cost = 0.101369
    closed_reason = "stop_loss"
    net_rr_tp1 = 0.65
    net_rr_tp2 = 2.65
    net_pnl_tp1 = 0.5
    net_pnl_tp2 = 2.03
    net_pnl_stop = -0.77
    entry_zone_json = {"from": 72.46, "to": 72.90}
    tp_json = {"tp1": 72.08, "tp2": 70.56}
    opened_at = closed_at = created_at = None

    plan_json = {
        "regime": "trend_down_candidate",
        "trade_mode": "trend",
        "radar_state": "none",
        "entry_depth": {"obi": -0.18},
        "entry_reason": "mtf_trend_down_neutral_normal_structure_confirmed",
        "config": {"fingerprint": "d9652df1c2af"},
        "tz_shadow": {
            "evaluated": True, "would_pass": False,
            "failed": ["adx_below_min:19.4", "stoch_not_in_pullback:15.5"],
            "adx": 19.4295, "di_spread": 11.0984,
            "stoch_k": 15.5152, "stoch_d": 39.7395, "obv_vs_ema": -11188.3,
        },
        "trend_trigger": {"allowed": True, "extension_atr": -0.7609},
        "setup_reach": {"applied": False, "reason": "disabled"},
        "lifecycle": {
            "entry_price": 72.684, "exit_price": 73.384574,
            "mfe_pct": 0.3744, "mae_pct": -0.9135,
            "traj": [[0, 0.0], [767, 0.3744], [2448, -0.9135]],
            "traj_step": 0.05, "close_reason": "stop_loss",
            "positive_then_negative": True, "went_positive": True,
        },
    }


@pytest.fixture
def logged(tmp_path, monkeypatch):
    logger = MLTradeLogger()
    monkeypatch.setattr(logger, "path", tmp_path / "outcomes.jsonl", raising=False)
    logger.log_closed_signal(_Signal(), known_logged_ids=set())
    line = (tmp_path / "outcomes.jsonl").read_text(encoding="utf-8").strip()
    return json.loads(line)


def test_gates_block_exists(logged):
    assert isinstance(logged.get("gates"), dict), (
        "без блока gates режим наблюдения не производит данных"
    )


def test_tz_shadow_survives_to_the_export(logged):
    """Именно это поле искал tz_calibrate.py и не находил."""
    shadow = logged["gates"]["tz_shadow"]
    assert shadow["evaluated"] is True
    assert shadow["adx"] == pytest.approx(19.4295)
    assert "adx_below_min:19.4" in shadow["failed"]


def test_measurable_values_not_just_the_verdict(logged):
    """Для калибровки нужен САМ ADX, а не только «условие не прошло».

    Порог назначается по распределению значений. Хранить один булев вердикт
    означало бы зафиксировать текущий порог навсегда: по нему нельзя посчитать,
    что было бы при другом.
    """
    shadow = logged["gates"]["tz_shadow"]
    for field in ("adx", "stoch_k", "di_spread", "obv_vs_ema"):
        assert shadow.get(field) is not None, field


def test_trend_trigger_extension_survives(logged):
    """TREND_MAX_EXTENSION_ATR тоже ждёт калибровки по этому полю."""
    assert logged["gates"]["trend_trigger"]["extension_atr"] == pytest.approx(-0.7609)


def test_entry_reason_survives(logged):
    """Без причины входа разворот от поддержки и пробой лежат в куче «trend»."""
    assert logged["gates"]["entry_reason"].startswith("mtf_trend_down")


def test_config_fingerprint_survives(logged):
    """Чтобы сделки до и после правок не смешивались незаметно."""
    assert logged["gates"]["config_fingerprint"] == "d9652df1c2af"


def test_outcome_is_still_there(logged):
    """Вердикт без итога не калибрует ничего — нужны обе половины пары."""
    assert logged["closed_net_pnl"] == pytest.approx(-0.801943)
    assert logged["lifecycle"]["traj"]


def test_missing_gates_do_not_break_logging(tmp_path, monkeypatch):
    """Сигнал без гейтов (скальп, старый план) обязан логироваться как прежде."""
    signal = _Signal()
    signal.plan_json = {"regime": "scalp", "trade_mode": "scalp", "lifecycle": {}}
    logger = MLTradeLogger()
    monkeypatch.setattr(logger, "path", tmp_path / "o.jsonl", raising=False)
    logger.log_closed_signal(signal, known_logged_ids=set())
    row = json.loads((tmp_path / "o.jsonl").read_text(encoding="utf-8").strip())
    assert row["gates"]["tz_shadow"] is None
    assert row["symbol"] == "SOL/USDT"
