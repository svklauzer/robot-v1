# apps/api/core/decision_codes.py

DECISION_NO_TRADE = "skip_no_trade_conditions"

DECISION_WATCH_LONG = "watch_long"
DECISION_WATCH_SHORT = "watch_short"
DECISION_WATCH_EXPIRED = "watch_expired"

DECISION_WAIT_CONFIRMATION = "candidate_but_wait_confirmation"
DECISION_WAIT_BETTER_ENTRY_RR = "wait_better_entry_rr"

DECISION_SETUP_TOO_LOW = "setup_quality_too_low"
DECISION_GRADE_TOO_LOW = "quality_grade_too_low"
DECISION_TRADE_PLAN_REJECTED = "trade_plan_rejected"

DECISION_SHORTS_DISABLED = "short_candidate_but_shorts_disabled"

DECISION_SIGNAL_PUBLISHED = "signal_published"
DECISION_SIGNAL_EXPIRED = "signal_expired"

DECISION_POSITION_OPENED = "position_opened"
DECISION_POSITION_ALREADY_OPEN = "position_already_open"

DECISION_TP1_REACHED = "tp1_reached"
DECISION_TP2_REACHED = "tp2_reached"
DECISION_STOP_LOSS = "stop_loss"
DECISION_BREAKEVEN_STOP = "breakeven_stop"

DECISION_READY_TO_PUBLISH = "ready_to_publish"
DECISION_PUBLISHED_SIGNAL_CREATED = "published_signal_created"
DECISION_ACTIVE_SIGNAL_ALREADY_EXISTS = "active_signal_already_exists"

DECISION_NET_RR_TOO_LOW = "net_rr_too_low"
DECISION_REQUIRED_MARGIN_EXCEEDS_BALANCE = "required_margin_exceeds_balance"

DECISION_WATCH_COOLDOWN = "watch_cooldown"

DECISION_MAX_ACTIVE_SIGNALS_REACHED = "max_active_signals_reached"
DECISION_REQUIRED_MARGIN_EXCEEDS_FREE_MARGIN = "required_margin_exceeds_free_margin"

DECISION_LABELS = {
    DECISION_NO_TRADE: "Нет торговых условий",

    DECISION_WATCH_LONG: "Наблюдение Long",
    DECISION_WATCH_SHORT: "Наблюдение Short",
    DECISION_WATCH_EXPIRED: "Наблюдение истекло",

    DECISION_WAIT_CONFIRMATION: "Кандидат ждёт подтверждения",
    DECISION_WAIT_BETTER_ENTRY_RR: "Ждём лучший вход по RR",

    DECISION_SETUP_TOO_LOW: "Качество сетапа низкое",
    DECISION_GRADE_TOO_LOW: "Grade ниже порога публикации",
    DECISION_TRADE_PLAN_REJECTED: "TradePlan отклонил сделку",

    DECISION_SHORTS_DISABLED: "Short заблокирован режимом spot",

    DECISION_SIGNAL_PUBLISHED: "Сигнал опубликован",
    DECISION_SIGNAL_EXPIRED: "Сигнал истёк",

    DECISION_POSITION_OPENED: "Позиция открыта",
    DECISION_POSITION_ALREADY_OPEN: "Позиция уже открыта",

    DECISION_TP1_REACHED: "TP1 достигнут",
    DECISION_TP2_REACHED: "TP2 достигнут",
    DECISION_STOP_LOSS: "Stop Loss",
    DECISION_BREAKEVEN_STOP: "Безубыток",

    DECISION_READY_TO_PUBLISH: "Готов к публикации",
    DECISION_PUBLISHED_SIGNAL_CREATED: "Сигнал создан",
    DECISION_ACTIVE_SIGNAL_ALREADY_EXISTS: "Активный сигнал уже существует",

    DECISION_NET_RR_TOO_LOW: "RR ниже минимального",
    DECISION_REQUIRED_MARGIN_EXCEEDS_BALANCE: "Недостаточно баланса под маржу",

    DECISION_WATCH_COOLDOWN: "Watch на паузе после истечения",

    DECISION_MAX_ACTIVE_SIGNALS_REACHED: "Достигнут лимит активных сигналов",
    DECISION_REQUIRED_MARGIN_EXCEEDS_FREE_MARGIN: "Недостаточно свободной маржи",    
}


def decision_label(decision: str | None) -> str:
    if not decision:
        return "Неизвестное решение"

    return DECISION_LABELS.get(decision, decision)