"""Контракт авторизации: ни один маршрут не открыт наружу по недосмотру.

Тест разбирает ВСЕ маршруты приложения — и объявленные в main.py, и вынесенные
в routers/*.py. Прежняя версия искала маршруты только в main.py: после выноса
роутеров `next(...)` переставал их находить, тест падал со StopIteration, и
проверка «каждый owner-эндпоинт требует токен» много релизов не выполнялась
вообще, оставаясь при этом в CI зелёным ожиданием.

Публичным маршрут становится только через явное добавление в PUBLIC_ROUTES —
с обоснованием, почему он обязан быть публичным.
"""
from __future__ import annotations

import re
from pathlib import Path

API = Path(__file__).resolve().parents[1]

# Маршруты, доступные без owner-токена. Каждый — осознанное решение.
PUBLIC_ROUTES = {
    ("get", "/"): "health-probe Render",
    ("head", "/"): "health-probe Render",
    ("get", "/health"): "healthCheckPath Render, до маршрутизации трафика",
    ("get", "/payments/plans"): "витрина тарифов для бота, данных клиента не отдаёт",
    # Вебхук не может нести owner-токен: его дёргает Telegram. Подлинность
    # проверяется секретом setWebhook внутри хендлера (verify_telegram_webhook).
    ("post", "/telegram/webhook"): "внешний вызов Telegram, защищён secret_token",
}

ROUTE_RE = re.compile(
    r'^@(?:app|router)\.(get|post|put|delete|patch|api_route)\(\s*"([^"]*)"(.*)$'
)


def _prefix_of(source: str) -> str:
    match = re.search(r'APIRouter\((?:[^)]*?)prefix="([^"]*)"', source, re.S)
    return match.group(1) if match else ""


def _collect_routes() -> list[tuple[str, str, str, str]]:
    """(method, path, decorator_line, file) по всему приложению."""
    files = [API / "main.py"] + sorted((API / "routers").glob("*.py"))
    routes: list[tuple[str, str, str, str]] = []

    for path in files:
        if not path.exists() or not path.stat().st_size:
            continue
        source = path.read_text(encoding="utf-8")
        prefix = _prefix_of(source) if path.name != "main.py" else ""

        lines = source.splitlines()
        for index, line in enumerate(lines):
            match = ROUTE_RE.match(line)
            if not match:
                continue
            verb, route, tail = match.groups()

            # Декоратор может занимать несколько строк — дочитываем до баланса скобок.
            block = line
            cursor = index
            while block.count("(") > block.count(")") and cursor + 1 < len(lines):
                cursor += 1
                block += lines[cursor]

            full = f"{prefix}{route}" or "/"
            if verb == "api_route":
                methods = re.findall(r'"(GET|POST|PUT|DELETE|PATCH|HEAD)"', block)
                for method in methods or ["GET"]:
                    routes.append((method.lower(), full, block, path.name))
            else:
                routes.append((verb, full, block, path.name))

    return routes


def test_route_table_is_discovered():
    """Страховка от «тест позеленел, потому что ничего не нашёл»."""
    routes = _collect_routes()
    assert len(routes) > 60, f"найдено всего {len(routes)} маршрутов — разбор сломался"
    paths = {route for _, route, _, _ in routes}
    for expected in ("/bot/start", "/system/kill-switch", "/payments/checkout", "/telegram/webhook"):
        assert expected in paths, f"маршрут {expected} не найден — разбор неполон"


def test_every_route_requires_owner_auth_unless_explicitly_public():
    routes = _collect_routes()
    leaked: list[str] = []

    for method, route, decorator, filename in routes:
        if (method, route) in PUBLIC_ROUTES:
            continue
        if "require_owner_action" in decorator:
            continue
        leaked.append(f"{method.upper()} {route}  ({filename})")

    assert not leaked, (
        "маршрут доступен без owner-токена и не объявлен публичным явно:\n  "
        + "\n  ".join(sorted(leaked))
    )


def test_public_routes_are_still_public():
    """Обратная сторона: перекрыв вебхук токеном, мы отключим бота целиком."""
    routes = {(m, r): d for m, r, d, _ in _collect_routes()}

    webhook = routes.get(("post", "/telegram/webhook"))
    assert webhook is not None, "вебхук Telegram исчез"
    assert "require_owner_action" not in webhook, (
        "вебхук закрыт owner-токеном — Telegram его не имеет, бот перестанет работать"
    )

    plans = routes.get(("get", "/payments/plans"))
    assert plans is not None and "require_owner_action" not in plans


def test_debug_routes_are_disabled_in_production():
    """Force-эндпоинты создают сигналы и позиции — в проде их быть не должно."""
    routes = _collect_routes()
    unguarded = [
        f"{m.upper()} {r}"
        for m, r, d, _ in routes
        if (r.startswith("/robot/force") or r.startswith("/debug/") or r == "/robot/debug-signals")
        and "require_non_production_debug" not in d
    ]
    assert not unguarded, f"debug-маршрут без прод-гейта: {unguarded}"
