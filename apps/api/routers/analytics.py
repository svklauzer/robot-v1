from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from core.config import settings
from core.db import SessionLocal
from core.security import require_owner_action
from models.bot import Bot
from models.signal import Signal
from services.exposure_guard import ExposureGuard
from services.validation_gates import ValidationGateService
from services.outcome_diagnostics import OutcomeDiagnosticsService
from services.symbol_performance_summary import SymbolPerformanceSummaryService
from services.symbol_policy_replay import SymbolPolicyReplayService
from services.daily_quality_report import DailyQualityReportService

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _analytics_summary_data():
    """
    Core analytics computation. Callable both as a route handler and
    as an internal helper (e.g. from system readiness endpoint).
    Creates and closes its own DB session.
    """
    db = SessionLocal()
    try:
        total = db.query(Signal).count()

        closed_signals = db.query(Signal).filter(Signal.status == "closed").all()
        active_statuses = ["published", "opened", "tp1", "breakeven"]
        active_signals = db.query(Signal).filter(Signal.status.in_(active_statuses)).all()

        expired = db.query(Signal).filter(Signal.status == "expired").count()
        rejected = db.query(Signal).filter(Signal.status == "rejected").count()
        telegram_failed = db.query(Signal).filter(Signal.status == "telegram_failed").count()
        queued = db.query(Signal).filter(Signal.status == "queued").count()

        wins = losses = 0
        total_result_pct = total_net_pnl = total_costs = 0.0
        closed_with_money = 0

        for s in closed_signals:
            result_pct = float(s.result_pct or 0)
            total_result_pct += result_pct
            net_pnl = s.closed_net_pnl
            if net_pnl is not None:
                net_pnl = float(net_pnl)
                total_net_pnl += net_pnl
                closed_with_money += 1
                wins += 1 if net_pnl > 0 else 0
                losses += 1 if net_pnl <= 0 else 0
            else:
                wins += 1 if result_pct > 0 else 0
                losses += 1 if result_pct <= 0 else 0
            if s.closed_total_cost is not None:
                total_costs += float(s.closed_total_cost)

        closed_count = len(closed_signals)
        winrate = round((wins / closed_count * 100), 2) if closed_count else 0.0
        avg_net_pnl = round((total_net_pnl / closed_with_money), 6) if closed_with_money else 0.0

        guard = ExposureGuard()
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        used_margin = max_allowed_margin = free_margin = 0.0

        if bot:
            equity_usdt = float(getattr(settings, "RISK_EQUITY_USDT", 950.0))
            max_used_margin_pct = float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85))
            used_margin = guard.used_margin(db, bot.id)
            max_allowed_margin = round(equity_usdt * max_used_margin_pct, 6)
            free_margin = round(max_allowed_margin - used_margin, 6)

        return {
            "total_signals": total,
            "active_signals": len(active_signals),
            "closed_signals": closed_count,
            "expired_signals": expired,
            "rejected_signals": rejected,
            "telegram_failed_signals": telegram_failed,
            "queued_signals": queued,
            "wins": wins,
            "losses": losses,
            "winrate": winrate,
            "total_result_pct": round(total_result_pct, 4),
            "total_net_pnl_usdt": round(total_net_pnl, 6),
            "avg_net_pnl_usdt": avg_net_pnl,
            "total_costs_usdt": round(total_costs, 6),
            "exposure": {
                "used_margin": used_margin,
                "max_allowed_margin": max_allowed_margin,
                "free_margin": free_margin,
                "active_signals_count": len(active_signals),
            },
        }
    finally:
        db.close()


@router.get("/summary", dependencies=[Depends(require_owner_action)])
def analytics_summary():
    return _analytics_summary_data()


@router.get("/validation-gates", dependencies=[Depends(require_owner_action)])
def analytics_validation_gates(limit: int | None = None):
    db = SessionLocal()
    try:
        return ValidationGateService().evaluate(db, limit=limit)
    finally:
        db.close()


@router.get("/reason-breakdown", dependencies=[Depends(require_owner_action)])
def analytics_reason_breakdown(limit: int = 500):
    db = SessionLocal()
    try:
        limit = min(max(limit, 50), 5000)
        signals = (
            db.query(Signal)
            .filter(Signal.status == "closed")
            .order_by(Signal.id.desc())
            .limit(limit)
            .all()
        )
        rows: dict = {}
        total_net = 0.0
        total_count = len(signals)

        for s in signals:
            reason = str(s.closed_reason or "unknown")
            result_pct = float(s.result_pct or 0.0)
            net = float(s.closed_net_pnl or 0.0)
            cost = float(s.closed_total_cost or 0.0)
            total_net += net
            if reason not in rows:
                rows[reason] = {"reason": reason, "count": 0, "wins": 0, "losses": 0,
                                "sum_result_pct": 0.0, "sum_net_pnl_usdt": 0.0, "sum_costs_usdt": 0.0}
            row = rows[reason]
            row["count"] += 1
            row["sum_result_pct"] += result_pct
            row["sum_net_pnl_usdt"] += net
            row["sum_costs_usdt"] += cost
            if net > 0:
                row["wins"] += 1
            else:
                row["losses"] += 1

        items = []
        for reason, row in rows.items():
            count = row["count"] or 1
            share = round((row["count"] / total_count) * 100, 2) if total_count else 0.0
            avg_net = row["sum_net_pnl_usdt"] / count
            avg_result = row["sum_result_pct"] / count
            pnl_share = round((row["sum_net_pnl_usdt"] / total_net) * 100, 2) if total_net else 0.0
            items.append({
                "reason": reason, "count": row["count"], "share_pct": share,
                "wins": row["wins"], "losses": row["losses"],
                "avg_result_pct": round(avg_result, 4),
                "sum_net_pnl_usdt": round(row["sum_net_pnl_usdt"], 6),
                "avg_net_pnl_usdt": round(avg_net, 6),
                "sum_costs_usdt": round(row["sum_costs_usdt"], 6),
                "pnl_share_pct": pnl_share,
            })
        items.sort(key=lambda x: (x["sum_net_pnl_usdt"], -x["count"]))
        return {"status": "ok", "sample_closed_signals": total_count,
                "total_net_pnl_usdt": round(total_net, 6), "items": items}
    finally:
        db.close()


@router.get("/outcome-root-cause", dependencies=[Depends(require_owner_action)])
def analytics_outcome_root_cause(reason: str = "failed_setup_exit", limit: int = 500):
    db = SessionLocal()
    try:
        return OutcomeDiagnosticsService().root_cause(db, reason=reason, limit=limit)
    finally:
        db.close()


@router.get("/symbol-performance", dependencies=[Depends(require_owner_action)])
def analytics_symbol_performance(lookback: int = 12, window_hours: float | None = None):
    db = SessionLocal()
    try:
        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        # Витрина шире живого окна (24ч): по умолчанию SYMBOL_PERF_SUMMARY_WINDOW_HOURS
        # (30 дней), чтобы оператор видел историю, а не пустые no_history. На решения
        # публикации НЕ влияет — это отдельный read-only вызов.
        wh = window_hours if window_hours is not None else float(
            getattr(settings, "SYMBOL_PERF_SUMMARY_WINDOW_HOURS", 720.0)
        )
        return SymbolPerformanceSummaryService().summarize(
            db, bot=bot, lookback=lookback, window_hours=wh,
        )
    finally:
        db.close()


@router.get("/symbol-policy-replay", dependencies=[Depends(require_owner_action)])
def analytics_symbol_policy_replay(lookback: int = 12, sample_limit: int = 25):
    return SymbolPolicyReplayService().replay_path(
        "storage/ml/trade_outcomes.jsonl",
        lookback=lookback,
        sample_limit=sample_limit,
    )


@router.get("/signal-quality", dependencies=[Depends(require_owner_action)])
def analytics_signal_quality(limit: int = 200, only_lifecycle: bool = False):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 1000)
        signals = (
            db.query(Signal)
            .filter(Signal.status == "closed")
            .order_by(Signal.id.desc())
            .limit(limit)
            .all()
        )
        total_closed = len(signals)
        lifecycle_count = legacy_count = went_positive = positive_then_negative = 0
        stop_loss_count = breakeven_count = trailing_count = 0
        post_tp1_stop_count = mfe_capture_count = tp2_count = 0
        mfe_values: list = []
        mae_values: list = []
        missed_values: list = []
        result_values: list = []
        net_pnl_values: list = []
        costs_values: list = []
        by_reason: dict = {}
        by_reason_money: dict = {}
        items = []

        trailing_reasons = {"protective_trailing_stop", "adaptive_trailing_stop", "trend_trailing_stop"}
        post_tp1_reasons = {"adaptive_post_tp1_stop"}
        mfe_capture_reasons = {"adaptive_mfe_capture"}

        for s in signals:
            plan = s.plan_json or {}
            lifecycle = plan.get("lifecycle") or {}
            has_lifecycle = bool(lifecycle)
            lifecycle_count += has_lifecycle
            legacy_count += not has_lifecycle
            if only_lifecycle and not has_lifecycle:
                continue

            reason = s.closed_reason or lifecycle.get("close_reason") or "unknown"
            by_reason[reason] = by_reason.get(reason, 0) + 1
            net_pnl = float(s.closed_net_pnl) if s.closed_net_pnl is not None else None
            total_cost = float(s.closed_total_cost) if s.closed_total_cost is not None else None
            result_pct = float(s.result_pct) if s.result_pct is not None else None

            if reason not in by_reason_money:
                by_reason_money[reason] = {"count": 0, "net_pnl": 0.0, "costs": 0.0,
                                            "avg_result_pct": 0.0, "_result_values": []}
            by_reason_money[reason]["count"] += 1
            if net_pnl is not None:
                by_reason_money[reason]["net_pnl"] += net_pnl
                net_pnl_values.append(net_pnl)
            if total_cost is not None:
                by_reason_money[reason]["costs"] += total_cost
                costs_values.append(total_cost)
            if result_pct is not None:
                by_reason_money[reason]["_result_values"].append(result_pct)
                result_values.append(result_pct)

            stop_loss_count += reason == "stop_loss"
            breakeven_count += reason == "breakeven_stop"
            trailing_count += reason in trailing_reasons
            post_tp1_stop_count += reason in post_tp1_reasons
            mfe_capture_count += reason in mfe_capture_reasons
            tp2_count += reason == "tp2_reached"
            went_positive += bool(lifecycle.get("went_positive"))
            positive_then_negative += bool(lifecycle.get("positive_then_negative"))

            if lifecycle.get("mfe_pct") is not None:
                mfe_values.append(float(lifecycle["mfe_pct"]))
            if lifecycle.get("mae_pct") is not None:
                mae_values.append(float(lifecycle["mae_pct"]))
            if lifecycle.get("missed_profit_pct") is not None:
                missed_values.append(float(lifecycle["missed_profit_pct"]))

            items.append({
                "id": s.id, "symbol": s.symbol, "side": s.side, "grade": s.grade,
                "status": s.status, "result_pct": s.result_pct,
                "closed_reason": s.closed_reason, "closed_net_pnl": s.closed_net_pnl,
                "closed_total_cost": s.closed_total_cost, "has_lifecycle": has_lifecycle,
                "mfe_pct": lifecycle.get("mfe_pct"), "mae_pct": lifecycle.get("mae_pct"),
                "missed_profit_pct": lifecycle.get("missed_profit_pct"),
                "positive_then_negative": lifecycle.get("positive_then_negative"),
                "entry_price": lifecycle.get("entry_price"),
                "max_profit_price": lifecycle.get("max_profit_price"),
                "max_drawdown_price": lifecycle.get("max_drawdown_price"),
                "exit_price": lifecycle.get("exit_price"),
                "close_reason": lifecycle.get("close_reason") or s.closed_reason,
            })

        def avg(values):
            return round(sum(values) / len(values), 4) if values else 0.0

        for reason, row in by_reason_money.items():
            values = row.pop("_result_values", [])
            row["net_pnl"] = round(row["net_pnl"], 6)
            row["costs"] = round(row["costs"], 6)
            row["avg_result_pct"] = avg(values)

        lc = lifecycle_count or 1
        return {
            "status": "ok", "total_closed": total_closed,
            "lifecycle_count": lifecycle_count, "legacy_count": legacy_count,
            "only_lifecycle": only_lifecycle, "went_positive": went_positive,
            "positive_then_negative": positive_then_negative,
            "positive_then_negative_rate": round((positive_then_negative / lc * 100), 2) if lifecycle_count else 0.0,
            "stop_loss_count": stop_loss_count, "breakeven_count": breakeven_count,
            "trailing_count": trailing_count, "post_tp1_stop_count": post_tp1_stop_count,
            "mfe_capture_count": mfe_capture_count, "tp2_count": tp2_count,
            "tp2_rate": round((tp2_count / lc * 100), 2) if lifecycle_count else 0.0,
            "trailing_rate": round((trailing_count / lc * 100), 2) if lifecycle_count else 0.0,
            "post_tp1_stop_rate": round((post_tp1_stop_count / lc * 100), 2) if lifecycle_count else 0.0,
            "mfe_capture_rate": round((mfe_capture_count / lc * 100), 2) if lifecycle_count else 0.0,
            "avg_mfe_pct": avg(mfe_values), "avg_mae_pct": avg(mae_values),
            "avg_missed_profit_pct": avg(missed_values),
            "avg_result_pct": avg(result_values), "avg_net_pnl_usdt": avg(net_pnl_values),
            "avg_costs_usdt": avg(costs_values),
            "total_net_pnl_usdt": round(sum(net_pnl_values), 6),
            "total_costs_usdt": round(sum(costs_values), 6),
            "by_reason": by_reason, "by_reason_money": by_reason_money, "items": items,
        }
    finally:
        db.close()


@router.get("/grade-c-audit", dependencies=[Depends(require_owner_action)])
def analytics_grade_c_audit(date_from: str | None = None):
    db = SessionLocal()
    try:
        q = db.query(Signal)
        if date_from:
            dt = datetime.fromisoformat(str(date_from).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            q = q.filter(Signal.created_at >= dt)
        total = q.count()
        grade_c = q.filter(Signal.grade == "C").count()
        opened_like = q.filter(Signal.status.in_(["published", "opened", "tp1", "breakeven", "closed"]))
        opened_total = opened_like.count()
        opened_c = opened_like.filter(Signal.grade == "C").count()
        return {
            "status": "ok", "date_from": date_from, "total_signals": total,
            "grade_c_signals": grade_c,
            "grade_c_share_pct": round((grade_c / total * 100), 2) if total else 0.0,
            "opened_family_total": opened_total, "opened_family_grade_c": opened_c,
            "opened_family_grade_c_share_pct": round((opened_c / opened_total * 100), 2) if opened_total else 0.0,
        }
    finally:
        db.close()


@router.get("/daily-quality-report", dependencies=[Depends(require_owner_action)])
def analytics_daily_quality_report(hours: int = 24):
    db = SessionLocal()
    try:
        return DailyQualityReportService(hours=hours).build(
            db=db,
            equity_usdt=float(getattr(settings, "RISK_EQUITY_USDT", 1000.0)),
        )
    finally:
        db.close()
