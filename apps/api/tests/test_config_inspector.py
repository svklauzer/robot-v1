"""Страница конфигурации: видно всё, кроме секретов (#config-visibility-2026-08-21).

Главный риск такой страницы — превратиться в способ прочитать ключи биржи через
браузер. Поэтому маскировка проверяется отдельно и по паттерну имени, а не по
перечню: новый ключ с секретом появится раньше, чем кто-то вспомнит дополнить
список.
"""
from __future__ import annotations

import os

from services import config_inspector as ci


def _all_rows(payload: dict) -> list[dict]:
    return [row for group in payload["groups"] for row in group["items"]]


def test_secrets_are_never_exposed():
    payload = ci.effective_config()
    for row in _all_rows(payload):
        if row["secret"]:
            assert row["value"] in ("задан", "не задан"), (
                f"{row['name']}: значение секрета не должно отдаваться наружу"
            )
            assert row["default"] is None


def test_known_secret_names_are_classified_as_secret():
    for name in ("JWT_SECRET", "HTX_API_KEY", "HTX_API_SECRET",
                 "TELEGRAM_BOT_TOKEN", "OWNER_PASSWORD", "DATABASE_URL"):
        assert ci.is_sensitive(name) is True, f"{name} обязан быть замаскирован"


def test_plain_settings_are_not_masked():
    for name in ("TZ_ADX_MIN", "RISK_PER_TRADE_PCT", "GRID_ENABLED"):
        assert ci.is_sensitive(name) is False


def test_source_reflects_environment(monkeypatch):
    """Источник определяется наличием ключа в env, а не совпадением значений."""
    monkeypatch.setenv("TZ_ADX_MIN", "15.0")
    payload = ci.effective_config()
    row = next(r for r in _all_rows(payload) if r["name"] == "TZ_ADX_MIN")
    assert row["source"] == "env"

    monkeypatch.delenv("TZ_ADX_MIN", raising=False)
    payload = ci.effective_config()
    row = next(r for r in _all_rows(payload) if r["name"] == "TZ_ADX_MIN")
    assert row["source"] == "default"


def test_safety_pins_are_never_offered_for_removal():
    """Совпадение с дефолтом не делает выключатель лишним (#pinning-2026-08-21).

    Первая версия метрики предлагала к удалению ENABLE_LIVE_ORDERS, ROBOT_MODE,
    лимиты убытка и семь ключей, закреплённых тестом блупринта. Удалить их —
    значит вернуть власть над реальными деньгами дефолту в config.py.
    """
    payload = ci.effective_config()
    removable = set(payload["removable_env_keys"])

    for name in ("ENABLE_LIVE_ORDERS", "ROBOT_MODE", "GRID_KILL_SWITCH_ENABLED",
                 "MAX_DAILY_LOSS_PCT", "MAX_DRAWDOWN_PCT", "ML_MODE",
                 "TZ_MODE", "TREND_TRIGGER_MODE", "TP_REACH_MODE"):
        assert name not in removable, f"{name} — предохранитель, удалять нельзя"


def test_calibrated_trading_params_stay_pinned():
    """Активно калибруемые пороги — тоже не шум.

    Владелец крутит TZ_ADX_MIN, зону Stoch и стопы движка по данным. Убрать их
    из блупринта — значит спрятать поведение системы в дефолты кода, где его
    никто не читает, и потерять защиту от случайной правки дефолта.
    """
    payload = ci.effective_config()
    removable = set(payload["removable_env_keys"])

    for name in ("TZ_ADX_MIN", "TZ_STOCH_ZONE", "TZ_DISASTER_STOP_PCT",
                 "TZ_STOP_MIN_DIST_ATR_MULT", "TZ_EXIT_CONDITIONS",
                 "TREND_MAX_EXTENSION_ATR", "KAMA_SLOW",
                 "CORR_CLUSTER_PORTFOLIO_MAX_SAME_DIR", "NEWS_ENABLED",
                 "PROD_GATE_A_MIN_SETUP", "BREAKEVEN_LOCK_ENABLED",
                 "SYMBOL_PERF_BLOCK_MAX_WINRATE"):
        assert name not in removable, f"{name} — параметр поведения, закрепляем"


def test_removable_and_protected_partition_pinned():
    payload = ci.effective_config()
    pinned = set(payload["pinned_env_keys"])
    assert set(payload["removable_env_keys"]) | set(payload["protected_env_keys"]) == pinned
    assert not (set(payload["removable_env_keys"]) & set(payload["protected_env_keys"]))


def test_totals_add_up():
    payload = ci.effective_config()
    assert payload["from_env"] + payload["from_default"] == payload["total"]
    assert len(_all_rows(payload)) == payload["total"]
