from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from models.intelligence_event import IntelligenceEvent


class IntelligenceMemory:
    """
    Память решений Market Intelligence.

    Логируем не каждый одинаковый hold каждую минуту,
    а только важные смены состояния:
    - hold -> watch
    - watch -> candidate
    - watch -> hold
    - candidate -> rejected
    - candidate -> published
    - blocked short
    """

    def should_log_event(
        self,
        db: Session,
        symbol: str,
        status: str,
        decision: str | None,
        action: str | None = None,
        radar_state: str | None = None,
    ) -> bool:
        last = (
            db.query(IntelligenceEvent)
            .filter(IntelligenceEvent.symbol == symbol)
            .order_by(IntelligenceEvent.id.desc())
            .first()
        )

        if not last:
            return True

        if (
            last.status == status
            and last.decision == decision
            and last.action == action
            and last.radar_state == radar_state
        ):
            return False

        return True

    def _ctx_value(self, ctx, key: str, default=None):
        if ctx is None:
            return default

        if isinstance(ctx, dict):
            return ctx.get(key, default)

        return getattr(ctx, key, default)

    def _is_noisy_decision(self, decision: str | None) -> bool:
        return decision in [
            "candidate_but_wait_confirmation",
            "setup_quality_too_low",
            "quality_grade_too_low",
            "skip_no_trade_conditions",
        ]

    def _has_recent_same_noisy_event(
        self,
        db: Session,
        symbol: str,
        status: str,
        decision: str | None,
        minutes: int = 15,
    ) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)

        recent = (
            db.query(IntelligenceEvent)
            .filter(
                IntelligenceEvent.symbol == symbol,
                IntelligenceEvent.status == status,
                IntelligenceEvent.decision == decision,
                IntelligenceEvent.created_at >= cutoff,
            )
            .order_by(IntelligenceEvent.id.desc())
            .first()
        )

        return recent is not None

    def record_scan_item(self, db: Session, item: dict) -> IntelligenceEvent | None:
        symbol = item.get("symbol")
        status = item.get("status")
        decision = item.get("decision")

        if not symbol or not status:
            return None

        if self._is_noisy_decision(decision):
            if self._has_recent_same_noisy_event(
                db=db,
                symbol=symbol,
                status=status,
                decision=decision,
                minutes=15,
            ):
                return None

        if not self.should_log_event(db, symbol, status, decision):
            return None

        setup_quality = item.get("setup_quality") or {}

        event = IntelligenceEvent(
            symbol=symbol,
            status=status,
            decision=decision,
            action=item.get("action"),
            regime=item.get("regime"),
            radar_state=item.get("radar_state"),
            confidence_hint=item.get("confidence_hint"),
            setup_score=setup_quality.get("final_score") if isinstance(setup_quality, dict) else None,
            payload_json=item,
        )

        db.add(event)
        db.flush()

        return event


    def latest_events(self, db: Session, limit: int = 100):
        return (
            db.query(IntelligenceEvent)
            .order_by(IntelligenceEvent.id.desc())
            .limit(limit)
            .all()
        )

    def symbol_events(self, db: Session, symbol: str, limit: int = 50):
        return (
            db.query(IntelligenceEvent)
            .filter(IntelligenceEvent.symbol == symbol)
            .order_by(IntelligenceEvent.id.desc())
            .limit(limit)
            .all()
        )

    def current_watch_started_at(self, db: Session, symbol: str, status: str, decision: str | None):
        """
        Возвращает время начала текущего непрерывного состояния.
        Например: SOL watch_long начался в 14:45 и всё ещё watch_long.
        """

        events = (
            db.query(IntelligenceEvent)
            .filter(IntelligenceEvent.symbol == symbol)
            .order_by(IntelligenceEvent.id.desc())
            .limit(100)
            .all()
        )

        if not events:
            return None

        latest = events[0]

        if latest.status != status or latest.decision != decision:
            return None

        started_at = latest.created_at

        for event in events[1:]:
            if event.status == status and event.decision == decision:
                started_at = event.created_at
            else:
                break

        return started_at


    def enrich_scan_item(self, db: Session, item: dict) -> dict:
        """
        Добавляет duration/radar diagnostics к scan item.
        """

        symbol = item.get("symbol")
        status = item.get("status")
        decision = item.get("decision")

        item["watch_started_at"] = None
        item["watch_age_minutes"] = 0
        item["escalation_state"] = None
        item["escalation_reason"] = None

        if not symbol or not status:
            return item

        if status == "watch":
            started_at = self.current_watch_started_at(db, symbol, status, decision)

            if started_at:
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)

                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)

                age_minutes = max((now - started_at).total_seconds() / 60, 0)

                item["watch_started_at"] = str(started_at)
                item["watch_age_minutes"] = round(age_minutes, 2)

        if status == "watch":
            scores = item.get("scores") or {}
            timeframes = item.get("timeframes") or {}

            m1 = timeframes.get("1m") or {}
            m5 = timeframes.get("5m") or {}
            m15 = timeframes.get("15m") or {}
            h1 = timeframes.get("1h") or {}
            h4 = timeframes.get("4h") or {}

            improving_long = (
                decision == "watch_long"
                and self._ctx_value(h4, "trend") == "trend_up"
                and self._ctx_value(h4, "momentum") in ["bullish", "neutral"]
                and self._ctx_value(m5, "momentum") in ["bullish", "neutral"]
                and self._ctx_value(m1, "momentum") in ["bullish", "neutral"]
                and scores.get("volume", 0) >= 55
            )

            weak_long = (
                decision == "watch_long"
                and (
                    self._ctx_value(h4, "trend") != "trend_up"
                    or self._ctx_value(h4, "momentum") == "bearish"
                    or scores.get("trend", 0) < 40
                )
            )

            improving_short = (
                decision == "watch_short"
                and self._ctx_value(h4, "trend") == "trend_down"
                and self._ctx_value(h4, "momentum") in ["bearish", "neutral"]
                and self._ctx_value(m5, "momentum") in ["bearish", "neutral"]
                and self._ctx_value(m1, "momentum") in ["bearish", "neutral"]
                and scores.get("volume", 0) >= 55
            )

            weak_short = (
                decision == "watch_short"
                and (
                    self._ctx_value(h4, "trend") != "trend_down"
                    or self._ctx_value(h4, "momentum") == "bullish"
                    or scores.get("trend", 0) > 60
                )
            )

            if improving_long or improving_short:
                item["escalation_state"] = "warming_up"
                item["escalation_reason"] = "lower_timeframes_improving"

            elif weak_long or weak_short:
                item["escalation_state"] = "weakening"
                item["escalation_reason"] = "higher_timeframe_bias_weakening"

            elif item["watch_age_minutes"] >= 60:
                item["escalation_state"] = "stale"
                item["escalation_reason"] = "watch_too_long_without_confirmation"

            else:
                item["escalation_state"] = "watching"
                item["escalation_reason"] = "waiting_for_confirmation"

        return item

    def is_watch_cooldown_active(
        self,
        db: Session,
        symbol: str,
        radar_state: str | None,
        cooldown_minutes: int = 60,
    ) -> tuple[bool, str | None]:
        """
        Если недавно был watch_expired, не даём монете сразу вернуться
        в тот же watch_long/watch_short.
        """

        if radar_state not in ["watch_long", "watch_short"]:
            return False, None

        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)

        last_expired = (
            db.query(IntelligenceEvent)
            .filter(
                IntelligenceEvent.symbol == symbol,
                IntelligenceEvent.status == "hold",
                IntelligenceEvent.decision == "watch_expired",
                IntelligenceEvent.created_at >= cutoff,
            )
            .order_by(IntelligenceEvent.id.desc())
            .first()
        )

        if not last_expired:
            return False, None

        return True, f"watch_cooldown_after_expiry_{cooldown_minutes}m"

    def has_strong_reentry_override(self, item: dict) -> tuple[bool, str | None]:
        """
        Разрешает вернуться в watch раньше cooldown,
        если рынок явно улучшился после watch_expired.

        Работает и с dict, и с TimeframeContext.
        """

        radar_state = item.get("radar_state")
        scores = item.get("scores") or {}
        timeframes = item.get("timeframes") or {}

        m1 = timeframes.get("1m")
        m5 = timeframes.get("5m")
        m15 = timeframes.get("15m")
        h1 = timeframes.get("1h")
        h4 = timeframes.get("4h")

        if radar_state == "watch_long":
            strong_long = (
                self._ctx_value(h4, "trend") == "trend_up"
                and self._ctx_value(h4, "momentum") in ["bullish", "neutral"]
                and self._ctx_value(h4, "volume_state") in ["normal", "strong"]
                and self._ctx_value(h1, "volume_state") in ["normal", "strong"]
                and self._ctx_value(m15, "momentum") in ["bullish", "neutral", "oversold"]
                and self._ctx_value(m15, "volume_state") in ["normal", "strong"]
                and self._ctx_value(m5, "momentum") in ["bullish", "neutral"]
                and self._ctx_value(m5, "volume_state") in ["normal", "strong"]
                and scores.get("volume", 0) >= 55
                and scores.get("total", 0) >= 52
            )

            if strong_long:
                return True, "strong_reentry_override_long"

        if radar_state == "watch_short":
            strong_short = (
                self._ctx_value(h4, "trend") == "trend_down"
                and self._ctx_value(h4, "momentum") in ["bearish", "neutral"]
                and self._ctx_value(h4, "volume_state") in ["normal", "strong"]
                and self._ctx_value(h1, "volume_state") in ["normal", "strong"]
                and self._ctx_value(m15, "momentum") in ["bearish", "neutral", "overheated"]
                and self._ctx_value(m15, "volume_state") in ["normal", "strong"]
                and self._ctx_value(m5, "momentum") in ["bearish", "neutral"]
                and self._ctx_value(m5, "volume_state") in ["normal", "strong"]
                and scores.get("volume", 0) >= 55
                and scores.get("total", 0) >= 52
            )

            if strong_short:
                return True, "strong_reentry_override_short"

        return False, None
