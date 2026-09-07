"""Заслон owner-дашборда и его видимость (#dashboard-auth-visible-2026-09-07).

Прокси `/api/proxy/*` подставляет OWNER_API_TOKEN на сервере: он есть у сервиса,
а не у браузера. Значит право владельца даёт не токен, а сам доступ к веб-адресу
— Start/Stop, kill-switch, закрытие позиций, платежи, подписчики.

Единственный заслон — Basic Auth в `middleware.ts`, и он намеренно fail-open:
при незаданных BASIC_AUTH_USER/PASS пропускает всех, чтобы владелец не запер сам
себя до настройки. Компромисс разумный, но его следствие обязано быть видно.
"""
from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parents[3] / "apps" / "web"
API = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── сам заслон ──────────────────────────────────────────────────────────────

def test_the_guard_covers_the_proxy_not_just_the_pages():
    """Закрыть только страницы недостаточно: прокси — отдельный вход, и именно
    он несёт токен. Матчер обязан покрывать всё, кроме статики.
    """
    mw = _read(WEB / "middleware.ts")

    assert "_next/static" in mw and "favicon" in mw, "матчер изменился — проверить охват"
    assert "/api/proxy" not in mw.split("matcher")[1].split("]")[0], (
        "прокси исключён из-под заслона"
    )
    assert "WWW-Authenticate" in mw, "нет вызова авторизации — браузер не спросит пароль"


def test_credentials_are_compared_in_constant_time():
    """Сравнение по `==` утекает совпадение по таймингу; пароль от дашборда,
    который останавливает робота, того не стоит.
    """
    mw = _read(WEB / "middleware.ts")

    assert "timingSafeEqual" in mw
    body = mw[mw.index("function timingSafeEqual"):]
    assert "^" in body and "|=" in body, "сравнение перестало быть постоянным по времени"


# ── видимость отказа ────────────────────────────────────────────────────────

def test_the_open_door_is_visible_on_every_screen():
    """Предохранитель, о состоянии которого нельзя спросить, — это тот, про
    который узнают постфактум. Полоса живёт в AppShell, то есть на всех
    страницах, рядом с баннером режима.
    """
    assert (WEB / "app/api/auth-state/route.ts").exists(), "спросить состояние нечем"
    shell = _read(WEB / "components/AppShell.tsx")
    assert "AuthGuardBanner" in shell, "предупреждение не на каждой странице"

    banner = _read(WEB / "components/AuthGuardBanner.tsx")
    assert "basic_auth_configured" in banner
    assert "false" in banner, "полоса показывается не по факту отсутствия заслона"


def test_the_auth_state_route_leaks_nothing_but_a_boolean():
    """Роут отвечает на вопрос «дверь заперта?» и ничего больше: ни имени, ни
    пароля, ни длины. При незапертой двери его прочтёт кто угодно.
    """
    route = _read(WEB / "app/api/auth-state/route.ts")

    assert "BASIC_AUTH_PASS" in route, "состояние берётся не из той переменной"
    assert "Boolean(" in route, "наружу уходит не булев ответ"
    for leak in ("process.env.BASIC_AUTH_USER}", "value", "length"):
        assert f"json({{ {leak}" not in route


# ── бэкенд закрыт независимо ────────────────────────────────────────────────

def test_the_api_itself_fails_closed_in_production():
    """Второй рубеж: даже с открытым дашбордом прямой доступ к API требует
    токена. Fail-open там только вне production и только при незаданном токене.
    """
    sec = _read(API / "core/security.py")

    assert 'raise HTTPException(status_code=503, detail="owner_api_token_not_configured")' in sec
    assert 'raise HTTPException(status_code=401, detail="owner_auth_required")' in sec
    assert 'settings.APP_ENV != "production"' in sec, "послабление больше не привязано к среде"
