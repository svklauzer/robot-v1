"""Контракт геометрии от фактического разброса сетапа (#setup-reach-2026-07-30).

Проверяется то, из-за чего движок терял деньги: цель, до которой сетап не
доходит, и стоп внутри обычного шума. И то, что правка не может навредить —
уровни двигаются только внутрь, структурные режимы не трогаются, сбой расчёта
не останавливает торговлю.
"""
from __future__ import annotations

import pytest

from core.config import settings
from services.setup_reach import (
    STRUCTURAL_REGIMES,
    ReachProfile,
    SetupReachService,
    apply_geometry,
)


class _Signal:
    def __init__(self, regime: str, mfe: float, mae: float):
        self.plan_json = {"regime": regime, "lifecycle": {"mfe_pct": mfe, "mae_pct": -abs(mae)}}
        self.status = "closed"


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def order_by(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def all(self):
        return self._rows


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_a, **_k):
        return _Query(self._rows)


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    # Правило выключено по умолчанию: гипотеза проверена и отвергнута
    # (см. SETUP_REACH_ENABLED в config.py). Тесты описывают контракт самого
    # механизма — на случай повторной проверки на большей выборке — поэтому
    # включают его явно.
    monkeypatch.setattr(settings, "SETUP_REACH_ENABLED", True, raising=False)
    SetupReachService.invalidate()
    yield
    SetupReachService.invalidate()


def _service():
    return SetupReachService(now=0.0)


def _rows(regime: str, n: int, mfe: float, mae: float):
    return [_Signal(regime, mfe, mae) for _ in range(n)]


# ── измерение ───────────────────────────────────────────────────────────────
def test_target_and_stop_come_from_measured_distribution():
    rows = _rows("trend_up_candidate", 40, mfe=0.50, mae=0.75)
    profile = _service().profile(_DB(rows), "trend_up_candidate")
    assert profile.applies
    assert profile.target_pct == pytest.approx(0.50)
    assert profile.stop_pct == pytest.approx(0.75)


def test_short_history_does_not_touch_geometry():
    rows = _rows("trend_up_candidate", 5, mfe=0.50, mae=0.75)
    profile = _service().profile(_DB(rows), "trend_up_candidate")
    assert not profile.applies
    assert profile.reason == "insufficient_history"


def test_structural_regimes_are_never_rewritten():
    """CRT и reversal берут уровни из структуры рынка — хвоста свипа и опоры.

    Подмена структурного стопа квантильным на прогоне УХУДШАЛА результат
    (crt −1.53%): стоп за хвостом C2 несёт смысл, которого нет в квантиле.
    """
    for regime in STRUCTURAL_REGIMES:
        SetupReachService.invalidate()
        profile = _service().profile(_DB(_rows(regime, 60, 1.0, 0.3)), regime)
        assert not profile.applies
        assert profile.reason == "structural_levels"


def test_target_below_cost_floor_is_left_alone():
    """Сетап, не окупающий round-trip, не получает «удобно близкую» цель.

    Решение не торговать принимает гейт явно; сервис не имеет права протащить
    сделку, выставив тейк внутри комиссии."""
    rows = _rows("scalp", 40, mfe=0.05, mae=0.30)
    profile = _service().profile(_DB(rows), "scalp")
    assert not profile.applies
    assert profile.reason == "target_below_cost_floor"


def test_disabled_flag(monkeypatch):
    monkeypatch.setattr(settings, "SETUP_REACH_ENABLED", False, raising=False)
    profile = _service().profile(_DB(_rows("scalp", 60, 0.5, 0.3)), "scalp")
    assert not profile.applies


def test_db_failure_is_fail_open():
    class _Broken:
        def query(self, *_a, **_k):
            raise RuntimeError("db down")

    assert not _service().profile(_Broken(), "scalp").applies


# ── применение уровней ──────────────────────────────────────────────────────
def _profile(target: float, stop: float) -> ReachProfile:
    return ReachProfile(regime="trend_up_candidate", sample=40, applies=True,
                        reason="empirical_reach", target_pct=target, stop_pct=stop,
                        mfe_median_pct=target, mae_median_pct=stop)


def test_long_levels_are_pulled_in_to_measured_reach():
    stop, tp1, tp2, report = apply_geometry(
        side="long", entry_price=100.0, stop_price=98.8, tp1=101.2, tp2=103.0,
        profile=_profile(target=0.60, stop=0.80),
    )
    assert report["applied"]
    assert stop == pytest.approx(99.2)    # −0.80%
    assert tp1 == pytest.approx(100.6)    # +0.60%
    assert tp2 < 103.0                    # TP2 сжимается пропорционально TP1
    assert tp2 > tp1


def test_short_levels_mirror_correctly():
    stop, tp1, tp2, report = apply_geometry(
        side="short", entry_price=100.0, stop_price=101.2, tp1=98.8, tp2=97.0,
        profile=_profile(target=0.60, stop=0.80),
    )
    assert report["applied"]
    assert stop == pytest.approx(100.8)
    assert tp1 == pytest.approx(99.4)
    assert tp2 < tp1 < 100.0


def test_levels_move_only_inward_never_widen_risk():
    """Стоп нельзя расширить статистикой.

    План и гейты одобрили сделку с конкретным риском; раздвинуть стоп после
    этого значит увеличить убыток, который никто не согласовывал.
    """
    stop, tp1, _tp2, report = apply_geometry(
        side="long", entry_price=100.0, stop_price=99.7, tp1=100.4, tp2=101.0,
        profile=_profile(target=2.00, stop=2.00),
    )
    assert report["applied"]
    assert stop == pytest.approx(99.7)   # исходный стоп ближе — оставлен
    assert tp1 == pytest.approx(100.4)   # исходная цель ближе — оставлена


def test_neutral_profile_leaves_levels_untouched():
    stop, tp1, tp2, report = apply_geometry(
        side="long", entry_price=100.0, stop_price=98.0, tp1=102.0, tp2=104.0,
        profile=ReachProfile("crt", 0, False, "structural_levels", None, None, None, None),
    )
    assert not report["applied"]
    assert (stop, tp1, tp2) == (98.0, 102.0, 104.0)


def test_geometry_resolves_the_original_failure():
    """Регресс на исходную поломку: цель 1.215% при MFE 0.529%.

    После правки цель обязана оказаться внутри того, куда сетап реально
    доходит, а стоп — не ближе цели, иначе сделка снова разрешается только
    в одну сторону.
    """
    stop, tp1, _tp2, report = apply_geometry(
        side="long", entry_price=100.0, stop_price=98.782, tp1=101.215, tp2=103.0,
        profile=_profile(target=0.75, stop=0.86),
    )
    assert report["applied"]
    reach_pct = (tp1 - 100.0)
    risk_pct = (100.0 - stop)
    assert reach_pct == pytest.approx(0.75)
    assert risk_pct == pytest.approx(0.86)
    assert reach_pct < 0.529 * 2  # цель больше не вдвое дальше медианного MFE
