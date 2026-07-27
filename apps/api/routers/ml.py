from fastapi import APIRouter, Depends
from core.db import SessionLocal
from core.security import require_owner_action
from services.ml_outcome_stats import MLOutcomeStatsService
from services.ml_trade_logger import MLTradeLogger

router = APIRouter(prefix="/ml", tags=["ml"])


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


@router.post("/outcomes/backfill", dependencies=[Depends(require_owner_action)])
def ml_outcomes_backfill(limit: int = 500):
    db = SessionLocal()
    try:
        return MLTradeLogger().log_unlogged_closed_signals(db, limit=limit)
    finally:
        db.close()
