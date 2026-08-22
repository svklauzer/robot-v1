"""Разрез edge по поколениям конфига (#edge-by-fingerprint-2026-08-22).

Скрипт `scripts/edge_by_fingerprint.py` отвечает на вопрос «работает ли эта
настройка», и ошибиться в нём дороже, чем в самой настройке: неверный разрез
даёт уверенный ответ на неправильно заданный вопрос.

Повод: 13 закрытых на 22.08 выглядели одной выборкой, а были ДВУМЯ
конфигурациями (`tz_adx_min` 18 и 15). Среднее по ним измеряло среднее по двум
разным системам.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import edge_by_fingerprint as ebf  # noqa: E402


def _row(sid, fp, net, cost=0.3, created="2026-08-20T10:00:00", adx_min=15.0,
         status="closed", regime="trend_up_candidate"):
    return {
        "id": sid,
        "status": status,
        "created_at": created,
        "closed_net_pnl": net,
        "closed_total_cost": cost,
        "rationale": "intelligence_mtf_trend_up",
        "plan": {
            "regime": regime,
            "config": {
                "fingerprint": fp,
                "guards": {"tz_adx_min": adx_min, "tz_mode": "enforce"},
            },
        },
    }


def test_gross_is_net_plus_cost_counted_once():
    """Издержки прибавляются РОВНО один раз.

    `closed_net_pnl` уже нетто, `closed_total_cost` — то, что из него вычли.
    Смешение этих единиц однажды дало ложноположительный результат на целой
    гипотезе, поэтому обе величины берутся из одной строки.
    """
    st = ebf._stats([_row(1, "aaa", net=1.0, cost=0.3)])
    assert st["net"] == pytest.approx(1.0)
    assert st["gross"] == pytest.approx(1.3)


def test_generations_are_not_pooled():
    """Две конфигурации не должны сливаться в одну выборку."""
    rows = [
        _row(1, "old", net=+2.5, adx_min=18.0),
        _row(2, "old", net=+0.5, adx_min=18.0),
        _row(3, "new", net=-3.0, adx_min=15.0),
        _row(4, "new", net=+1.0, adx_min=15.0),
    ]
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(ebf._fingerprint(row), []).append(row)

    assert set(groups) == {"old", "new"}
    assert ebf._stats(groups["old"])["net"] == pytest.approx(3.0)
    assert ebf._stats(groups["new"])["net"] == pytest.approx(-2.0)


def test_sign_flip_between_halves_is_detected():
    """Разрез, меняющий знак между половинами, обязан помечаться нестабильным.

    Это и есть та дисциплина, ради которой скрипт написан: 11 сделок с
    −1.30 в первой половине и +2.00 во второй — шум, а не находка.
    """
    rows = [_row(i, "fp", net=v) for i, v in enumerate([-1.0, -1.0, +2.0, +2.0])]
    st = ebf._stats(rows)
    assert st["sign_stable"] is False

    rows_ok = [_row(i, "fp", net=v) for i, v in enumerate([1.0, 1.0, 1.0, 1.0])]
    assert ebf._stats(rows_ok)["sign_stable"] is True


def test_config_diff_shows_only_changed_keys():
    """Диф настроек — ответ на «что вообще менялось».

    Без него отпечаток остаётся хэшем, по которому нельзя понять, что проверяем.
    """
    old = ebf._flatten(ebf._config(_row(1, "old", 0.0, adx_min=18.0)))
    new = ebf._flatten(ebf._config(_row(2, "new", 0.0, adx_min=15.0)))

    changed = [k for k in set(old) | set(new) if old.get(k) != new.get(k)]

    assert changed == ["guards.tz_adx_min"]
    assert "fingerprint" not in old  # сам отпечаток из дифа исключён


def test_grade_thresholds_do_not_split_a_generation():
    """Одна конфигурация не должна дробиться по грейдам сделок.

    `decision_config` кладёт в снимок фактические пороги production_gate, а они
    зависят от ГРЕЙДА (A+/A/B). В сыром fingerprint это давало три «поколения»
    вместо одного: на выгрузке 03.08 пять групп жили одновременно 29.07–02.08,
    а min_setup скакал 65↔58 не по датам, а по грейду.
    """
    a_plus = _row(1, "fp-a", net=1.0)
    a_plus["plan"]["config"]["entry_gate"] = {"thresholds": {"min_setup": 65.0}}
    grade_b = _row(2, "fp-b", net=1.0)
    grade_b["plan"]["config"]["entry_gate"] = {"thresholds": {"min_setup": 58.0}}

    # сырые отпечатки разные, системный ключ — один
    assert ebf._fingerprint(a_plus) != ebf._fingerprint(grade_b)
    assert ebf._system_key(a_plus) == ebf._system_key(grade_b)


def test_per_trade_parameters_do_not_split_a_generation():
    """Скальп и направленная сделка при одном конфиге — одно поколение.

    `decision_config.snapshot()` принимает `is_scalp` и `fee_rate` и кладёт их
    в тот же словарь, что и глобальные настройки. На выгрузке 03.08 это дало
    девять «поколений» на 44 сделки, из которых пять жили одновременно, —
    а реальное отличие было ровно одно (`max_trades_per_day` 3 → 100).
    """
    scalp = _row(1, "fp-scalp", net=1.0)
    scalp["plan"]["config"].update({
        "sizing": {"max_position_margin_pct": 0.20},
        "anti_drain": {"min_net_rr_tp1": 0.4},
        "market": {"market_type": "spot", "taker_fee": 0.002},
    })
    trend = _row(2, "fp-trend", net=1.0)
    trend["plan"]["config"].update({
        "sizing": {"max_position_margin_pct": 0.13},
        "anti_drain": {"min_net_rr_tp1": 0.1},
        "market": {"market_type": "swap", "taker_fee": 0.0005},
    })

    assert ebf._system_key(scalp) == ebf._system_key(trend)


def test_real_setting_change_still_splits_generations():
    """А вот настройка системы обязана разводить поколения."""
    old = _row(1, "fp", net=1.0, adx_min=18.0)
    new = _row(2, "fp", net=1.0, adx_min=15.0)
    assert ebf._system_key(old) != ebf._system_key(new)


def test_rows_without_result_are_ignored():
    """Открытая сделка не должна попадать в статистику как ноль."""
    assert ebf._money({"closed_net_pnl": None}) is None
    assert ebf._stats([{"closed_net_pnl": None}])["n"] == 0


def test_engine_detection_uses_regime_first():
    assert ebf._engine(_row(1, "fp", 0.0, regime="scalp")) == "scalp"
    assert ebf._engine(_row(1, "fp", 0.0, regime="crt")) == "crt"
    assert ebf._engine(_row(1, "fp", 0.0, regime="trend_up_candidate")) == "trend"
