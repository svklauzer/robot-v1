"""Страница конфигурации не должна становиться способом прочитать ключи.

(#passphrase-leak-2026-09-07) `/system/config-effective` отдаёт все параметры с
действующими значениями. Секреты маскируются по ШАБЛОНУ имени — так задумано:
«новый ключ с секретом появится раньше, чем кто-то вспомнит дополнить перечень».

Шаблон при этом сам оказался у́же нужного: `PASSWORD|PASSWD` не покрывало
PASSPHRASE, и OKX_API_PASSPHRASE — третий из трёх ключей, которыми подписывается
торговый запрос на OKX, — уходил в ответ открытым текстом.
"""
from __future__ import annotations

import re

import pytest

from services.config_inspector import _field_defaults, effective_config, is_sensitive


# ── тот самый промах ────────────────────────────────────────────────────────

def test_the_okx_passphrase_is_masked():
    assert is_sensitive("OKX_API_PASSPHRASE"), "парольная фраза снова открыта"


@pytest.mark.parametrize("name", [
    "OKX_API_PASSPHRASE", "OKX_API_KEY", "OKX_API_SECRET",
    "HTX_API_KEY", "HTX_API_SECRET",
    "JWT_SECRET", "OWNER_PASSWORD", "OWNER_API_TOKEN",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
    "DATABASE_URL", "REDIS_URL",
    # (#config-audit-2026-09-12) Прокси с логином в URL и постоянная ссылка в
    # платный VIP-канал.
    "HTX_PROXY_URL", "OKX_PROXY_URL", "KRAKEN_PROXY_URL", "TELEGRAM_PROXY_URL",
    "VIP_INVITE_LINK",
])
def test_every_credential_is_masked(name):
    """Полный набор того, чем система подписывается, ходит в БД и авторизует
    владельца. Один незакрытый ключ здесь равен всем.
    """
    assert is_sensitive(name), f"{name} отдаётся значением"


# ── общий признак, а не перечень ────────────────────────────────────────────

def test_no_password_shaped_field_escapes_the_pattern():
    """Ровно тот класс, на котором шаблон и промахнулся. Проверяется по всем
    полям Settings, а не по списку из головы.
    """
    leaked = [n for n in _field_defaults()
              if re.search(r"PASS|SECRET|TOKEN|CREDENTIAL|PRIVATE", n, re.IGNORECASE)
              and not is_sensitive(n)]

    assert leaked == [], f"секретные по имени поля отдаются значением: {leaked}"


def test_masking_does_not_swallow_ordinary_thresholds():
    """Обратная сторона: слишком широкий шаблон спрятал бы торговые пороги, и
    страница перестала бы отвечать на вопрос, ради которого заведена.
    """
    for name in ("MAX_ACTIVE_SIGNALS", "TZ_ADX_MIN", "SIGNAL_PROFILE",
                 "FUNDING_ARB_MIN_SIGN_CONSISTENCY", "LEVELS_SIGNAL_TF",
                 # Публичные по замыслу: партнёрские ссылки и срок одноразовых
                 # приглашений.
                 "OKX_AFFILIATE_LINK", "HTX_AFFILIATE_LINK", "VIP_INVITE_EXPIRE_HOURS"):
        assert not is_sensitive(name), f"{name} спрятан без причины"


# ── и то, что реально уходит наружу ─────────────────────────────────────────

def test_the_response_carries_presence_not_values():
    """Проверка на самом ответе, а не только на предикате: маскирование обязано
    доживать до сериализации.
    """
    out = effective_config()
    rows = {r["name"]: r for g in out["groups"] for r in g["items"]}

    row = rows["OKX_API_PASSPHRASE"]
    assert row["secret"] is True
    assert row["value"] in ("задан", "не задан"), "в ответе лежит значение"
    assert row.get("default") is None, "дефолт секрета тоже наружу не идёт"


# ── откуда взято значение ───────────────────────────────────────────────────

def test_the_page_does_not_claim_the_blueprint_it_cannot_see():
    """(#three-places-2026-09-07) Мест три: config.py, render.yaml и дашборд
    Render. `source` различает только «окружение или дефолт» — блупринт и
    дашборд в окружении процесса неотличимы.

    Страница подписывала env как «перекрыто render.yaml». Совет «поправь
    render.yaml» на дашбордном ключе не сработает, и один раз это уже стоило
    времени: OKX_MARKET_TYPE не было ни в блупринте, ни в дашборде.
    """
    from pathlib import Path

    web = Path(__file__).resolve().parents[3] / "apps" / "web"
    page = (web / "app/config/page.tsx").read_text(encoding="utf-8")

    label = page[page.index('label="Задано в окружении"'):]
    hint = label[:label.index("/>")]
    assert "ИЛИ дашборд" in hint, "карточка снова приписывает значение блупринту"
    assert "перекрыто render.yaml —" not in page, "старая подпись вернулась"
