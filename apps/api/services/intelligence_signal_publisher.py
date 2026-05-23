from datetime import datetime, timezone, timedelta

from models.signal import Signal
from models.bot import Bot


class IntelligenceSignalPublisher:
    def __init__(self):
        pass

    def publish_if_ready(self, db, bot: Bot, result: dict):
        """
        Превращает intelligence candidate в обычный Signal.
        Возвращает:
        - created
        - skipped_not_ready
        - skipped_duplicate
        """

        if not result:
            return {"status": "skipped_not_ready", "reason": "empty_result"}

        if result.get("status") != "candidate":
            return {"status": "skipped_not_ready", "reason": "not_candidate"}

        if result.get("decision") != "ready_to_publish":
            return {"status": "skipped_not_ready", "reason": "decision_not_ready"}

        grade = result.get("grade")
        if grade not in ["A+", "A"]:
            return {
                "status": "skipped_not_ready",
                "reason": "grade_not_publishable",
                "grade": grade,
            }

        plan = result.get("plan") or {}
        if plan and plan.get("is_valid") is False:
            return {
                "status": "skipped_not_ready",
                "reason": plan.get("reject_reason") or "plan_invalid",
            }

        symbol = result.get("symbol")
        side = result.get("action")

        if not symbol or side not in ["long", "short"]:
            return {
                "status": "skipped_not_ready",
                "reason": "invalid_symbol_or_side",
            }

        entry_zone = result.get("entry_zone")
        stop_price = result.get("stop_price")
        tp = result.get("tp")

        if not entry_zone or stop_price is None or not tp:
            return {
                "status": "skipped_not_ready",
                "reason": "missing_trade_levels",
            }

        # защита от дублей: не создаём новый сигнал,
        # если по той же монете и стороне уже есть живой сигнал
        existing = (
            db.query(Signal)
            .filter(
                Signal.bot_id == bot.id,
                Signal.symbol == symbol,
                Signal.side == side,
                Signal.status.in_(["published", "opened", "tp1", "breakeven"]),
            )
            .order_by(Signal.id.desc())
            .first()
        )

        if existing:
            return {
                "status": "skipped_duplicate",
                "signal_id": existing.id,
                "reason": "active_signal_exists",
            }

        confidence = float(result.get("effective_confidence") or result.get("confidence_hint") or 0)

        now = datetime.now(timezone.utc)

        if grade == "A+":
            expires_at = now + timedelta(minutes=90)
        elif grade == "A":
            expires_at = now + timedelta(minutes=60)
        else:
            expires_at = now + timedelta(minutes=30)

        signal = Signal(
            bot_id=bot.id,
            symbol=symbol,
            side=side,
            status="published",
            entry_zone_json={
                "from": float(entry_zone[0]),
                "to": float(entry_zone[1]),
            },
            stop_price=float(stop_price),
            tp_json={
                "tp1": float(tp.get("tp1")),
                "tp2": float(tp.get("tp2")),
            },
            confidence=round(confidence, 2),
            grade=grade,
            is_public=True,
            expires_at=expires_at,
            rationale=f"intelligence_{result.get('reason') or result.get('decision')}",
            created_at=now,

            qty=plan.get("qty"),
            required_margin=plan.get("required_margin"),
            net_rr_tp1=plan.get("net_rr_tp1"),
            net_rr_tp2=plan.get("net_rr_tp2"),
            net_pnl_tp1=plan.get("net_pnl_tp1"),
            net_pnl_tp2=plan.get("net_pnl_tp2"),
            net_pnl_stop=plan.get("net_pnl_stop"),
            plan_json={
                "qty": plan.get("qty"),
                "required_margin": plan.get("required_margin"),
                "net_pnl_tp1": plan.get("net_pnl_tp1"),
                "net_pnl_tp2": plan.get("net_pnl_tp2"),
                "net_pnl_stop": plan.get("net_pnl_stop"),
                "net_rr_tp1": plan.get("net_rr_tp1"),
                "net_rr_tp2": plan.get("net_rr_tp2"),
                "is_valid": plan.get("is_valid"),
                "reject_reason": plan.get("reject_reason"),
            } if plan else None,
        )

        db.add(signal)
        db.flush()

        return {
            "status": "created",
            "signal_id": signal.id,
            "symbol": symbol,
            "side": side,
            "grade": grade,
        }