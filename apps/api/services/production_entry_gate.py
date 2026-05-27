from dataclasses import dataclass

from core.config import settings


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

        # Defensive defaults to avoid any accidental unbound local usage
        # if thresholds are later logged/refactored.
        min_setup = None
        min_confidence = None
        min_rr1 = None
        min_rr2 = None
        min_priority = None

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
            min_setup = float(getattr(settings, "PROD_GATE_A_PLUS_MIN_SETUP", 82.0))
            min_confidence = float(getattr(settings, "PROD_GATE_A_PLUS_MIN_CONFIDENCE", 74.0))
            min_rr1 = float(getattr(settings, "PROD_GATE_A_PLUS_MIN_RR_TP1", 0.95))
            min_rr2 = float(getattr(settings, "PROD_GATE_A_PLUS_MIN_RR_TP2", 1.45))

            if setup < min_setup:
                return ProductionGateDecision(False, "a_plus_setup_too_weak", payload)
            if confidence < min_confidence:
                return ProductionGateDecision(False, "a_plus_confidence_too_low", payload)
            if rr1 < min_rr1:
                return ProductionGateDecision(False, "a_plus_rr_tp1_too_low", payload)
            if rr2 < min_rr2:
                return ProductionGateDecision(False, "a_plus_rr_tp2_too_low", payload)
            return ProductionGateDecision(True, "a_plus_passed", payload)

        if grade_value == "A":
            min_setup = float(getattr(settings, "PROD_GATE_A_MIN_SETUP", 76.0))
            min_confidence = float(getattr(settings, "PROD_GATE_A_MIN_CONFIDENCE", 70.0))
            trading_mode = str(getattr(settings, "TRADING_MODE", "paper_signal")).lower()
            if trading_mode in ["paper_signal", "paper_trade"]:
                min_rr1 = float(getattr(settings, "PROD_GATE_A_MIN_RR_TP1_PAPER", 0.78))
            else:
                min_rr1 = float(getattr(settings, "PROD_GATE_A_MIN_RR_TP1", 0.9))
                min_rr2 = float(getattr(settings, "PROD_GATE_A_MIN_RR_TP2", 1.35))

            if setup < min_setup:
                return ProductionGateDecision(False, "a_setup_too_weak", payload)
            if confidence < min_confidence:
                return ProductionGateDecision(False, "a_confidence_too_low", payload)
            if rr1 < min_rr1:
                return ProductionGateDecision(False, "a_rr_tp1_too_low", payload)
            if rr2 < min_rr2:
                return ProductionGateDecision(False, "a_rr_tp2_too_low", payload)
            return ProductionGateDecision(True, "a_passed", payload)

        if grade_value == "B":
            min_setup = float(getattr(settings, "PROD_GATE_B_MIN_SETUP", 70.0))
            min_confidence = float(getattr(settings, "PROD_GATE_B_MIN_CONFIDENCE", 60.0))
            min_rr1 = float(getattr(settings, "PROD_GATE_B_MIN_RR_TP1", 0.8))
            min_rr2 = float(getattr(settings, "PROD_GATE_B_MIN_RR_TP2", 1.25))
            min_priority = float(getattr(settings, "PROD_GATE_B_MIN_PRIORITY", 85.0))

            if setup < min_setup:
                return ProductionGateDecision(False, "b_setup_too_weak", payload)
            if confidence < min_confidence:
                return ProductionGateDecision(False, "b_confidence_too_low", payload)
            if rr1 < min_rr1:
                return ProductionGateDecision(False, "b_rr_tp1_too_low", payload)
            if rr2 < min_rr2:
                return ProductionGateDecision(False, "b_rr_tp2_too_low", payload)
            if priority < min_priority:
                return ProductionGateDecision(False, "b_priority_too_low", payload)
            return ProductionGateDecision(True, "b_passed", payload)

        return ProductionGateDecision(
            allowed=False,
            reason="unknown_grade_not_tradeable",
            payload=payload,
        )
