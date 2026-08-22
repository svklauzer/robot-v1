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


def test_totals_add_up():
    payload = ci.effective_config()
    assert payload["from_env"] + payload["from_default"] == payload["total"]
    assert len(_all_rows(payload)) == payload["total"]
