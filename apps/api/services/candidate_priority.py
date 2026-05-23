from dataclasses import dataclass
from typing import Any


@dataclass
class RankedCandidate:
    symbol: str
    side: str | None
    grade: str | None
    priority_score: float
    reason: str
    payload: dict[str, Any]


class CandidatePriorityService:
    """
    Ранжирует кандидатов после intelligence scan.

    Цель:
    - не брать первый попавшийся сигнал;
    - выбирать лучший актуальный шанс;
    - штрафовать слабый RR, старые watch-сигналы, плохую историю символа;
    - учитывать performance_guard и exposure.
    """

    GRADE_BONUS = {
        "A+": 12.0,
        "A": 8.0,
        "B": 3.0,
        "C": -10.0,
        None: 0.0,
    }

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def _safe_str(self, value, default: str = "") -> str:
        if value is None:
            return default
        return str(value)

    def _plan_rr_score(self, result: dict) -> float:
        plan = result.get("plan") or {}
        rr = self._safe_float(plan.get("net_rr_tp2"), 0.0)

        if rr <= 0:
            return -40.0

        # RR 1.2 = нормально, 2.0+ = хорошо, 3.0+ = отлично.
        return min(rr, 3.0) * 8.0

    def _performance_score(self, result: dict) -> float:
        guard = result.get("performance_guard") or {}

        if not guard:
            return 0.0

        if guard.get("allowed") is False:
            return -100.0

        multiplier = self._safe_float(guard.get("risk_multiplier"), 1.0)
        winrate = self._safe_float(guard.get("winrate"), 0.0)
        closed_count = int(self._safe_float(guard.get("closed_count"), 0.0))
        losing_streak = int(self._safe_float(guard.get("losing_streak"), 0.0))
        positive_then_negative = int(self._safe_float(guard.get("positive_then_negative_count"), 0.0))

        score = 0.0

        score += (multiplier - 1.0) * 20.0

        if closed_count >= 3:
            if winrate >= 60:
                score += 8.0
            elif winrate < 40:
                score -= 10.0

        if losing_streak >= 2:
            score -= losing_streak * 8.0

        if positive_then_negative >= 2:
            score -= positive_then_negative * 4.0

        return score

    def _exposure_score(self, result: dict) -> float:
        exposure = result.get("exposure") or {}

        if not exposure:
            return 0.0

        if exposure.get("allowed") is False:
            reason = self._safe_str(exposure.get("reason"))

            if reason in ["active_signal_already_exists", "active_symbol_signal_exists"]:
                return -80.0

            if reason in ["max_active_signals_reached", "not_enough_free_margin", "max_margin_exceeded"]:
                return -120.0

            return -70.0

        free_margin = self._safe_float(exposure.get("free_margin"), 0.0)
        required_margin = self._safe_float(exposure.get("required_margin"), 0.0)

        if required_margin > 0 and free_margin > 0:
            margin_ratio = free_margin / required_margin

            if margin_ratio < 1.0:
                return -100.0

            if margin_ratio < 1.5:
                return -10.0

        return 0.0

    def _age_penalty(self, result: dict) -> float:
        age = self._safe_float(result.get("watch_age_minutes"), 0.0)

        if age <= 0:
            return 0.0

        if age <= 15:
            return 0.0

        if age <= 45:
            return -5.0

        if age <= 90:
            return -12.0

        return -25.0

    def _decision_score(self, result: dict) -> float:
        status = self._safe_str(result.get("status"))
        decision = self._safe_str(result.get("decision"))
        setup_decision = self._safe_str(result.get("setup_decision"))
        radar_state = self._safe_str(result.get("radar_state"))

        score = 0.0

        if setup_decision == "approve":
            score += 20.0

        if status == "published":
            score += 15.0
        elif status == "candidate":
            score += 10.0
        elif status == "wait":
            score -= 35.0
        elif status == "watch":
            score -= 45.0
        elif status == "rejected":
            score -= 80.0
        elif status in ["hold", "blocked", "error"]:
            score -= 100.0

        if decision in ["published_signal_created", "ready_to_publish"]:
            score += 15.0

        if "wait_better_entry_rr" in radar_state or decision == "wait_better_entry_rr":
            score -= 20.0

        if "skip" in decision:
            score -= 40.0

        return score

    def rank_one(self, result: dict) -> RankedCandidate:
        symbol = self._safe_str(result.get("symbol"))
        side = result.get("action") or result.get("side")
        grade = result.get("grade")

        effective_confidence = self._safe_float(
            result.get("effective_confidence"),
            self._safe_float(result.get("confidence_hint"), 0.0),
        )

        setup_quality = result.get("setup_quality") or {}
        setup_score = self._safe_float(
            setup_quality.get("final_score"),
            self._safe_float(setup_quality.get("raw_score"), 0.0),
        )

        score = 0.0
        score += effective_confidence * 0.35
        score += setup_score * 0.30
        score += self.GRADE_BONUS.get(grade, 0.0)
        score += self._plan_rr_score(result)
        score += self._performance_score(result)
        score += self._exposure_score(result)
        score += self._age_penalty(result)
        score += self._decision_score(result)

        reason = "ranked"

        status = self._safe_str(result.get("status"))

        if status in ["hold", "error"]:
            reason = "not_candidate"
        elif status == "watch":
            reason = "watch_not_publishable"
        elif status == "wait":
            reason = "wait_not_publishable"
        elif status == "rejected":
            reason = "rejected_not_publishable"
        elif status == "blocked":
            reason = "blocked_not_publishable"

        if (result.get("exposure") or {}).get("allowed") is False:
            reason = f"exposure_blocked:{(result.get('exposure') or {}).get('reason')}"

        if (result.get("performance_guard") or {}).get("allowed") is False:
            reason = f"performance_blocked:{(result.get('performance_guard') or {}).get('reason')}"

        return RankedCandidate(
            symbol=symbol,
            side=side,
            grade=grade,
            priority_score=round(score, 4),
            reason=reason,
            payload=result,
        )

    def rank(self, results: list[dict]) -> list[RankedCandidate]:
        ranked = [self.rank_one(item) for item in results]
        ranked.sort(key=lambda x: x.priority_score, reverse=True)
        return ranked

    def top_candidates(
        self,
        results: list[dict],
        limit: int = 4,
        min_score: float = 70.0,
    ) -> list[RankedCandidate]:
        ranked = self.rank(results)

        return [
            item for item in ranked
            if item.priority_score >= min_score
            and item.reason == "ranked"
        ][:limit]