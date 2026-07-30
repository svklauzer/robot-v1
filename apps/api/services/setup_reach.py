"""Цель и стоп — из фактического разброса сетапа (#setup-reach-2026-07-30).

Диагноз, из которого выросла эта правка
---------------------------------------
По 302 закрытым сделкам (издержки пересчитаны по фактическому маршруту) видно,
что трендовый движок терял не из-за направления и не из-за ведения, а из-за
ГЕОМЕТРИИ: цель ставилась туда, куда сетап не ходит, а стоп — туда, куда он
ходит в обычном шуме.

    режим                     TP1     SL    факт. MFE  MAE   TP1 достигнут
    trend_up_candidate      1.215%  1.218%    0.529%  0.753%      14%
    trend_down_candidate    1.209%  1.126%    0.732%  0.673%      31%
    scalp                   0.801%  0.349%    0.305%  0.179%       8%
    reversal_long_candidate 1.200%  0.420%    0.865%  0.224%      53%

Трендовая сделка получает цель 1.215% на сетапе, у которого медианный ход в
свою сторону 0.529% — цель в 2.3 раза дальше, чем сетап вообще доходит, и
достигается в 14% случаев. Стоп при этом стоит на 1.218% при медианном ходе
против 0.753%, то есть внутри обычного шума. У сделки остаётся ровно один
достижимый исход, и это стоп. Отсюда и `positive_then_negative` 60%, за
которым все гонялись: это не проблема выхода, это следствие недостижимой цели.

Для сравнения — `reversal_long_candidate`, единственный сетап со статистически
значимым edge (+0.633% на 6 ч, 95% ДИ [+0.187; +1.131]): стоп вдвое ближе
цели, MFE 0.865% против цели 1.200%, TP1 берётся в 53% случаев. Геометрия
согласована с разбросом — сделка разрешается в обе стороны.

Причина расхождения — в источнике уровней. Трендовый и скальп-путь строят
стоп и цель ФОРМУЛОЙ (ATR + support/resistance таймфрейма), не сверяясь с
тем, как этот класс сетапов реально ходит. CRT и reversal берут уровни из
структуры рынка (хвост свипа C2, опора разворота) — там формула не при чём, и
статистику к ним применять нельзя: у CRT замена структурного стопа на
квантильный результат УХУДШАЕТ (−1.53% на прогоне). Поэтому сервис применяется
только к формульному пути.

Что делает сервис
-----------------
Считает по закрытым сделкам эмпирическое распределение MFE и MAE для каждого
режима и отдаёт два числа:

    цель = квантиль SETUP_REACH_TP_QUANTILE распределения MFE
    стоп = квантиль SETUP_REACH_SL_QUANTILE распределения MAE

Цель по построению достижима заявленной долей случаев, стоп по построению
стоит за пределами обычного шума. Оба числа — измерения, а не подобранные
константы.

РЕЗУЛЬТАТ ПРОВЕРКИ: ГИПОТЕЗА ОТВЕРГНУТА
---------------------------------------
Правило выключено по умолчанию (`SETUP_REACH_ENABLED=False`). Ниже — почему,
потому что отрицательный результат здесь ценнее правки.

Первый прогон по фактическим траекториям (квантили считались только по
предыдущим закрытиям того же режима) показывал −28.03% → −16.24% и улучшение
на всех 15 комбинациях квантилей. Это оказалось ошибкой измерения: базовая
линия облагалась издержками ДВАЖДЫ. `lifecycle.final_result_pct` записан уже
НЕТТО (сверка: `closed_net_pnl == notional × final_result_pct/100`, расхождение
по 302 сделкам ровно равно сумме издержек), а скрипт вычитал round-trip ещё
раз. Траектория же (`lifecycle.traj`) — валовая, и там вычитание корректно.
Сравнивались величины в разных единицах.

После исправления знак меняется на противоположный:

    82 сделки, издержки учтены один раз с каждой стороны
    −14.28 → −19.53 USDT  (−5.25)
      trend_up_candidate   −3.71 →  −8.49
      scalp                −2.31 →  −2.82
      trend_down_candidate −7.10 →  −7.28
      range                −1.15 →  −0.94
    ХУЖЕ базы на всех 15 комбинациях квантилей (TP p45…p80 × SL p60…p90).

Почему — понятно постфактум, и это важнее самой правки. Систему держат хвосты:
`tp2_reached` даёт +3.55 USDT на сделку, `trend_ride_trailing_stop` +2.99,
`adaptive_post_tp1_stop` +1.99. Цель, обрезанная по p60 MFE, чаще забирает
мелкую прибыль, но именно этим убивает сделки, на которых всё держится.
«Недостижимая цель» — не дефект, а плата за право доехать до хвоста. Медиана
MFE 0.529% описывает типичную сделку, а зарабатывает нетипичная.

Что из этого остаётся полезным
------------------------------
Измерения. `SetupReachService.table()` даёт эмпирическое распределение MFE/MAE
по режимам — это то, чем стоит проверять любую следующую гипотезу о геометрии,
и то, чего в системе раньше не было. Сама перезапись уровней остаётся
доступной под флагом для повторной проверки на большей выборке, но включать её
на текущих данных оснований нет.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal

# Режимы, у которых уровни берутся из СТРУКТУРЫ рынка, а не из формулы.
# Их геометрия несёт смысл (хвост свипа, опора разворота) и статистикой не
# заменяется: на прогоне подмена ухудшала результат.
STRUCTURAL_REGIMES = frozenset({"crt", "reversal_long_candidate"})


@dataclass(frozen=True)
class ReachProfile:
    regime: str
    sample: int
    applies: bool
    reason: str
    target_pct: float | None   # ход в свою сторону, % от цены входа
    stop_pct: float | None     # ход против, % от цены входа
    mfe_median_pct: float | None
    mae_median_pct: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(q * len(ordered)), len(ordered) - 1)
    return float(ordered[index])


def _neutral(regime: str, reason: str, sample: int = 0) -> ReachProfile:
    return ReachProfile(
        regime=regime, sample=sample, applies=False, reason=reason,
        target_pct=None, stop_pct=None, mfe_median_pct=None, mae_median_pct=None,
    )


class SetupReachService:
    """Эмпирический разброс сетапа по режимам. Кэшируется — торговый цикл идёт
    раз в 60 с, а полный скан закрытых сделок стоит дороже скана рынка."""

    _cache: dict[str, ReachProfile] = {}
    _cache_at: float = 0.0

    def __init__(self, *, now: float | None = None):
        self._now = now

    def _compute(self, db: Session, *, bot_id: int | None) -> dict[str, ReachProfile]:
        window_hours = float(getattr(settings, "SETUP_REACH_WINDOW_HOURS", 720.0))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

        query = db.query(Signal).filter(
            Signal.status == "closed",
            Signal.closed_at.isnot(None),
            Signal.closed_at >= cutoff,
        )
        if bot_id is not None:
            query = query.filter(Signal.bot_id == bot_id)
        rows = query.order_by(Signal.id.desc()).limit(
            int(getattr(settings, "SETUP_REACH_MAX_ROWS", 2000))
        ).all()

        buckets: dict[str, tuple[list[float], list[float]]] = {}
        for signal in rows:
            plan = signal.plan_json or {}
            regime = str(plan.get("regime") or "").strip()
            lifecycle = plan.get("lifecycle") or {}
            mfe = lifecycle.get("mfe_pct")
            mae = lifecycle.get("mae_pct")
            if not regime or mfe is None:
                continue
            try:
                mfe_value = float(mfe)
                mae_value = abs(float(mae or 0.0))
            except (TypeError, ValueError):
                continue
            mfes, maes = buckets.setdefault(regime, ([], []))
            mfes.append(mfe_value)
            maes.append(mae_value)

        min_history = int(getattr(settings, "SETUP_REACH_MIN_HISTORY", 15))
        tp_q = float(getattr(settings, "SETUP_REACH_TP_QUANTILE", 0.60))
        sl_q = float(getattr(settings, "SETUP_REACH_SL_QUANTILE", 0.75))
        floor_pct = float(getattr(settings, "SETUP_REACH_MIN_TARGET_PCT", 0.25))

        profiles: dict[str, ReachProfile] = {}
        for regime, (mfes, maes) in buckets.items():
            if regime in STRUCTURAL_REGIMES:
                profiles[regime] = _neutral(regime, "structural_levels", len(mfes))
                continue
            if len(mfes) < min_history:
                profiles[regime] = _neutral(regime, "insufficient_history", len(mfes))
                continue

            target = _quantile(mfes, tp_q)
            stop = _quantile(maes, sl_q)
            if target is None or stop is None or stop <= 0:
                profiles[regime] = _neutral(regime, "no_distribution", len(mfes))
                continue

            # Цель ниже порога окупаемости — это не «узкая цель», это сетап,
            # который не покрывает собственные издержки. Геометрию не трогаем:
            # решение не торговать принимается гейтом явно, а не тем, что
            # сервис молча выставит недостижимо близкий тейк.
            if target < floor_pct:
                profiles[regime] = _neutral(regime, "target_below_cost_floor", len(mfes))
                continue

            profiles[regime] = ReachProfile(
                regime=regime,
                sample=len(mfes),
                applies=True,
                reason="empirical_reach",
                target_pct=round(target, 4),
                stop_pct=round(stop, 4),
                mfe_median_pct=round(_quantile(mfes, 0.5) or 0.0, 4),
                mae_median_pct=round(_quantile(maes, 0.5) or 0.0, 4),
            )
        return profiles

    def table(self, db: Session, *, bot_id: int | None = None,
              force: bool = False) -> dict[str, ReachProfile]:
        ttl = float(getattr(settings, "SETUP_REACH_CACHE_TTL_SEC", 300.0))
        now = self._now if self._now is not None else time.monotonic()
        if not force and type(self)._cache and (now - type(self)._cache_at) < ttl:
            return type(self)._cache
        try:
            table = self._compute(db, bot_id=bot_id)
        except Exception as exc:  # noqa: BLE001
            # Fail-open: не разрешаем отчётному запросу останавливать торговлю.
            print(f"[SETUP REACH ERROR] {exc}")
            return {}
        type(self)._cache = table
        type(self)._cache_at = now
        return table

    def profile(self, db: Session, regime: str | None, *,
                bot_id: int | None = None) -> ReachProfile:
        regime_value = str(regime or "").strip()
        if not regime_value:
            return _neutral("", "no_regime")
        if not bool(getattr(settings, "SETUP_REACH_ENABLED", True)):
            return _neutral(regime_value, "disabled")
        if regime_value in STRUCTURAL_REGIMES:
            return _neutral(regime_value, "structural_levels")
        return self.table(db, bot_id=bot_id).get(regime_value) \
            or _neutral(regime_value, "insufficient_history")

    @classmethod
    def invalidate(cls) -> None:
        cls._cache = {}
        cls._cache_at = 0.0


def apply_geometry(
    *,
    side: str,
    entry_price: float,
    stop_price: float,
    tp1: float,
    tp2: float,
    profile: ReachProfile,
) -> tuple[float, float, float, dict]:
    """Пересчёт уровней под измеренный разброс сетапа.

    Возвращает (stop, tp1, tp2, отчёт). TP2 сдвигается ПРОПОРЦИОНАЛЬНО TP1:
    вторая цель — это про то, как далеко тянуть удачную сделку, и её отношение
    к первой задано стратегией; статистика отвечает только за масштаб.

    Уровни двигаются ТОЛЬКО внутрь. Расширять стоп по статистике нельзя: это
    увеличило бы риск сделки против того, что посчитал план и одобрили гейты.
    """
    if not profile.applies or entry_price <= 0:
        return stop_price, tp1, tp2, {"applied": False, "reason": profile.reason}

    is_long = str(side or "").lower() in ("long", "buy")
    sign = 1.0 if is_long else -1.0

    old_stop_pct = abs(entry_price - stop_price) / entry_price * 100
    old_tp1_pct = abs(tp1 - entry_price) / entry_price * 100
    ratio = (abs(tp2 - entry_price) / entry_price * 100) / old_tp1_pct if old_tp1_pct > 0 else 2.0

    new_tp1_pct = min(profile.target_pct, old_tp1_pct)
    new_stop_pct = min(profile.stop_pct, old_stop_pct)
    new_tp2_pct = min(new_tp1_pct * ratio, abs(tp2 - entry_price) / entry_price * 100)

    new_stop = entry_price * (1 - sign * new_stop_pct / 100)
    new_tp1 = entry_price * (1 + sign * new_tp1_pct / 100)
    new_tp2 = entry_price * (1 + sign * new_tp2_pct / 100)

    return new_stop, new_tp1, new_tp2, {
        "applied": True,
        "reason": profile.reason,
        "regime": profile.regime,
        "sample": profile.sample,
        "stop_pct": {"was": round(old_stop_pct, 4), "now": round(new_stop_pct, 4)},
        "tp1_pct": {"was": round(old_tp1_pct, 4), "now": round(new_tp1_pct, 4)},
        "mfe_median_pct": profile.mfe_median_pct,
        "mae_median_pct": profile.mae_median_pct,
    }
