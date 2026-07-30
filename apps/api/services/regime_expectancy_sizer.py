"""Размер сделки от ФАКТИЧЕСКОГО ожидания режима (#regime-sizing-2026-07-30).

Зачем это нужно — на наших числах за 302 закрытые сделки (издержки пересчитаны
по фактическому маршруту, своп 0.15% round-trip):

    режим                      n     net USDT   риск R   ожидание
    trend_up_candidate        64      −36.75     ~218      −0.169 R
    trend_down_candidate      93       −8.78     ~301      −0.029 R
    scalp                     50       −3.16      ~26      −0.124 R
    crt                       41       +6.56      ~49      +0.134 R
    reversal_long_candidate   15      +10.06      ~35      +0.287 R

Убыток системы не размазан по всем входам — он сидит в одном режиме, и это
устойчиво: trend_up_candidate отрицателен на ОБЕИХ половинах выборки (−28.99 и
−7.76), тогда как любой другой разрез меняет знак между половинами, то есть
является подгонкой.

При этом сайзинг был устроен ровно наоборот: худший режим нёс САМЫЙ БОЛЬШОЙ
размер (медианный нотионал 186 USDT, риск 3.36 USDT на сделку) — просто потому,
что у трендовых сетапов шире стоп и своя ветка лимитов, а прибыльный CRT шёл
размером 100 / 1.20. Капитал систематически распределялся обратно ожиданию.

Почему множитель, а не чёрный список (TRADEABLE_REGIMES). Список — решение,
принятое один раз по прошлой выборке, и он не умеет ни отключаться, ни
включаться обратно: режим, переставший терять, останется запрещённым, а
режим, начавший терять завтра, останется разрешённым. Множитель — обратная
связь: он считается по скользящему окну, ужимает проигрывающий режим до
наблюдательного размера (`REGIME_EXP_MIN_MULT`, дефолт 0.15) и САМ возвращает
полный размер, когда ожидание вернётся в ноль. Наблюдательный размер важен:
при нулевом режим перестаёт давать данные и уже никогда не восстановится.

Ожидание считается В ЕДИНИЦАХ РИСКА, а не в USDT:

    expectancy_r = Σ closed_net_pnl / Σ |net_pnl_stop|

Иначе режим с мелким размером всегда выглядит «почти безубыточным» рядом с
режимом с крупным (scalp −3.16 USDT против trend_up −36.75 USDT — при том, что
на единицу риска scalp теряет сопоставимо). R-нормировка сравнивает сетапы, а
не размеры позиций, которые сама же и должна назначить.

Глубина среза шринкуется размером выборки — `n / (n + REGIME_EXP_PRIOR_N)`. На
20 сделках нельзя резать так же уверенно, как на 100; без шринкажа режим
получал бы полный штраф за случайную серию из трёх стопов.

Только сайзинг. Вход не блокируется никогда: гейты — отдельный слой, и решение
«не торговать» должно приниматься там явно, а не побочным эффектом множителя.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core.config import settings
from models.signal import Signal


@dataclass(frozen=True)
class RegimeSizing:
    regime: str
    multiplier: float
    reason: str
    sample: int
    expectancy_r: float | None
    net_pnl_usdt: float
    risk_usdt: float
    confidence: float

    def as_dict(self) -> dict:
        return asdict(self)


def _neutral(regime: str, reason: str, sample: int = 0) -> RegimeSizing:
    return RegimeSizing(
        regime=regime,
        multiplier=1.0,
        reason=reason,
        sample=sample,
        expectancy_r=None,
        net_pnl_usdt=0.0,
        risk_usdt=0.0,
        confidence=0.0,
    )


class RegimeExpectancySizer:
    """Множитель размера по режиму. Кэшируется: цикл идёт раз в 60 с, а полный
    скан закрытых сделок на basic-256mb Postgres стоит дороже, чем сам скан
    рынка (см. H5 аудита 28.07 — health-страница уже роняла инстанс)."""

    _cache: dict[str, RegimeSizing] = {}
    _cache_at: float = 0.0

    def __init__(self, *, now: float | None = None):
        self._now = now

    # ── расчёт ──────────────────────────────────────────────────────────────
    def _multiplier(self, expectancy_r: float, sample: int) -> tuple[float, str]:
        floor = float(getattr(settings, "REGIME_EXP_MIN_MULT", 0.15))
        floor_at = abs(float(getattr(settings, "REGIME_EXP_FLOOR_AT_R", 0.10)))
        prior_n = float(getattr(settings, "REGIME_EXP_PRIOR_N", 10.0))

        if expectancy_r >= 0:
            return 1.0, "expectancy_non_negative"

        # Насколько глубоко резать, если бы выборка была бесконечной.
        # 0 R → 1.0, −floor_at R и хуже → floor, между ними линейно.
        depth = min(abs(expectancy_r) / floor_at, 1.0) if floor_at > 0 else 1.0
        raw = 1.0 - depth * (1.0 - floor)

        # Шринкаж: короткая история не даёт права на полный штраф.
        confidence = sample / (sample + prior_n) if (sample + prior_n) > 0 else 0.0
        multiplier = 1.0 - confidence * (1.0 - raw)

        return round(max(floor, min(1.0, multiplier)), 4), "expectancy_negative"

    def _compute(self, db: Session, *, bot_id: int | None) -> dict[str, RegimeSizing]:
        window_hours = float(getattr(settings, "REGIME_EXP_WINDOW_HOURS", 720.0))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

        query = (
            db.query(Signal)
            .filter(
                Signal.status == "closed",
                Signal.closed_at.isnot(None),
                Signal.closed_at >= cutoff,
                Signal.closed_net_pnl.isnot(None),
            )
        )
        if bot_id is not None:
            query = query.filter(Signal.bot_id == bot_id)

        rows = query.order_by(Signal.id.desc()).limit(
            int(getattr(settings, "REGIME_EXP_MAX_ROWS", 2000))
        ).all()

        buckets: dict[str, list[Signal]] = {}
        for signal in rows:
            regime = str((signal.plan_json or {}).get("regime") or "").strip()
            if not regime:
                continue
            buckets.setdefault(regime, []).append(signal)

        min_history = int(getattr(settings, "REGIME_EXP_MIN_HISTORY", 20))
        result: dict[str, RegimeSizing] = {}

        for regime, signals in buckets.items():
            net = 0.0
            risk = 0.0
            counted = 0
            for signal in signals:
                try:
                    planned_risk = abs(float(signal.net_pnl_stop or 0.0))
                except (TypeError, ValueError):
                    planned_risk = 0.0
                if planned_risk <= 0:
                    # Без планового риска сделку нельзя выразить в R —
                    # молча считать её нулевой значит занизить знаменатель
                    # и раздуть ожидание. Исключаем из обеих сумм.
                    continue
                net += float(signal.closed_net_pnl or 0.0)
                risk += planned_risk
                counted += 1

            if counted < min_history or risk <= 0:
                result[regime] = _neutral(regime, "insufficient_history", counted)
                continue

            expectancy_r = net / risk
            multiplier, reason = self._multiplier(expectancy_r, counted)
            result[regime] = RegimeSizing(
                regime=regime,
                multiplier=multiplier,
                reason=reason,
                sample=counted,
                expectancy_r=round(expectancy_r, 6),
                net_pnl_usdt=round(net, 6),
                risk_usdt=round(risk, 6),
                confidence=round(counted / (counted + float(getattr(settings, "REGIME_EXP_PRIOR_N", 10.0))), 4),
            )

        return result

    # ── публичный API ───────────────────────────────────────────────────────
    def table(self, db: Session, *, bot_id: int | None = None,
              force: bool = False) -> dict[str, RegimeSizing]:
        ttl = float(getattr(settings, "REGIME_EXP_CACHE_TTL_SEC", 300.0))
        now = self._now if self._now is not None else time.monotonic()
        if not force and type(self)._cache and (now - type(self)._cache_at) < ttl:
            return type(self)._cache
        try:
            table = self._compute(db, bot_id=bot_id)
        except Exception as exc:  # noqa: BLE001
            # Fail-open: сайзинг не на критическом пути. Сорвать торговый цикл
            # из-за отчётного запроса нельзя — отдаём нейтральную таблицу.
            print(f"[REGIME EXPECTANCY ERROR] {exc}")
            return {}
        type(self)._cache = table
        type(self)._cache_at = now
        return table

    def evaluate(self, db: Session, regime: str | None, *,
                 bot_id: int | None = None) -> RegimeSizing:
        regime_value = str(regime or "").strip()
        if not regime_value:
            return _neutral("", "no_regime")
        if not bool(getattr(settings, "REGIME_EXP_SIZING_ENABLED", True)):
            return _neutral(regime_value, "disabled")
        table = self.table(db, bot_id=bot_id)
        return table.get(regime_value) or _neutral(regime_value, "insufficient_history")

    @classmethod
    def invalidate(cls) -> None:
        cls._cache = {}
        cls._cache_at = 0.0
