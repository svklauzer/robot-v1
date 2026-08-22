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
    ("Выходы", ("EXIT_", "BREAKEVEN_", "MFE_", "TP1_", "TP2_", "TP_", "PROTECTIVE_", "FAILED_SETUP_")),
    ("Риск и сайзинг", ("RISK_", "MAX_POSITION", "MAX_USED", "SIZING_", "DYNAMIC_MARGIN", "LEVERAGE", "MAX_LEVERAGE")),
    ("Анти-дрейн и гейты", ("ANTI_DRAIN", "PROD_GATE", "SYMBOL_PERF", "REENTRY_", "POST_LOSS", "CORR_CLUSTER")),
    ("Стакан / ликвидность", ("OB_", "ORDERBOOK", "LIQUIDITY", "DEPTH_", "SLIPPAGE")),
    ("Комиссии и маршрут", ("SPOT_", "FUTURES_", "EXECUTION_", "MARKET_", "HTX_", "KRAKEN_")),
    ("Арбитраж", ("FUNDING_ARB", "CROSS_FARB", "FUNDING_", "ARB_")),
    ("Grid", ("GRID_",)),
    ("ML", ("ML_",)),
    ("Live-исполнение", ("LIVE_", "ENABLE_LIVE", "ROBOT_MODE", "TRADING_MODE")),
    ("Телеграм и отчёты", ("TELEGRAM_", "REPORT_", "DIGEST_", "SUBSCRIPTION_", "BILLING_", "PAYMENT_")),
    ("Инфраструктура", ("APP_", "DB_", "CORS_", "JWT_", "OWNER_", "PORT", "REDIS_", "DATABASE_")),
)


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
    redundant: list[str] = []

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
            # env, повторяющий дефолт: перекрытия по смыслу нет, только шум
            # в блупринте. Помечаем, чтобы такие ключи можно было вычистить.
            if from_env and _jsonable(current) == _jsonable(default):
                row["redundant"] = True
                redundant.append(name)

        groups.setdefault(_group_of(name), []).append(row)

    return {
        "total": len(defaults),
        "from_env": env_count,
        "from_default": len(defaults) - env_count,
        # Ключи, где env дублирует дефолт один в один — кандидаты на удаление
        # из render.yaml. Не ошибка, но лишний шум в блупринте.
        "redundant_env_keys": sorted(redundant),
        "groups": [
            {"name": label, "items": groups[label]}
            for label, _ in _GROUPS
            if label in groups
        ] + ([{"name": "Прочее", "items": groups["Прочее"]}] if "Прочее" in groups else []),
    }
