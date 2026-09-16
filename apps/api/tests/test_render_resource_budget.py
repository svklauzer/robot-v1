"""Трафик и память в рамках тарифа Render (#memory-probe-2026-09-16).

16.09 два письма Render: исчерпан лимит трафика 5 ГБ, robot-api перезапущен за
превышение памяти 512 МБ, а в логах приложения пусто. Разбор показал, что оба
расхода — от дашборда, а не от торговли: прокси ходил в API по публичному
адресу (двойная оплата), страницы опрашивали API в свёрнутой вкладке, readonly
скан пересчитывал всё на каждый вызов, журнал egress разбирался целиком раз в
минуту. Торговые циклы ни одна правка не трогает.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from core.config import settings

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "apps" / "web"


def _web(rel: str) -> str:
    path = WEB / rel
    if not path.exists():
        pytest.skip("нет фронтенда")
    return path.read_text(encoding="utf-8")


# ── замер памяти ────────────────────────────────────────────────────────────

def _fake_files(monkeypatch, files: dict[str, str]):
    from services import memory_probe

    monkeypatch.setattr(memory_probe, "_read", lambda path: files.get(path))
    return memory_probe


STATUS = "Name:\tuvicorn\nVmHWM:\t  409600 kB\nVmRSS:\t  307200 kB\n"


def test_memory_reads_process_and_cgroup_v2(monkeypatch):
    mp = _fake_files(monkeypatch, {
        "/proc/self/status": STATUS,
        "/sys/fs/cgroup/memory.current": str(400 * 1024 * 1024),
        "/sys/fs/cgroup/memory.max": str(512 * 1024 * 1024),
        "/sys/fs/cgroup/memory.peak": str(500 * 1024 * 1024),
    })
    mem = mp.read_memory()
    assert mem["rss_mb"] == 300.0 and mem["rss_peak_mb"] == 400.0
    assert mem["container_used_mb"] == 400.0 and mem["container_limit_mb"] == 512.0
    assert mem["container_peak_mb"] == 500.0
    assert mem["container_used_share"] == pytest.approx(400 / 512, abs=1e-3)


def test_memory_falls_back_to_cgroup_v1(monkeypatch):
    mp = _fake_files(monkeypatch, {
        "/sys/fs/cgroup/memory/memory.usage_in_bytes": str(256 * 1024 * 1024),
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": str(512 * 1024 * 1024),
    })
    mem = mp.read_memory()
    assert mem["container_used_share"] == pytest.approx(0.5)
    assert mem["rss_mb"] is None


def test_an_unlimited_or_missing_cgroup_gives_no_share(monkeypatch):
    mp = _fake_files(monkeypatch, {
        "/sys/fs/cgroup/memory.current": str(100 * 1024 * 1024),
        "/sys/fs/cgroup/memory.max": "max",
    })
    assert mp.read_memory()["container_used_share"] is None
    mp = _fake_files(monkeypatch, {})
    assert set(mp.read_memory().values()) == {None}


def test_health_reports_memory_and_the_page_shows_it():
    main_src = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    assert 'out["memory"] = read_memory()' in main_src
    page = _web("app/health/page.tsx")
    for key in ("container_used_mb", "container_limit_mb", "container_used_share",
                "container_peak_mb", "rss_mb", "rss_peak_mb"):
        assert f"health?.memory?.{key}" in page or f"health.memory.{key}" in page, key


def test_memory_is_logged_periodically_from_startup():
    """Процесс убивает ядро — приложение ничего не запишет. Рост обязан быть в
    логе ДО падения."""
    main_src = (ROOT / "apps/api/main.py").read_text(encoding="utf-8")
    assert "asyncio.create_task(background_memory_log_loop())" in main_src
    assert '"memory_usage"' in main_src


# ── readonly-скан ───────────────────────────────────────────────────────────

@pytest.fixture
def scan(monkeypatch):
    import main

    calls = {"n": 0}

    def fake_compute():
        calls["n"] += 1
        return {"status": "ok", "results": [], "symbols": [], "n": calls["n"]}

    monkeypatch.setattr(main, "_intelligence_scan_compute", fake_compute)
    monkeypatch.setitem(main._SCAN_CACHE, "payload", None)
    monkeypatch.setitem(main._SCAN_CACHE, "at", 0.0)
    monkeypatch.setattr(settings, "INTEL_SCAN_CACHE_SEC", 60.0, raising=False)
    return main, calls


def test_a_fresh_scan_is_served_from_cache(scan):
    main, calls = scan
    first = main.intelligence_scan_readonly()
    second = main.intelligence_scan_readonly()
    assert calls["n"] == 1
    assert first["cache"]["hit"] is False and second["cache"]["hit"] is True
    assert second["n"] == 1


def test_an_expired_cache_rescans(scan):
    main, calls = scan
    main.intelligence_scan_readonly()
    main._SCAN_CACHE["at"] = time.time() - 61
    main.intelligence_scan_readonly()
    assert calls["n"] == 2


def test_an_overlapping_call_does_not_start_a_second_scan(scan):
    main, calls = scan
    main.intelligence_scan_readonly()
    main._SCAN_CACHE["at"] = time.time() - 61              # кеш устарел
    assert main._SCAN_CACHE_LOCK.acquire(blocking=False)   # скан идёт в другом потоке
    try:
        out = main.intelligence_scan_readonly()
    finally:
        main._SCAN_CACHE_LOCK.release()
    assert calls["n"] == 1
    assert out["cache"]["refreshing"] is True


def test_without_any_result_an_overlap_answers_busy(scan):
    main, calls = scan
    assert main._SCAN_CACHE_LOCK.acquire(blocking=False)
    try:
        out = main.intelligence_scan_readonly()
    finally:
        main._SCAN_CACHE_LOCK.release()
    assert out["status"] == "busy" and calls["n"] == 0


def test_a_failed_scan_is_not_cached(scan, monkeypatch):
    main, calls = scan
    monkeypatch.setattr(main, "_intelligence_scan_compute",
                        lambda: {"status": "error", "results": []})
    main.intelligence_scan_readonly()
    assert main._SCAN_CACHE["payload"] is None


def test_the_trading_loop_does_not_use_the_readonly_scan():
    loop_src = (ROOT / "apps/api/workers/robot_loop.py").read_text(encoding="utf-8")
    assert "intelligence_scan_readonly" not in loop_src
    assert "_SCAN_CACHE" not in loop_src


# ── датасет сделок ──────────────────────────────────────────────────────────

def test_outcome_stats_recount_only_when_the_file_changes(tmp_path, monkeypatch):
    import main
    from services import ml_trade_logger

    path = tmp_path / "trade_outcomes.jsonl"
    path.write_text(json.dumps({"labels": {"is_win": True}, "symbol": "BTC/USDT"}) + "\n",
                    encoding="utf-8")
    monkeypatch.setattr(ml_trade_logger.MLTradeLogger, "_resolve_path", lambda self, p: path)
    main._OUTCOME_STATS_CACHE.clear()

    parsed = {"n": 0}
    real_loads = json.loads

    def counting_loads(*a, **kw):
        parsed["n"] += 1
        return real_loads(*a, **kw)

    monkeypatch.setattr(json, "loads", counting_loads)
    assert main.ml_outcomes_stats()["count"] == 1
    assert main.ml_outcomes_stats()["count"] == 1
    assert parsed["n"] == 1, "без изменений файла разбора быть не должно"

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"labels": {"is_loss": True}}) + "\n")
    assert main.ml_outcomes_stats()["count"] == 2


# ── журнал egress ───────────────────────────────────────────────────────────

@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / "egress.jsonl"
    monkeypatch.setattr(settings, "EGRESS_MONITOR_PATH", str(path))
    return path


def test_the_journal_is_read_from_the_end_only_for_the_window(journal):
    from services import egress_monitor as em

    now = time.time()
    # ~400 КБ: чтение обязано пересечь границу куска в 256 КБ.
    rows = [{"ts": now - (6000 - i) * 60, "v": "ok", "pad": "x" * 40} for i in range(6000)]
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    got = em._load(since_ts=now - 24 * 3600)
    assert len(got) == sum(1 for r in rows if r["ts"] >= now - 24 * 3600)
    assert [r["ts"] for r in got] == sorted(r["ts"] for r in got)
    assert len(em._load()) == 6000
    assert [r["ts"] for r in em._load(limit=3)] == [r["ts"] for r in rows[-3:]]


def test_a_journal_without_trailing_newline_keeps_its_first_line(journal):
    from services import egress_monitor as em

    journal.write_text('{"ts": 1, "v": "ok"}\n{"ts": 2, "v": "ok"}', encoding="utf-8")
    assert [r["ts"] for r in em._load()] == [1, 2]


def test_the_journal_is_capped(journal, monkeypatch):
    from services import egress_monitor as em

    monkeypatch.setattr(settings, "EGRESS_MONITOR_MAX_BYTES", 10_000, raising=False)
    journal.write_text("".join(json.dumps({"ts": i, "v": "ok"}) + "\n" for i in range(2000)),
                       encoding="utf-8")
    em._trim(journal)
    assert journal.stat().st_size <= 5_000
    kept = em._load()
    assert kept and kept[-1]["ts"] == 1999
    assert all(isinstance(r["ts"], int) for r in kept), "обрезанная строка попала в журнал"


# ── рынки OKX ───────────────────────────────────────────────────────────────

def test_okx_loads_only_spot_and_swap_markets(monkeypatch):
    import ccxt
    from services import okx_client

    seen = {}

    class FakeOkx:
        def __init__(self, config):
            seen.update(config)
            self.has = {}
            self.markets = None

    monkeypatch.setattr(ccxt, "okx", FakeOkx)
    okx_client.OKXClient()
    assert seen["options"]["fetchMarkets"] == {"types": ["spot", "swap"]}


# ── дашборд ─────────────────────────────────────────────────────────────────

POLLING_PAGES = ("app/funding/page.tsx", "app/grid/page.tsx", "app/health/page.tsx",
                 "app/intelligence/page.tsx", "app/orderbook/page.tsx",
                 "components/ModeBanner.tsx")


def test_every_polling_page_sleeps_in_a_hidden_tab():
    for rel in POLLING_PAGES:
        src = _web(rel)
        assert "useVisiblePolling(" in src, rel
        assert "setInterval(" not in src, f"{rel}: опрос мимо хука"
    hook = _web("lib/useVisiblePolling.ts")
    assert 'document.visibilityState === "visible"' in hook
    assert "visibilitychange" in hook


def test_no_page_polls_outside_the_hook():
    offenders = [
        str(p.relative_to(WEB)) for p in list((WEB / "app").rglob("*.tsx")) + list((WEB / "components").rglob("*.tsx"))
        if "setInterval(" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"опрос мимо useVisiblePolling: {offenders}"


def test_the_proxy_prefers_the_private_network_and_never_repeats_a_sent_command():
    proxy = _web("app/api/proxy/[...path]/route.ts")
    assert "process.env.API_INTERNAL_HOSTPORT" in proxy
    codes = re.search(r"NOT_SENT_CODES = new Set\(\[([^\]]*)\]\)", proxy)
    assert codes and set(re.findall(r'"([A-Z_]+)"', codes.group(1))) == {"ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED"}
    assert "if (!idempotent && !NOT_SENT_CODES.has(code)) throw err;" in proxy
    assert "fetch(`${API_BASE_URL}${pathAndSearch}`, init)" in proxy, "нет запасного публичного адреса"


def test_the_blueprint_gives_the_web_service_the_internal_api_address():
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")
    web = blueprint.split("name: robot-web", 1)[1].split("- type:", 1)[0]
    assert ("- key: API_INTERNAL_HOSTPORT\n        fromService:\n          type: web\n"
            "          name: robot-api\n          property: hostport") in web
    assert "- key: API_BASE_URL" in web


# ── надпись о правиле на TP1 (#tp1-rule-label-2026-09-16) ────────────────────

def test_the_trade_card_reads_the_tp1_rule_from_the_trade_snapshot():
    """С 11.09 фиксации на TP1 нет, а карточка сделки писала «на TP1
    фиксируется 50%» для всех. Надпись обязана идти из снимка конфига сделки,
    а снимок — содержать поля, которые она читает."""
    from services.decision_config import snapshot

    exit_cfg = snapshot(market_type="swap", fee_rate=0.0005, leverage=1)["exit"]
    for key in ("tp1_partial_enabled", "tp1_partial_share", "post_tp1_lock_frac"):
        assert key in exit_cfg, key

    page = _web("app/signals/page.tsx")
    label = page.split("function tp1RuleLabel", 1)[1].split("\nfunction ", 1)[0]
    for key in ("tp1_partial_enabled", "tp1_partial_share", "post_tp1_lock_frac"):
        assert f"exit.{key}" in label, key
    assert "$ = вся позиция · на TP1 фиксируется 50%</span>" not in page
