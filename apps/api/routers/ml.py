from fastapi import APIRouter, Depends
from core.db import SessionLocal
from core.security import require_owner_action
from services.ml_outcome_stats import MLOutcomeStatsService
from services.ml_trade_logger import MLTradeLogger

router = APIRouter(prefix="/ml", tags=["ml"])


@router.get("/status")
def ml_status():
    """Статус ML-контура: режимы, качество моделей, доступность компонентов."""
    from services.ml_intelligence_hub import get_ml_hub
    
    hub = get_ml_hub()
    return hub.health()


@router.get("/shadow-report")
def ml_shadow_report():
    """Отчёт Shadow mode: прогноз vs факт по закрытым сделкам.
    
    Используется для калибровки модели и оценки live AUC.
    """
    from services.ml_outcome_stats import MLOutcomeStatsService
    
    stats_service = MLOutcomeStatsService()
    return stats_service.shadow_report()


@router.get("/evaluate")
def ml_evaluate_candidate(
    confidence: float = 60.0,
    grade: str = "B",
    side: str = "long",
    regime: str = "reversal",
    net_rr_tp1: float = 1.5,
    net_rr_tp2: float = 3.0,
):
    """Тестовая оценка кандидата через ML Intelligence Hub.
    
    Для использования в production вызывайте hub.evaluate_candidate() напрямую из robot_loop.
    """
    from services.ml_intelligence_hub import get_ml_hub
    
    hub = get_ml_hub()
    candidate = {
        "confidence": confidence,
        "grade": grade,
        "side": side,
        "regime": regime,
        "net_rr_tp1": net_rr_tp1,
        "net_rr_tp2": net_rr_tp2,
    }
    decision = hub.evaluate_candidate(candidate)
    return decision.to_dict()


@router.get("/outcomes/summary", dependencies=[Depends(require_owner_action)])
def ml_outcomes_summary():
    return MLOutcomeStatsService().safe_summary()


@router.get("/exit-replay", dependencies=[Depends(require_owner_action)])
def ml_exit_replay(limit: int = 2000, profile: str = "trend"):
    """(#audit-traj) Offline A/B exit-параметров по траекториям закрытых сделок.

    (#backtest-trend-2026-07-27) Профиль по умолчанию — `trend`. Раньше эндпоинт
    умел только scalp/range и молча пропускал трендовый контур, то есть основную
    массу сделок и ровно то место, где сидит потолок прибыли.
    """
    from services.exit_replay import build as build_scalp, build_trend

    if str(profile).lower() == "scalp":
        return build_scalp(limit=limit)
    return build_trend(limit=limit)


@router.get("/walk-forward", dependencies=[Depends(require_owner_action)])
def ml_walk_forward(regime: str = "trend", folds: int = 4, limit: int = 2000):
    """(#walk-forward-2026-07-27) Out-of-sample оценка подбора exit-параметров.

    `/ml/exit-replay` подбирает и оценивает на ОДНОЙ выборке — это in-sample,
    и его лидер систематически красивее правды. Здесь параметры выбираются на
    прошлых фолдах и применяются к следующему без права пересмотра.
    """
    from services.exit_replay import walk_forward

    return walk_forward(regime=regime, folds=folds, limit=limit)


@router.get("/walk-forward/history", dependencies=[Depends(require_owner_action)])
def ml_walk_forward_history(limit: int = 60):
    """История регулярных прогонов: виден дрейф оптимума во времени."""
    from services.walkforward_monitor import history

    return history(limit=limit)


@router.post("/outcomes/backfill", dependencies=[Depends(require_owner_action)])
def ml_outcomes_backfill(limit: int = 500):
    db = SessionLocal()
    try:
        return MLTradeLogger().log_unlogged_closed_signals(db, limit=limit)
    finally:
        db.close()
