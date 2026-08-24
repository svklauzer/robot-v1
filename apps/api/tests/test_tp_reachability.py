"""Достижимость награды (#tp-reachability-2026-08-03, переписан 24.08.2026).

`net_rr_tp2` считается ОТ ЦЕЛИ, и гейт входа `min_rr_tp2` пропускает сделку по
геометрии. Если до цели почти никогда не доходят, геометрии не существует.

Прежняя версия сравнивала дистанцию TP1 с медианой MFE и требовала ≤ 1.5. Тесты
ниже закрепляют, почему так больше нельзя:

* медиана MFE по всем закрытым при винрейте 36.75% описывает ПРОИГРАВШИХ
  (24.08: победители 2.2–3.1%, проигравшие 0.00–0.05%);
* `TP1_MIN_PCT=0.6` при потолке 1.5 давал безусловный замок — гейт проходим
  только при медиане ≥ 0.4%, а ближе 0.6% цель не поставить. ETH 24.08:
  медиана 0.3154% при ATR 1h 0.95%, блокировка каждый скан, выборка расти не
  могла, потому что росла только от закрытых сделок.
"""
from __future__ import annotations

import json

import pytest

from core.config import settings
from services import tp_reachability as tr


@pytest.fixture(autouse=True)
def _clear_cache():
    tr._CACHE["ts"] = 0.0
    tr._CACHE["by_key"] = {}
    tr._CACHE["by_regime"] = {}
    yield
    tr._CACHE["ts"] = 0.0


@pytest.fixture(autouse=True)
def _enforce(monkeypatch):
    monkeypatch.setattr(settings, "TP_REACH_MODE", "enforce", raising=False)
    monkeypatch.setattr(settings, "TP_REACH_EV_MARGIN", 1.0, raising=False)
    monkeypatch.setattr(settings, "TP_REACH_MIN_SAMPLE", 15, raising=False)


def _seed(monkeypatch, tmp_path, rows):
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    class _Logger:
        def __init__(self):
            self.path = str(path)

    monkeypatch.setattr("services.ml_trade_logger.MLTradeLogger", _Logger)


def _rows(n, mfe, symbol="SOL/USDT", regime="trend_up_candidate"):
    return [{"symbol": symbol, "regime": regime, "lifecycle": {"mfe_pct": mfe}}
            for _ in range(n)]


# ── порог выводится из плана, а не подбирается ──────────────────────────────
def test_threshold_comes_from_the_claimed_rr(monkeypatch, tmp_path):
    """Одна и та же частота: смелый RR проходит, скромный — нет.

    Награда R окупается, если сбывается чаще 1/(1+R). До цели доходят 30%:
    для RR 2.9 нужно 26% — хватает; для RR 1.3 нужно 43% — не хватает.
    Никакого подбираемого числа, порог диктует сам план.
    """
    _seed(monkeypatch, tmp_path, _rows(30, 2.5) + _rows(70, 0.5))

    bold = tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                       tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=2.9)
    assert bold.tp2_hit_rate == pytest.approx(0.30)
    assert bold.required_hit_rate == pytest.approx(0.2564, abs=0.001)
    assert bold.allowed is True

    modest = tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                         tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=1.3)
    assert modest.required_hit_rate == pytest.approx(0.4348, abs=0.001)
    assert modest.allowed is False
    assert modest.reason.startswith("tp2_reached_too_rarely")


def test_shrinking_the_target_does_not_buy_passage(monkeypatch, tmp_path):
    """Подогнать цель под гейт нельзя — приём, которым уже пользовались.

    `SCALP_TARGET_PCT` понижали 0.8 → 0.5 ровно ради прохождения прежнего
    гейта. Теперь ближняя цель поднимает частоту, но опускает RR, а с ним
    поднимает требуемую частоту: при стопе 1% обе версии отвергаются.
    """
    _seed(monkeypatch, tmp_path, _rows(10, 4.0) + _rows(10, 2.0) + _rows(80, 0.5))
    stop_pct = 1.0

    far = tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                      tp1_dist_pct=0.9, tp2_dist_pct=3.0, net_rr_tp2=3.0 / stop_pct)
    near = tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                       tp1_dist_pct=0.6, tp2_dist_pct=1.5, net_rr_tp2=1.5 / stop_pct)

    assert far.tp2_hit_rate == pytest.approx(0.10)
    assert near.tp2_hit_rate == pytest.approx(0.20)   # частота выросла вдвое
    assert near.required_hit_rate > far.required_hit_rate  # и требование тоже
    assert far.allowed is False and near.allowed is False


# ── замок узкой выборки ─────────────────────────────────────────────────────
def test_symbol_sample_cannot_lock_an_instrument_out(monkeypatch, tmp_path):
    """Боевой случай ETH 24.08.

    Своя выборка символа показывает крошечный ход (наши же ранние выходы её и
    обрезали), до цели не доходит НИ ОДНА сделка. Раньше это блокировало вход
    навсегда: без входов выборка не растёт, отказ подтверждает сам себя.
    Теперь отказ узкой выборки перепроверяется по режиму.
    """
    _seed(monkeypatch, tmp_path,
          _rows(20, 0.32, symbol="ETH/USDT") + _rows(40, 2.5, symbol="XRP/USDT"))

    out = tr.evaluate(symbol="ETH/USDT", regime="trend_up_candidate",
                      tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=2.9)

    assert out.symbol_sample_overridden is True
    assert out.source == "regime_override"
    assert out.allowed is True


def test_block_stands_when_both_samples_refuse(monkeypatch, tmp_path):
    """Страховка не превращает гейт в выключенный: отказали обе — блокируем."""
    _seed(monkeypatch, tmp_path,
          _rows(20, 0.32, symbol="ETH/USDT") + _rows(40, 0.5, symbol="XRP/USDT"))

    out = tr.evaluate(symbol="ETH/USDT", regime="trend_up_candidate",
                      tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=2.9)

    assert out.allowed is False
    assert out.symbol_sample_overridden is False


def test_old_ratio_deadlock_is_gone(monkeypatch, tmp_path):
    """Арифметика прежнего замка больше не воспроизводится.

    Медиана 0.32% и TP1 на 0.9% давали отношение 2.8 при потолке 1.5 — отказ
    при ЛЮБОЙ достижимой цели, ведь ближе TP1_MIN_PCT=0.6 её не поставить.
    Сейчас вердикт выносится по TP2 и частоте, и такая сделка проходит.
    """
    _seed(monkeypatch, tmp_path,
          _rows(20, 0.32, symbol="ETH/USDT") + _rows(40, 2.5, symbol="XRP/USDT"))

    out = tr.evaluate(symbol="ETH/USDT", regime="trend_up_candidate",
                      tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=2.9)

    assert out.median_mfe_pct is not None
    assert out.allowed is True
    assert float(settings.TP1_MIN_PCT) == 0.6  # пол коридора остался прежним


# ── какое плечо решает ──────────────────────────────────────────────────────
def test_verdict_is_about_tp2_and_tp1_is_only_recorded(monkeypatch, tmp_path):
    """TP1 — точка частичной фиксации, а не награда, и входу не мешает.

    До TP1 доходят ЧАЩЕ, чем до TP2 (цель ближе), поэтому прежний гейт судил по
    более лёгкому плечу, а решение принималось по более трудному.
    """
    _seed(monkeypatch, tmp_path, _rows(30, 2.5) + _rows(70, 1.0))

    out = tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                      tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=2.9)

    assert out.tp1_hit_rate == pytest.approx(1.0)
    assert out.tp2_hit_rate == pytest.approx(0.30)
    assert out.tp1_hit_rate >= out.tp2_hit_rate
    assert out.reason == "reward_reached_often_enough"


# ── предохранители ──────────────────────────────────────────────────────────
def test_small_sample_does_not_block(monkeypatch, tmp_path):
    """Частота по трём сделкам — не частота."""
    _seed(monkeypatch, tmp_path, _rows(3, 0.4))

    out = tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                      tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=2.9)
    assert out.allowed is True
    assert out.reason.startswith("sample_too_small")


def test_missing_geometry_passes(monkeypatch, tmp_path):
    """Нет TP2 или RR — судить не о чем, пропускаем."""
    _seed(monkeypatch, tmp_path, _rows(40, 0.4))

    assert tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                       tp1_dist_pct=0.9, tp2_dist_pct=None, net_rr_tp2=2.9).allowed
    assert tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                       tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=None).allowed
    assert tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                       tp1_dist_pct=None, tp2_dist_pct=2.0, net_rr_tp2=2.9).allowed


def test_no_data_passes(monkeypatch, tmp_path):
    """Отсутствие статистики не должно выглядеть как «цель недостижима»."""
    _seed(monkeypatch, tmp_path, [])

    out = tr.evaluate(symbol="NEW/USDT", regime="trend_up_candidate",
                      tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=2.9)
    assert out.allowed is True
    assert out.evaluated is False


def test_shadow_mode_blocks_nothing_but_measures(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, _rows(100, 0.5))
    monkeypatch.setattr(settings, "TP_REACH_MODE", "shadow", raising=False)

    out = tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                      tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=2.9)
    assert out.allowed is True
    assert out.reason == "mode_shadow"
    assert out.tp2_hit_rate == pytest.approx(0.0)  # наблюдать всё равно есть что


def test_negative_and_zero_mfe_ignored(monkeypatch, tmp_path):
    """Сделка без хода в свою сторону не говорит ничего о достижимости цели."""
    _seed(monkeypatch, tmp_path, _rows(40, 2.5) + _rows(20, 0.0))

    out = tr.evaluate(symbol="SOL/USDT", regime="trend_up_candidate",
                      tp1_dist_pct=0.9, tp2_dist_pct=2.0, net_rr_tp2=2.9)
    assert out.sample == 40
