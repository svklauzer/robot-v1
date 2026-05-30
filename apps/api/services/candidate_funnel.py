from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from models.bot import Bot
from models.intelligence_event import IntelligenceEvent
from models.signal import Signal
from models.telegram_delivery import TelegramDelivery

ACTIVE_SIGNAL_STATUSES = ["published", "opened", "tp1", "breakeven"]
TERMINAL_SIGNAL_STATUSES = ["closed", "expired", "rejected", "telegram_failed"]

PUBLISH_SUCCESS_DECISIONS = {
    "published_signal_created",
    "signal_published",
    "published_by_priority_queue",
}

PUBLISH_READY_DECISIONS = {
    "ready_to_publish",
}

KNOWN_BLOCKING_DECISIONS = {
    "quality_grade_too_low",
    "setup_quality_too_low",
    "wait_better_entry_rr",
    "net_rr_too_low",
    "trade_plan_rejected",
    "required_margin_exceeds_balance",
    "required_margin_exceeds_free_margin",
    "max_active_signals_reached",
    "active_signal_already_exists",
    "short_candidate_but_shorts_disabled",
    "grade_c_learning_only_not_publishable",
    "grade_c_blocked_before_signal_create",
    "a_rr_tp1_too_low",
    "a_plus_rr_tp1_too_low",
    "b_priority_too_low",
    "symbol_cooldown_failed_setup_streak",
    "reentry_cooldown_same_side",
    "initial_telegram_publish_failed",
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _hours_between(start: Any, end: Any) -> float | None:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return round((end - start).total_seconds() / 3600, 2)


def build_candidate_funnel_diagnosis(
    *,
    readonly_scan_hits: int,
    bot_running: bool,
    ready_candidates: int,
    published_recent: int,
    telegram_failed_signals: int,
    telegram_failed_deliveries: int,
    latest_event_newer_than_signal: bool,
    top_blockers: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Return operator-facing reasons/actions for candidates not reaching published/open."""

    reasons: list[str] = []
    actions: list[str] = []

    if readonly_scan_hits > 0:
        reasons.append(
            "UI дергает GET /intelligence/scan: это readonly-скан, он не создает Signal и не публикует Telegram."
        )
        actions.append("Для фактической публикации запускай POST /intelligence/scan/run или включай фонового бота.")

    if not bot_running:
        reasons.append("Main Robot не в статусе running: фоновый контур publish/open не будет продвигать кандидатов.")
        actions.append("Переведи бота в running перед длительным наблюдением или используй ручной publish-run endpoint.")

    if ready_candidates > 0 and published_recent == 0:
        reasons.append("Есть ready_to_publish кандидаты, но за выбранное окно нет published-сигналов.")
        actions.append("Смотри top_blockers/priority_publish_status и запускай publish-run под lock без readonly режима.")

    if latest_event_newer_than_signal:
        reasons.append("Последние intelligence events свежее последнего Signal: сканирование идет, но сигналов не добавляется.")
        actions.append("Проверь, что используется publish endpoint, а не только мониторинговый scan endpoint.")

    if telegram_failed_signals > 0 or telegram_failed_deliveries > 0:
        reasons.append("Есть Telegram failures: сигнал может создаться и сразу стать telegram_failed вместо active published.")
        actions.append("Проверь сеть/бот-token/chat_id и повтори публикацию после восстановления Telegram delivery.")

    if top_blockers:
        blocker_names = ", ".join(str(item.get("decision")) for item in top_blockers[:3])
        reasons.append(f"Главные блокеры кандидатов в текущем окне: {blocker_names}.")
        actions.append("Для каждого blocker decision снижай риск точечно: RR/entry pullback, quality gate, exposure или reentry cooldown.")

    if not reasons:
        reasons.append("Явного блокера в последних событиях нет; нужен свежий publish-run с diagnostics.ranked.")
        actions.append("Запусти POST /intelligence/scan/run и сравни diagnostics.decision_counts с funnel.top_blockers.")

    return {"reasons": reasons, "actions": actions}


class CandidateFunnelService:
    def summarize(self, db: Session, limit: int = 120) -> dict[str, Any]:
        limit = min(max(int(limit or 120), 1), 500)

        bot = db.query(Bot).filter(Bot.name == "Main Robot").first()
        bot_running = bool(bot and bot.status == "running")

        events = (
            db.query(IntelligenceEvent)
            .order_by(IntelligenceEvent.id.desc())
            .limit(limit)
            .all()
        )
        signals = db.query(Signal).order_by(Signal.id.desc()).limit(limit).all()

        decision_counts = Counter(str(e.decision or "unknown") for e in events)
        status_counts = Counter(str(e.status or "unknown") for e in events)
        signal_status_counts = Counter(str(s.status or "unknown") for s in signals)

        ready_candidates = sum(decision_counts[d] for d in PUBLISH_READY_DECISIONS)
        published_recent = sum(
            signal_status_counts[status]
            for status in ACTIVE_SIGNAL_STATUSES
        )
        telegram_failed_signals = signal_status_counts.get("telegram_failed", 0)

        latest_event = events[0] if events else None
        latest_signal = signals[0] if signals else None
        latest_event_newer_than_signal = False
        if latest_event and latest_signal:
            latest_event_newer_than_signal = bool(latest_event.created_at > latest_signal.created_at)
        elif latest_event and not latest_signal:
            latest_event_newer_than_signal = True

        top_blockers = [
            {"decision": decision, "count": count}
            for decision, count in decision_counts.most_common()
            if decision in KNOWN_BLOCKING_DECISIONS
        ][:10]

        readonly_scan_hits = 1 if latest_event_newer_than_signal or events else 0

        delivery_counts = Counter()
        telegram_failed_deliveries = 0
        try:
            delivery_rows = db.query(TelegramDelivery).order_by(TelegramDelivery.id.desc()).limit(limit).all()
            delivery_counts = Counter(str(row.status or "unknown") for row in delivery_rows)
            telegram_failed_deliveries = delivery_counts.get("failed", 0)
        except Exception:
            delivery_rows = []

        diagnosis = build_candidate_funnel_diagnosis(
            readonly_scan_hits=readonly_scan_hits,
            bot_running=bot_running,
            ready_candidates=ready_candidates,
            published_recent=published_recent,
            telegram_failed_signals=telegram_failed_signals,
            telegram_failed_deliveries=telegram_failed_deliveries,
            latest_event_newer_than_signal=latest_event_newer_than_signal,
            top_blockers=top_blockers,
        )

        return {
            "status": "ok",
            "limit": limit,
            "bot": {
                "found": bot is not None,
                "status": bot.status if bot else None,
                "mode": bot.mode if bot else None,
                "running": bot_running,
                "symbols": (bot.config_json or {}).get("symbols", []) if bot else [],
            },
            "events": {
                "sample": len(events),
                "latest_id": latest_event.id if latest_event else None,
                "latest_at": _iso(latest_event.created_at) if latest_event else None,
                "hours_since_oldest_in_sample": _hours_between(events[-1].created_at, events[0].created_at) if len(events) > 1 else None,
                "status_counts": dict(status_counts),
                "decision_counts": dict(decision_counts.most_common(30)),
                "ready_candidates": ready_candidates,
                "top_blockers": top_blockers,
            },
            "signals": {
                "sample": len(signals),
                "latest_id": latest_signal.id if latest_signal else None,
                "latest_at": _iso(latest_signal.created_at) if latest_signal else None,
                "status_counts": dict(signal_status_counts),
                "active_like": sum(signal_status_counts[s] for s in ACTIVE_SIGNAL_STATUSES),
                "terminal_like": sum(signal_status_counts[s] for s in TERMINAL_SIGNAL_STATUSES),
                "telegram_failed": telegram_failed_signals,
            },
            "telegram_delivery": {
                "sample": len(delivery_rows),
                "status_counts": dict(delivery_counts),
                "failed": telegram_failed_deliveries,
            },
            "gap": {
                "latest_event_newer_than_signal": latest_event_newer_than_signal,
                "latest_event_at": _iso(latest_event.created_at) if latest_event else None,
                "latest_signal_at": _iso(latest_signal.created_at) if latest_signal else None,
            },
            "diagnosis": diagnosis,
        }
