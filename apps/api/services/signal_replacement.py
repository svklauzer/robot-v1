# signal_replacement.py

from dataclasses import dataclass
from datetime import datetime, timezone

from models.signal import Signal


@dataclass
class ReplacementDecision:
    allowed: bool
    reason: str
    replace_signal_id: int | None
    payload: dict


class SignalReplacementPolicy:
    """
    Замена слабого published-сигнала более сильным кандидатом.

    Безопасная версия:
    - заменяем только status='published';
    - НЕ закрываем opened-позиции;
    - не трогаем tp1/breakeven;
    - используется, когда новый сильный кандидат заблокирован по active limit / margin.
    """

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def _priority_score(self, signal: Signal) -> float:
        plan = signal.plan_json or {}
        return self._safe_float(plan.get("priority_score"), 0.0)

    def _setup_score(self, signal: Signal) -> float:
        plan = signal.plan_json or {}
        return self._safe_float(plan.get("setup_score"), 0.0)

    def _grade_rank(self, grade: str | None) -> int:
        grade = str(grade or "").upper()
        if grade == "A+":
            return 4
        if grade == "A":
            return 3
        if grade == "B":
            return 2
        if grade == "C":
            return 1
        return 0

    def _is_expiring_soon(self, signal: Signal, minutes: int = 15) -> bool:
        if not signal.expires_at:
            return False

        exp = signal.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        seconds_left = (exp - now).total_seconds()

        return seconds_left <= minutes * 60

    def check(
        self,
        *,
        db,
        bot_id: int,
        new_symbol: str,
        new_side: str,
        new_grade: str | None,
        new_priority_score: float,
        new_setup_score: float,
        new_rr_tp1: float,
        new_rr_tp2: float,
        new_required_margin: float,
    ) -> ReplacementDecision:
        new_grade_rank = self._grade_rank(new_grade)

        # Замена разрешается только для сильных кандидатов.
        if new_grade_rank < 4:
            return ReplacementDecision(
                allowed=False,
                reason="replacement_requires_a_plus_candidate",
                replace_signal_id=None,
                payload={
                    "new_symbol": new_symbol,
                    "new_side": new_side,
                    "new_grade": new_grade,
                    "new_priority_score": new_priority_score,
                    "new_setup_score": new_setup_score,
                    "new_rr_tp1": new_rr_tp1,
                    "new_rr_tp2": new_rr_tp2,
                },
            )

        if (
            float(new_priority_score or 0) < 110
            or float(new_setup_score or 0) < 90
            or float(new_rr_tp1 or 0) < 1.0
            or float(new_rr_tp2 or 0) < 1.6
        ):
            return ReplacementDecision(
                allowed=False,
                reason="replacement_candidate_not_strong_enough",
                replace_signal_id=None,
                payload={
                    "new_symbol": new_symbol,
                    "new_grade": new_grade,
                    "new_priority_score": new_priority_score,
                    "new_setup_score": new_setup_score,
                    "new_rr_tp1": new_rr_tp1,
                    "new_rr_tp2": new_rr_tp2,
                },
            )

        active_published = (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot_id,
                Signal.status == "published",
            )
            .order_by(Signal.id.asc())
            .all()
        )

        if not active_published:
            return ReplacementDecision(
                allowed=False,
                reason="no_published_signal_to_replace",
                replace_signal_id=None,
                payload={
                    "new_symbol": new_symbol,
                    "new_side": new_side,
                },
            )

        candidates = []

        for sig in active_published:
            old_priority = self._priority_score(sig)
            old_setup = self._setup_score(sig)
            old_grade_rank = self._grade_rank(sig.grade)
            expiring_soon = self._is_expiring_soon(sig)

            penalty_score = 0.0

            # Чем ниже старый grade, тем охотнее заменяем.
            penalty_score += (4 - old_grade_rank) * 20

            # Слабый priority/setup — кандидат на замену.
            if old_priority <= 0:
                penalty_score += 20
            elif float(new_priority_score or 0) - old_priority >= 25:
                penalty_score += 30

            if old_setup and old_setup < 80:
                penalty_score += 15

            if expiring_soon:
                penalty_score += 20

            # Не заменяем такой же symbol+side, это должен решать reentry/freshness.
            if sig.symbol == new_symbol and sig.side == new_side:
                penalty_score -= 100

            candidates.append({
                "signal": sig,
                "penalty_score": penalty_score,
                "old_priority_score": old_priority,
                "old_setup_score": old_setup,
                "old_grade": sig.grade,
                "expiring_soon": expiring_soon,
            })

        candidates.sort(key=lambda x: x["penalty_score"], reverse=True)

        best = candidates[0]

        if best["penalty_score"] < 30:
            return ReplacementDecision(
                allowed=False,
                reason="no_weak_enough_published_signal",
                replace_signal_id=None,
                payload={
                    "new_symbol": new_symbol,
                    "new_side": new_side,
                    "new_grade": new_grade,
                    "new_priority_score": new_priority_score,
                    "best_candidate_penalty": best["penalty_score"],
                    "best_candidate_signal_id": best["signal"].id,
                    "best_candidate_symbol": best["signal"].symbol,
                    "best_candidate_side": best["signal"].side,
                    "best_candidate_grade": best["signal"].grade,
                },
            )

        old_signal = best["signal"]

        return ReplacementDecision(
            allowed=True,
            reason="replace_weak_published_signal",
            replace_signal_id=old_signal.id,
            payload={
                "replace_signal_id": old_signal.id,
                "replace_symbol": old_signal.symbol,
                "replace_side": old_signal.side,
                "replace_grade": old_signal.grade,
                "replace_priority_score": best["old_priority_score"],
                "replace_setup_score": best["old_setup_score"],
                "replace_expiring_soon": best["expiring_soon"],
                "new_symbol": new_symbol,
                "new_side": new_side,
                "new_grade": new_grade,
                "new_priority_score": new_priority_score,
                "new_setup_score": new_setup_score,
                "new_rr_tp1": new_rr_tp1,
                "new_rr_tp2": new_rr_tp2,
                "new_required_margin": new_required_margin,
                "replacement_penalty_score": best["penalty_score"],
            },
        )