# apps/api/services/exposure_guard.py

from dataclasses import dataclass
from sqlalchemy.orm import Session

from models.signal import Signal


ACTIVE_SIGNAL_STATUSES = ["published", "opened", "tp1", "breakeven"]


@dataclass
class ExposureGuardResult:
    allowed: bool
    reason: str | None
    active_signals_count: int
    active_symbol_signals_count: int
    used_margin: float
    max_allowed_margin: float
    free_margin: float
    required_margin: float
    cluster_same_dir_count: int = 0


class ExposureGuard:
    def active_signals(self, db: Session, bot_id: int):
        return (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot_id,
                Signal.status.in_(ACTIVE_SIGNAL_STATUSES),
            )
            .order_by(Signal.id.desc())
            .all()
        )

    def active_signals_for_symbol(self, db: Session, bot_id: int, symbol: str):
        return (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot_id,
                Signal.symbol == symbol,
                Signal.status.in_(ACTIVE_SIGNAL_STATUSES),
            )
            .order_by(Signal.id.desc())
            .all()
        )

    @staticmethod
    def engine_of_regime(regime: str | None) -> str:
        """Движок по строке режима. trend_up и trend_down — один движок: делить
        его слоты ещё и по направлению смысла нет, направление уже в side."""
        value = str(regime or "")
        if value.startswith("trend_"):
            return "trend"
        if value.startswith("reversal") or "escalated" in value:
            return "reversal"
        return value or "unknown"

    @classmethod
    def engine_of(cls, signal) -> str:
        plan_json = getattr(signal, "plan_json", None) or {}
        regime = plan_json.get("regime") if isinstance(plan_json, dict) else None
        return cls.engine_of_regime(regime)

    def active_same_direction_in_cluster(
        self,
        db: Session,
        bot_id: int,
        side: str,
        cluster_symbols: set[str] | None,
        engine: str | None = None,
    ) -> int:
        """Сколько активных позиций в ТОМ ЖЕ направлении внутри кластера.

        `cluster_symbols=None` → вся вселенная один кластер: наши символы —
        коррелированные мажоры, и шорт по всем это одна ставка с плечом.

        `engine` задан → считаем только позиции ЭТОГО ЖЕ движка. Иначе движки
        отнимают слоты друг у друга по принципу «кто раньше пришёл»: на медвежьем
        рынке trend_down занимал 60% ленты решений и забирал оба шортовых слота,
        после чего CRT блокировался `cluster_direction_cap` в 24 случаях из 44 —
        при том, что у CRT ожидание +0.134R, а у trend_down −0.012R. Дефицитный
        ресурс доставался режиму с худшим ожиданием просто по очереди.
        """
        side = str(side).lower()
        n = 0
        for sig in self.active_signals(db, bot_id):
            if str(getattr(sig, "side", "")).lower() != side:
                continue
            if cluster_symbols is not None and str(sig.symbol).upper() not in cluster_symbols:
                continue
            if engine is not None and self.engine_of(sig) != engine:
                continue
            n += 1
        return n

    def estimate_signal_margin(self, signal: Signal) -> float:
        margin = None
        if getattr(signal, "required_margin", None):
            margin = float(signal.required_margin)
        else:
            plan_json = getattr(signal, "plan_json", None) or {}
            if isinstance(plan_json, dict) and plan_json.get("required_margin"):
                margin = float(plan_json["required_margin"])

        if margin is None:
            return 325.0

        # (#tp1-partial-margin-2026-07-19) После частичной фиксации на TP1 половина
        # позиции реально закрыта — её маржа свободна. Signal.required_margin
        # НАМЕРЕННО не трогаем (result_pct закрытия честно считается от исходной
        # маржи всей сделки), поэтому освобождение учитываем здесь, в оценке
        # экспозиции: занято = маржа × доля остатка. Иначе dynamic budget и
        # anti-drain часами недосчитывали свободные деньги (у #266 — ~134 USDT).
        plan_json = getattr(signal, "plan_json", None) or {}
        partial = plan_json.get("tp1_partial") if isinstance(plan_json, dict) else None
        if isinstance(partial, dict):
            try:
                closed_qty = float(partial.get("closed_qty") or 0.0)
                remaining_qty = float(partial.get("remaining_qty") or 0.0)
                total_qty = closed_qty + remaining_qty
                if closed_qty > 0 and remaining_qty > 0 and total_qty > 0:
                    margin = margin * (remaining_qty / total_qty)
            except (TypeError, ValueError):
                pass  # битые данные партиала → консервативно полная маржа

        return round(margin, 6)

    def used_margin(self, db: Session, bot_id: int) -> float:
        total = 0.0

        for signal in self.active_signals(db, bot_id):
            total += self.estimate_signal_margin(signal)

        return round(total, 6)

    def check_before_publish(
        self,
        db: Session,
        bot_id: int,
        symbol: str,
        required_margin: float,
        equity_usdt: float,
        max_used_margin_pct: float,
        max_active_signals: int,
        max_active_per_symbol: int,
        side: str | None = None,
        max_same_direction_cluster: int = 0,
        cluster_symbols: set[str] | None = None,
        engine: str | None = None,
        portfolio_max_same_direction: int = 0,
    ) -> ExposureGuardResult:
        active = self.active_signals(db, bot_id)
        active_for_symbol = self.active_signals_for_symbol(db, bot_id, symbol)

        active_count = len(active)
        symbol_active_count = len(active_for_symbol)

        used_margin = self.used_margin(db, bot_id)
        max_allowed_margin = round(equity_usdt * max_used_margin_pct, 6)
        free_margin = round(max_allowed_margin - used_margin, 6)

        # Лимит направления — внутри своего движка (engine задан) или по всему
        # портфелю (engine=None, прежнее поведение).
        cluster_same_dir = (
            self.active_same_direction_in_cluster(db, bot_id, side, cluster_symbols, engine)
            if side else 0
        )
        portfolio_same_dir = (
            self.active_same_direction_in_cluster(db, bot_id, side, cluster_symbols, None)
            if side else 0
        )

        base = {
            "active_signals_count": active_count,
            "active_symbol_signals_count": symbol_active_count,
            "used_margin": used_margin,
            "max_allowed_margin": max_allowed_margin,
            "free_margin": free_margin,
            "required_margin": round(float(required_margin or 0), 6),
            "cluster_same_dir_count": cluster_same_dir,
            "portfolio_same_dir_count": portfolio_same_dir,
            "engine": engine,
        }

        # (#leak-correlation) Одно направление по коррелированным мажорам — одна
        # ставка с плечом, поэтому лимит нужен. Но считать его по всему портфелю
        # значит заставить движки конкурировать за общий слот: тот, кто пришёл
        # первым, закрывает вход всем остальным. Теперь лимит свой у каждого
        # движка, а портфельный потолок стоит отдельно и выше.
        if side and max_same_direction_cluster > 0 and cluster_same_dir >= max_same_direction_cluster:
            return ExposureGuardResult(
                allowed=False,
                reason="cluster_direction_cap",
                **base,
            )

        # Портфельный предохранитель. Не для того, чтобы делить слоты между
        # движками, а чтобы одновременная просадка по всем коррелированным
        # позициям осталась в пределах дневного лимита убытка: при
        # RISK_PER_TRADE_PCT=0.4 пять одинаково направленных стопов дают 2%
        # эквити против MAX_DAILY_LOSS_PCT=3.
        if side and portfolio_max_same_direction > 0 and portfolio_same_dir >= portfolio_max_same_direction:
            return ExposureGuardResult(
                allowed=False,
                reason="portfolio_direction_cap",
                **base,
            )

        if symbol_active_count >= max_active_per_symbol:
            return ExposureGuardResult(
                allowed=False,
                reason="active_signal_already_exists",
                **base,
            )

        if active_count >= max_active_signals:
            return ExposureGuardResult(
                allowed=False,
                reason="max_active_signals_reached",
                **base,
            )

        if required_margin > free_margin:
            return ExposureGuardResult(
                allowed=False,
                reason="required_margin_exceeds_free_margin",
                **base,
            )

        return ExposureGuardResult(
            allowed=True,
            reason="ok",
            **base,
        )