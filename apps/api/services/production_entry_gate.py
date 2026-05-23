from dataclasses import dataclass


@dataclass
class ProductionGateDecision:
    allowed: bool
    reason: str
    payload: dict


class ProductionEntryGate:
    """
    Финальный фильтр перед созданием Signal.

    Grade — это не разрешение на сделку.
    Разрешение даёт production gate:
    - RR должен быть нормальным;
    - setup должен быть достаточно сильным;
    - confidence должен быть достаточным;
    - B-сигналы должны быть сильно лучше по RR.
    """

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def check(
        self,
        *,
        grade: str | None,
        setup_score: float | None,
        effective_confidence: float | None,
        net_rr_tp1: float | None,
        net_rr_tp2: float | None,
        priority_score: float | None,
    ) -> ProductionGateDecision:
        grade_value = str(grade or "").upper()

        setup = self._safe_float(setup_score)
        confidence = self._safe_float(effective_confidence)
        rr1 = self._safe_float(net_rr_tp1)
        rr2 = self._safe_float(net_rr_tp2)
        priority = self._safe_float(priority_score)

        payload = {
            "grade": grade_value,
            "setup_score": setup,
            "effective_confidence": confidence,
            "net_rr_tp1": rr1,
            "net_rr_tp2": rr2,
            "priority_score": priority,
        }

        if grade_value == "C":
            return ProductionGateDecision(
                allowed=False,
                reason="grade_c_learning_only",
                payload=payload,
            )

        if rr1 <= 0 or rr2 <= 0:
            return ProductionGateDecision(
                allowed=False,
                reason="invalid_net_rr",
                payload=payload,
            )

        if grade_value == "A+":
            if setup < 85:
                return ProductionGateDecision(False, "a_plus_setup_too_weak", payload)

            if confidence < 78:
                return ProductionGateDecision(False, "a_plus_confidence_too_low", payload)

            if rr1 < 1.0:
                return ProductionGateDecision(False, "a_plus_rr_tp1_too_low", payload)

            if rr2 < 1.6:
                return ProductionGateDecision(False, "a_plus_rr_tp2_too_low", payload)

            return ProductionGateDecision(True, "a_plus_passed", payload)

        if grade_value == "A":
            if setup < 80:
                return ProductionGateDecision(False, "a_setup_too_weak", payload)

            if confidence < 74:
                return ProductionGateDecision(False, "a_confidence_too_low", payload)

            if rr1 < 0.95:
                return ProductionGateDecision(False, "a_rr_tp1_too_low", payload)

            if rr2 < 1.5:
                return ProductionGateDecision(False, "a_rr_tp2_too_low", payload)

            return ProductionGateDecision(True, "a_passed", payload)

        if grade_value == "B":
            if setup < 82:
                return ProductionGateDecision(False, "b_setup_too_weak", payload)

            if confidence < 68:
                return ProductionGateDecision(False, "b_confidence_too_low", payload)

            if rr1 < 1.05:
                return ProductionGateDecision(False, "b_rr_tp1_too_low", payload)

            if rr2 < 1.8:
                return ProductionGateDecision(False, "b_rr_tp2_too_low", payload)

            if priority < 95:
                return ProductionGateDecision(False, "b_priority_too_low", payload)

            return ProductionGateDecision(True, "b_passed", payload)

        return ProductionGateDecision(
            allowed=False,
            reason="unknown_grade_not_tradeable",
            payload=payload,
        )