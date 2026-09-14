from services.candidate_funnel import KNOWN_BLOCKING_DECISIONS, build_candidate_funnel_diagnosis


def test_candidate_funnel_explains_readonly_scan_and_stopped_bot():
    diagnosis = build_candidate_funnel_diagnosis(
        readonly_scan_hits=20,
        bot_running=False,
        ready_candidates=0,
        published_recent=0,
        telegram_failed_signals=0,
        telegram_failed_deliveries=0,
        latest_event_newer_than_signal=True,
        top_blockers=[],
    )

    assert any("readonly" in reason for reason in diagnosis["reasons"])
    assert any("running" in reason for reason in diagnosis["reasons"])
    assert any("POST /intelligence/scan/run" in action for action in diagnosis["actions"])


def test_candidate_funnel_prioritizes_telegram_and_gate_blockers():
    diagnosis = build_candidate_funnel_diagnosis(
        readonly_scan_hits=0,
        bot_running=True,
        ready_candidates=2,
        published_recent=0,
        telegram_failed_signals=1,
        telegram_failed_deliveries=3,
        latest_event_newer_than_signal=False,
        top_blockers=[{"decision": "a_plus_rr_tp1_too_low", "count": 4}],
    )

    assert any("Telegram failures" in reason for reason in diagnosis["reasons"])
    assert any("a_plus_rr_tp1_too_low" in reason for reason in diagnosis["reasons"])
    assert any("ready_to_publish" in reason for reason in diagnosis["reasons"])


def test_known_blocking_decisions_includes_live_gate_codes():
    """(#audit-2026-08-27) top_blockers на /intelligence/funnel фильтрует
    decision_counts через KNOWN_BLOCKING_DECISIONS — список не обновлялся с
    момента добавления (29.05) и молча прятал новые причины блокировок
    (depth-гейт 12.06, переписанный tp_reachability 24.08, trend_trigger's
    extended_from_ema20, symbol_policy_* гейт), из-за чего дашборд показывал
    древний net_rr_too_low вместо реально доминирующих причин. Регрессия:
    эти коды обязаны быть в списке."""
    for decision in (
        "blocked_depth_gate",
        "tp2_reached_too_rarely",
        "extended_from_ema20",
        "extended_from_ema20_shadow",
        "symbol_policy_confidence_too_low",
        "symbol_policy_rr_tp1_too_low",
        "symbol_policy_rr_tp2_too_low",
        "symbol_policy_publish_blocked",
    ):
        assert decision in KNOWN_BLOCKING_DECISIONS, f"{decision} must be in KNOWN_BLOCKING_DECISIONS"


def test_top_blockers_come_from_the_event_status_not_only_the_list():
    """(#blockers-by-status-2026-09-14) 14.09 воронка назвала главным блокером
    стакан (19 из 120), а лимит кластера (60) и условия ТЗ (29) не показывала:
    их не было в ручном списке. Статус blocked/rejected — сам себе признак."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.db import Base
    from models.bot import Bot
    from models.intelligence_event import IntelligenceEvent
    from models.signal import Signal
    from models.user import User
    from services.candidate_funnel import CandidateFunnelService

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, Bot.__table__, Signal.__table__, IntelligenceEvent.__table__,
    ])
    db = sessionmaker(bind=engine)()
    for decision, status, n in (("cluster_direction_cap", "blocked", 6),
                                ("tz_entry_conditions", "blocked", 3),
                                ("blocked_depth_gate", "blocked", 2),
                                ("net_rr_blended_too_low", "rejected", 1),
                                ("position_opened", "opened", 4)):
        for _ in range(n):
            db.add(IntelligenceEvent(symbol="X/USDT", status=status, decision=decision))
    db.flush()

    out = CandidateFunnelService().summarize(db, limit=50)
    blockers = [b["decision"] for b in out["events"]["top_blockers"]]
    assert blockers[:4] == ["cluster_direction_cap", "tz_entry_conditions",
                            "blocked_depth_gate", "net_rr_blended_too_low"]
    assert "position_opened" not in blockers


def test_every_exposure_guard_block_has_a_label_on_the_intelligence_page():
    """Статусный фильтр пропускает в ленту любой blocked — значит у кодов
    ExposureGuard обязан быть ярлык, иначе в ленте будет машинный код."""
    import re
    from pathlib import Path

    api = Path(__file__).resolve().parents[1]
    page = api.parent / "web" / "app" / "intelligence" / "page.tsx"
    if not page.exists():
        import pytest
        pytest.skip("нет фронтенда")
    guard = (api / "services" / "exposure_guard.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'reason="([a-z_]+)"', guard)) - {"ok"}   # ok — вход разрешён
    ui = page.read_text(encoding="utf-8")
    labels = ui.split("function decisionLabel", 1)[1].split("};", 1)[0]
    missing = sorted(c for c in codes if f"{c}:" not in labels)
    assert not missing, f"код ExposureGuard без ярлыка: {missing}"
    assert "BLOCKING_STATUSES.has(" in ui
