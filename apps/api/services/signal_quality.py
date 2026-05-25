from datetime import datetime, timezone, timedelta

from core.config import settings


class SignalQualityService:
    def grade(
        self,
        confidence: float,
        rationale: str,
        regime: str | None = None,
        setup_score: float | None = None,
        effective_confidence: float | None = None,
    ) -> str:
        score = float(effective_confidence if effective_confidence is not None else confidence or 0)

        rationale = str(rationale or "")
        regime = str(regime or "")

        if setup_score is not None:
            setup_score = float(setup_score)

            if setup_score >= 85:
                score += 4
            elif setup_score >= 75:
                score += 3
            elif setup_score >= 65:
                score += 2
            elif setup_score >= 55:
                score += 1
            elif setup_score < 45:
                score -= 3

        if regime in [
            "trend_up_candidate",
            "trend_down_candidate",
            "watch_long_escalated_candidate",
            "watch_short_escalated_candidate",
        ]:
            score += 1
        elif regime in ["mixed", "flat"]:
            score -= 1

        if "approved_weak_volume" in rationale or "weak_volume" in rationale:
            score -= 1

        if score >= 88:
            return "A+"

        if score >= 78:
            return "A"

        if score >= 62:
            return "B"

        return "C"

    def should_publish_to_clients(
        self,
        grade: str,
        setup_score: float | None = None,
        effective_confidence: float | None = None,
        setup_decision: str | None = None,
        setup_quality: dict | None = None,
    ) -> bool:
        trading_mode = str(getattr(settings, "TRADING_MODE", "paper_signal")).lower()

        if setup_decision != "approve":
            return False

        if setup_score is None or effective_confidence is None:
            return False

        setup_score = float(setup_score)
        effective_confidence = float(effective_confidence)
        setup_quality = setup_quality or {}

        weak_volume_count = int(setup_quality.get("weak_volume_count") or 0)
        trend_alignment = float(setup_quality.get("trend_alignment") or 0.0)
        entry_timing = float(setup_quality.get("entry_timing") or 0.0)
        volume_confirmation = float(setup_quality.get("volume_confirmation") or 0.0)

        # Execution Plan V1 hard filters:
        # слишком слабый объём + слабый тренд = не публикуем даже в paper.
        if weak_volume_count >= 4 and volume_confirmation <= 3:
            return False
        if trend_alignment < 35:
            return False
        if entry_timing < 12:
            return False

        # DEV/PAPER: разрешаем больше сделок, чтобы система собирала статистику.
        if trading_mode in ["paper_signal", "paper_trade"]:
            if grade in ["A+", "A"]:
                return setup_score >= 55 and effective_confidence >= 55

            if grade == "B":
                return setup_score >= 58 and effective_confidence >= 56

            # Execution Plan V1:
            # в paper-режиме отключаем C-класс, чтобы не загрязнять статистику
            # заведомо слабыми сценариями.
            if grade == "C":
                return False

            return False

        # LIVE SIGNAL: только нормальные A/B.
        if trading_mode == "live_signal":
            if grade in ["A+", "A"]:
                return setup_score >= 72 and effective_confidence >= 68

            if grade == "B":
                return setup_score >= 76 and effective_confidence >= 72

            return False

        # LIVE TRADE: максимально строго.
        if trading_mode == "live_trade":
            if grade in ["A+", "A"]:
                return setup_score >= 80 and effective_confidence >= 74

            return False

        return grade in ["A+", "A"] and setup_score >= 70 and effective_confidence >= 68

    def expiry_time(self, grade: str):
        now = datetime.now(timezone.utc)

        if grade == "A+":
            return now + timedelta(minutes=90)

        if grade == "A":
            return now + timedelta(minutes=60)

        if grade == "B":
            return now + timedelta(minutes=45)

        return now + timedelta(minutes=30)

    def human_risk_label(self, grade: str) -> str:
        if grade == "A+":
            return "низкий/средний"

        if grade == "A":
            return "средний"

        if grade == "B":
            return "повышенный"

        return "обучающий/dev"
