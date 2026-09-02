"""Контрольный хост не должен создавать ложную тревогу
(#diag-control-role-2026-07-27).

Баг. `_probe_host` дёргал у ВСЕХ хостов путь `/v1/common/timestamp` — публичный
эндпоинт HTX. У Kraken и Telegram такого пути нет, они отвечают 302 и 404, и
критерий `ok = status == 200` красил живые хосты в красный с подписью
«биржа отвечает ошибкой».

Цена ошибки не косметическая: контрольная группа существует ровно для того,
чтобы отличить «лежит биржа» от «лежит наш egress». Витрина, на которой
здоровый Kraken выглядит упавшим, ведёт ровно к тому выводу, от которого
инцидент 26.07 нас и должен был отучить.
"""
from __future__ import annotations

import pytest

from services import exchange_diagnostics as diag


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def net_ok(monkeypatch):
    """Сеть исправна на всех стадиях; различается только HTTP-ответ."""
    monkeypatch.setattr(
        diag.socket, "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("1.2.3.4", 443))],
    )

    class _Sock:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(diag.socket, "create_connection", lambda *a, **k: _Sock())

    class _Ctx:
        def wrap_socket(self, sock, server_hostname=None): return _Sock()

    monkeypatch.setattr(diag.ssl, "create_default_context", lambda *a, **k: _Ctx())


def _patch_http(monkeypatch, status: int, body: str = ""):
    import httpx

    captured: dict = {}

    def _get(url, **kwargs):
        captured["url"] = url
        return _Resp(status, body)

    monkeypatch.setattr(httpx, "get", _get)
    return captured


def test_control_host_302_is_success(net_ok, monkeypatch):
    """Живой Kraken отдаёт редирект — это доказательство сети, а не сбой."""
    _patch_http(monkeypatch, 302)
    step = diag._probe_host("futures.kraken.com", timeout=1.0, role="control")

    assert step["http"]["ok"] is True, "302 у контрольного хоста — успех"
    assert step["http"]["status"] == 302
    assert "ошибк" not in step["verdict"].lower(), (
        f"вердикт не должен обвинять живой хост: {step['verdict']}"
    )


def test_control_host_404_is_success(net_ok, monkeypatch):
    """Telegram на корне отдаёт 404 — сеть дошла."""
    _patch_http(monkeypatch, 404)
    step = diag._probe_host("api.telegram.org", timeout=1.0, role="control")
    assert step["http"]["ok"] is True


def test_control_host_is_not_probed_with_htx_path(net_ok, monkeypatch):
    """Корень вместо эндпоинта HTX — иначе проверяем не то, что думаем."""
    captured = _patch_http(monkeypatch, 200)
    diag._probe_host("futures.kraken.com", timeout=1.0, role="control")
    assert "/v1/common/timestamp" not in captured["url"], (
        "контрольный хост опрашивался чужим для него путём HTX"
    )


def test_exchange_host_still_requires_200(net_ok, monkeypatch):
    """Послабление не должно протечь на биржу: там 200 или ничего."""
    _patch_http(monkeypatch, 404)
    step = diag._probe_host("api.huobi.pro", timeout=1.0, role="exchange")
    assert step["http"]["ok"] is False


def test_exchange_geo_block_keeps_actionable_verdict(net_ok, monkeypatch):
    """403/451 — гео-блокировка ДЦ; вердикт обязан называть решение."""
    _patch_http(monkeypatch, 451)
    step = diag._probe_host("api.huobi.pro", timeout=1.0, role="exchange")
    assert step["http"]["ok"] is False
    assert "HTX_PROXY_URL" in step["verdict"]


# ── OKX (#okx-satellite-2026-09-02): та же диагностика, свой публичный путь ──

def test_okx_probe_uses_its_own_path_not_htx(net_ok, monkeypatch):
    captured = _patch_http(monkeypatch, 200)
    diag._probe_host("www.okx.com", timeout=1.0, role="exchange", path="/api/v5/public/time")
    assert "/api/v5/public/time" in captured["url"]
    assert "/v1/common/timestamp" not in captured["url"]


def test_default_path_is_unchanged_when_no_override_given(net_ok, monkeypatch):
    """Существующие вызовы без `path=` не должны увидеть поведенческих
    изменений — путь HTX остаётся дефолтом."""
    captured = _patch_http(monkeypatch, 200)
    diag._probe_host("api.huobi.pro", timeout=1.0, role="exchange")
    assert "/v1/common/timestamp" in captured["url"]


def test_diagnose_okx_returns_okx_flavored_shape(net_ok, monkeypatch):
    _patch_http(monkeypatch, 200)
    result = diag.diagnose_okx(timeout=1.0)
    assert result["status"] == "ok"
    assert "circuit" in result
    assert result["any_host_reachable"] is True
    assert "OKX" in result["note"]


def test_diagnose_all_returns_both_exchanges_and_active_flag(net_ok, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "ACTIVE_EXCHANGE", "okx", raising=False)
    _patch_http(monkeypatch, 200)
    result = diag.diagnose_all(timeout=1.0)
    assert result["active_exchange"] == "okx"
    assert "htx" in result and "okx" in result
    assert result["htx"]["status"] == "ok"
    assert result["okx"]["status"] == "ok"
