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
def ml_exit_replay(limit: int = 2000):
    """(#audit-traj) Offline A/B exit-параметров по траекториям закрытых сделок."""
    from services.exit_replay import build as build_exit_replay
    return build_exit_replay(limit=limit)


@router.post("/outcomes/backfill", dependencies=[Depends(require_owner_action)])
def ml_outcomes_backfill(limit: int = 500):
    db = SessionLocal()
    try:
        return MLTradeLogger().log_unlogged_closed_signals(db, limit=limit)
    finally:
        db.close()
