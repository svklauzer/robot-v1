"""Действующая конфигурация и ОТКУДА взято каждое значение.

Зачем. Настройка живёт в двух местах: дефолт в `config.py` и перекрытие в
`render.yaml` → env. По коду не видно, какое из двух сейчас работает, и это
уже приводило к потере времени: правишь дефолт, деплоишь, а в проде значение
из блупринта, потому что ключ там прибит. Обратный случай тоже был — ключ в
блупринте есть, а поля в Settings нет, pydantic его молча игнорирует
(`extra="ignore"`), и настройка оказывается фикцией.

Здесь показывается ТРИ вещи по каждому параметру: что действует сейчас, каков
дефолт кода и кто победил — env или дефолт. Источник определяется наличием
имени в `os.environ`, а не сравнением значений: env, повторяющий дефолт,
формально тоже перекрытие, и знать об этом полезно (такие ключи — шум в
блупринте, их можно убрать).

Только чтение. Ничего не меняет и меняться отсюда не может: числовые пороги
торговли правятся коммитом, чтобы у каждой правки остались ревью, тесты и
причина. Это осознанное ограничение, а не недоделка.
"""
from __future__ import annotations

import os
import re
from typing import Any

from core.config import Settings, settings

# Значения этих полей наружу не отдаются никогда — только факт «задано/нет».
# Список по ПАТТЕРНУ, а не перечислением: новый ключ с секретом появится
# раньше, чем кто-то вспомнит дополнить перечень.
_SECRET_PATTERN = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|_KEY|APIKEY|API_KEY|PRIVATE|CREDENTIAL|DSN|WEBHOOK)",
    re.IGNORECASE,
)
# URL подключений содержат логин/пароль в теле строки.
_URL_PATTERN = re.compile(r"(DATABASE_URL|REDIS_URL|_URI$)", re.IGNORECASE)


def is_sensitive(name: str) -> bool:
    return bool(_SECRET_PATTERN.search(name) or _URL_PATTERN.search(name))


# Группировка по префиксу имени: 582 параметра плоским списком нечитаемы.
# Порядок важен — первое совпадение выигрывает, поэтому частные префиксы
# стоят раньше общих.
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Тренд / KAMA (ТЗ)", ("TZ_", "KAMA_", "TREND_")),
    ("Движки входа", ("ENABLE_", "RANGE_", "SCALP_", "CRT_", "REVERSAL_")),
    # (#ui-audit-2026-09-03) POST_TP1_ добавлен явно: защита прибыли между TP1 и
    # TP2 — это ВЫХОД, а по префиксу "POST_" она уезжала в «Прочее», где
    # торговый порог выглядит как случайная инфраструктурная переменная.
    ("Выходы", ("EXIT_", "BREAKEVEN_", "MFE_", "TP1_", "TP2_", "TP_", "POST_TP1_",
                "PROTECTIVE_", "FAILED_SETUP_")),
    # (#grade-axis-2026-09-04) GRADE_ — это про размер ставки, а не про качество
    # сигнала: флаг разрешает сайзинг по грейду, ось которого измерена
    # антипредсказательной. В «Прочем» предохранитель выглядит как техническая
    # переменная и снимается не глядя.
    ("Риск и сайзинг", ("RISK_", "MAX_POSITION", "MAX_USED", "SIZING_", "DYNAMIC_MARGIN",
                        "LEVERAGE", "MAX_LEVERAGE", "GRADE_AXIS")),
    # ANTI_CHOP_ — гейт входа, а не «прочее»: он решает, брать ли сделку вообще.
    ("Анти-дрейн и гейты", ("ANTI_DRAIN", "ANTI_CHOP_", "HTF_ALIGN", "RANGE_POS_",
                            "PROD_GATE", "SYMBOL_PERF", "REENTRY_", "POST_LOSS", "CORR_CLUSTER",
                            # GRADE_*_MIN_SCORE решают, КАКОЙ грейд получит сигнал,
                            # то есть это пороги отбора рядом с PROD_GATE_, а не
                            # сайзинг. GRADE_AXIS_ выше — про размер ставки, и
                            # совпадает раньше по порядку групп.
                            "GRADE_", "CONFIDENCE_")),
    ("Стакан / ликвидность", ("OB_", "ORDERBOOK", "LIQUIDITY", "DEPTH_", "SLIPPAGE")),
    # OKX_ и ACTIVE_EXCHANGE — там же, где HTX_: это маршрут исполнения.
    ("Комиссии и маршрут", ("SPOT_", "FUTURES_", "EXECUTION_", "MARKET_", "HTX_",
                            "OKX_", "ACTIVE_EXCHANGE", "KRAKEN_")),
    ("Арбитраж", ("FUNDING_ARB", "CROSS_FARB", "FUNDING_", "ARB_")),
    ("Grid", ("GRID_",)),
    ("ML", ("ML_",)),
    # LOOP_ — поведение самого торгового цикла (пульс отчёта о простое и т.п.).
    ("Live-исполнение", ("LIVE_", "ENABLE_LIVE", "ROBOT_MODE", "TRADING_MODE", "LOOP_")),
    ("Телеграм и отчёты", ("TELEGRAM_", "REPORT_", "DIGEST_", "SUBSCRIPTION_", "BILLING_", "PAYMENT_")),
    ("Инфраструктура", ("APP_", "DB_", "CORS_", "JWT_", "OWNER_", "PORT", "REDIS_", "DATABASE_")),
)


# Ключи, которые ОБЯЗАНЫ стоять в блупринте явно, даже если значение совпадает
# с дефолтом кода. Совпадение сегодня не делает их лишними: смысл записи не
# «перекрыть», а ЗАКРЕПИТЬ. Поменяется дефолт в config.py — прод не поедет.
#
# (#pinning-2026-08-21) Метрика «дубликат дефолта» изначально предлагала их к
# удалению, и это был опасный совет: в списке оказались ENABLE_LIVE_ORDERS и
# ROBOT_MODE (переключатели реальных денег), лимиты убытка и семь ключей, на
# которые прямо ссылается test_render_blueprint_enforces_capital_leak_entry_gates.
_PINNED_ON_PURPOSE: frozenset[str] = frozenset({
    # Реальные деньги: значение по умолчанию не должно решать за нас
    "ENABLE_LIVE_ORDERS", "ROBOT_MODE", "TRADING_MODE", "ENABLE_FUTURES",
    "LIVE_MAX_ORDER_NOTIONAL_USDT",
    # Аварийные выключатели
    "GRID_ENABLED", "GRID_KILL_SWITCH_ENABLED", "CROSS_FARB_ENABLED",
    "ENABLE_FUNDING_ARB", "ENABLE_RANGE_STRATEGY", "ENABLE_CRT_STRATEGY",
    "ENABLE_SCALP_STRATEGY", "ENABLE_ORDERBOOK_ENGINE", "ML_MODE",
    # Лимиты потерь и капитала
    "MAX_DAILY_LOSS_PCT", "MAX_DRAWDOWN_PCT", "RISK_EQUITY_USDT",
    "RISK_PER_TRADE_PCT", "MAX_POSITION_MARGIN_PCT",
    "ANTI_DRAIN_MAX_OPEN_POSITIONS", "MAX_TRADES_PER_DAY", "MAX_ACTIVE_SIGNALS",
    # Гейты течи капитала — закреплены тестом блупринта
    "TREND_TRIGGER_MODE", "TZ_MODE", "TP_REACH_MODE", "TP_REACH_EV_MARGIN",
    "REGIME_EXP_SIZING_ENABLED", "DYNAMIC_MARGIN_FAIR_SHARE",
    "DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE",
    # Что и где торгуем
    "HTX_SYMBOLS", "HTX_MARKET_TYPE",
})

# Правило вместо перечня: закрепляем ЛЮБОЙ выключатель, лимит и параметр
# торгового поведения. Перечислять поимённо бессмысленно — новый порог движка
# появится раньше, чем кто-то вспомнит дополнить список, и молча уедет в
# «можно удалять».
#
# Смысл закрепления: блупринт должен оставаться местом, по которому ЧИТАЕТСЯ
# поведение системы. Вычистив из него интересные ручки, мы не наведём порядок,
# а спрячем состояние в дефолты кода — ровно то, от чего уходим.
_PINNED_PREFIXES: tuple[str, ...] = (
    "TZ_", "KAMA_", "TREND_",          # трендовый контур: активно калибруется
    "PROD_GATE_",                       # гейты входа в деньги
    "TP_", "TP1_", "TP2_", "POST_TP1_", # цели, гейт достижимости, защита после TP1
    # (#ui-audit-2026-09-03) Гейты, решающие БРАТЬ ЛИ СДЕЛКУ. Без них страница
    # советовала вычистить из блупринта ANTI_CHOP_MIN_EMA_FAN_ATR (порог, вокруг
    # которого шла вся работа 03.09) и ACTIVE_EXCHANGE — переключатель биржи,
    # на которой торгуем. Совет «удалить, поведение не изменится» верен ровно до
    # первого изменения дефолта в коде, после чего прод молча уезжает.
    "ANTI_CHOP_", "HTF_ALIGN", "RANGE_POS_",
    "ACTIVE_EXCHANGE", "OKX_",          # маршрут исполнения: какая биржа и чем
    # (#grade-axis-2026-09-04) Предохранитель сайзинга по грейду. Совпадает с
    # дефолтом кода — и именно поэтому обязан стоять в блупринте явно: смена
    # дефолта на true молча разблокировала бы ставку на измеренно убыточное
    # ведро, без единой строки в диффе прода.
    "GRADE_AXIS_", "CONFIDENCE_",
    # (#loop-knobs-pinning-2026-09-04) Поведение самого цикла. Страница
    # советовала вычистить LOOP_SKIP_HEARTBEAT_SEC — ключ, задающий, как часто
    # цикл сообщает о простое. Ровно тот же промах, что утром был у ANTI_CHOP_
    # и POST_TP1_, повторённый через несколько часов: правило пополняется не
    # само, и каждый новый префикс надо заводить руками.
    "LOOP_",
    "VALIDATION_",                      # пороги вердикта готовности к live
    "BREAKEVEN_", "MFE_", "PROTECTIVE_",  # защита прибыли на выходе
    "CORR_CLUSTER", "SYMBOL_PERF_", "REENTRY_", "POST_LOSS", "ANTI_DRAIN",
    "RISK_", "MAX_", "MIN_",            # лимиты
    "ENABLE_", "ALLOW_",                # выключатели
    "LEARNING_",                        # условия входа в learning-режиме
    "SCALP_", "RANGE_", "CRT_",         # пороги альт-движков
    "GRID_", "FUNDING_ARB", "CROSS_FARB",
)
_PINNED_SUFFIXES: tuple[str, ...] = (
    "_ENABLED",   # любой тумблер поведения
    "_MODE",      # shadow/enforce и режимы исполнения
)


def is_pinned_on_purpose(name: str) -> bool:
    """Закреплять ли ключ в блупринте, даже если значение совпало с дефолтом."""
    if name in _PINNED_ON_PURPOSE:
        return True
    if any(name.startswith(p) for p in _PINNED_PREFIXES):
        return True
    return any(name.endswith(s) for s in _PINNED_SUFFIXES)


def _group_of(name: str) -> str:
    for label, prefixes in _GROUPS:
        if any(name.startswith(p) for p in prefixes):
            return label
    return "Прочее"


def _field_defaults() -> dict[str, Any]:
    fields = getattr(Settings, "model_fields", None) or getattr(Settings, "__fields__", {})
    out: dict[str, Any] = {}
    for name, field in fields.items():
        default = getattr(field, "default", None)
        # pydantic помечает обязательные поля специальным маркером
        if default.__class__.__name__ in ("PydanticUndefinedType", "UndefinedType"):
            default = None
        out[name] = default
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def effective_config(include_secrets_presence: bool = True) -> dict:
    """Все параметры: действующее значение, дефолт кода, источник.

    Секреты отдаются как `{"set": true}` без значения — страница конфигурации
    не должна становиться способом прочитать ключи биржи через браузер.
    """
    defaults = _field_defaults()
    groups: dict[str, list[dict]] = {}
    env_count = 0
    pinned: list[str] = []
    removable: list[str] = []

    for name, default in sorted(defaults.items()):
        from_env = name in os.environ
        if from_env:
            env_count += 1
        current = getattr(settings, name, None)
        secret = is_sensitive(name)

        row: dict[str, Any] = {
            "name": name,
            "source": "env" if from_env else "default",
            "secret": secret,
        }
        if secret:
            if include_secrets_presence:
                row["value"] = "задан" if current not in (None, "") else "не задан"
            row["default"] = None
        else:
            row["value"] = _jsonable(current)
            row["default"] = _jsonable(default)
            # env, повторяющий дефолт. Это НЕ автоматически мусор: для
            # выключателей и лимитов запись в блупринте — закрепление, а не
            # перекрытие. Удалять можно только то, что закреплять незачем.
            if from_env and _jsonable(current) == _jsonable(default):
                protected = is_pinned_on_purpose(name)
                row["pinned"] = True
                row["protected"] = protected
                pinned.append(name)
                if not protected:
                    removable.append(name)

        groups.setdefault(_group_of(name), []).append(row)

    return {
        "total": len(defaults),
        "from_env": env_count,
        "from_default": len(defaults) - env_count,
        # Совпадает с дефолтом кода. Делится на две ЗАЩИЩЁННЫЕ группы:
        #   protected — закреплено намеренно (выключатели, лимиты, гейты течи);
        #                удаление вернёт власть дефолту кода, это опасно;
        #   removable — закреплять нечего, можно вычистить из блупринта.
        "pinned_env_keys": sorted(pinned),
        "removable_env_keys": sorted(removable),
        "protected_env_keys": sorted(n for n in pinned if is_pinned_on_purpose(n)),
        "groups": [
            {"name": label, "items": groups[label]}
            for label, _ in _GROUPS
            if label in groups
        ] + ([{"name": "Прочее", "items": groups["Прочее"]}] if "Прочее" in groups else []),
    }
