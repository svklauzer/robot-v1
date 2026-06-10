from fastapi import APIRouter, Depends
from pydantic import BaseModel
from core.db import SessionLocal
from core.security import require_owner_action
from models.funding_arbitrage import FundingArbOpportunity, FundingArbPosition
from services.funding_arbitrage import FundingMonitorService, FundingArbEngine
from services.audit_log import AuditLogService

router = APIRouter(prefix="/funding-arb", tags=["funding-arb"])


class FundingArbScanRequest(BaseModel):
    symbols: list[str] | None = None


class FundingArbOpenRequest(BaseModel):
    opportunity_id: int
    notional_usdt: float | None = None
    mode: str = "paper"


class FundingArbCloseRequest(BaseModel):
    spot_exit_price: float
    swap_exit_price: float
    funding_periods: int = 1
    exit_funding_rate: float | None = None


class FundingArbPaperSmokeRequest(BaseModel):
    notional_usdt: float | None = None
    funding_periods: int = 1
    persist: bool = False


@router.get("/summary", dependencies=[Depends(require_owner_action)])
def funding_arb_summary():
    db = SessionLocal()
    try:
        return FundingArbEngine().summary(db)
    finally:
        db.close()


@router.post("/scan", dependencies=[Depends(require_owner_action)])
def funding_arb_scan(payload: FundingArbScanRequest | None = None):
    db = SessionLocal()
    try:
        result = FundingMonitorService().scan(db, symbols=payload.symbols if payload else None)
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.get("/opportunities", dependencies=[Depends(require_owner_action)])
def funding_arb_opportunities(limit: int = 50, status: str | None = None):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 200)
        query = db.query(FundingArbOpportunity)
        if status:
            query = query.filter(FundingArbOpportunity.status == status)
        items = query.order_by(FundingArbOpportunity.id.desc()).limit(limit).all()
        monitor = FundingMonitorService()
        return {"items": [monitor.serialize_opportunity(item) for item in items]}
    finally:
        db.close()


@router.get("/positions", dependencies=[Depends(require_owner_action)])
def funding_arb_positions(limit: int = 50, status: str | None = None):
    db = SessionLocal()
    try:
        limit = min(max(limit, 1), 200)
        query = db.query(FundingArbPosition)
        if status:
            query = query.filter(FundingArbPosition.status == status)
        items = query.order_by(FundingArbPosition.id.desc()).limit(limit).all()
        engine = FundingArbEngine()
        return {"items": [engine.serialize_position(item) for item in items]}
    finally:
        db.close()


@router.post("/open", dependencies=[Depends(require_owner_action)])
def funding_arb_open(payload: FundingArbOpenRequest):
    db = SessionLocal()
    try:
        position = FundingArbEngine().open_hedge(
            db,
            opportunity_id=payload.opportunity_id,
            notional_usdt=payload.notional_usdt,
            mode=payload.mode,
        )
        AuditLogService().record(
            db,
            action="funding_arb_opened",
            resource_type="funding_arb_position",
            resource_id=position.id,
            details={"symbol": position.symbol, "notional_usdt": position.notional_usdt, "mode": position.mode},
        )
        db.commit()
        db.refresh(position)
        return {"status": "ok", "position": FundingArbEngine().serialize_position(position)}
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.post("/paper-smoke", dependencies=[Depends(require_owner_action)])
def funding_arb_paper_smoke(payload: FundingArbPaperSmokeRequest | None = None):
    db = SessionLocal()
    request = payload or FundingArbPaperSmokeRequest()
    try:
        result = FundingArbEngine().paper_cycle_smoke(
            db,
            notional_usdt=request.notional_usdt,
            funding_periods=request.funding_periods,
        )
        result["persisted"] = bool(request.persist)
        if request.persist:
            AuditLogService().record(
                db,
                action="funding_arb_paper_smoke",
                resource_type="funding_arb_position",
                resource_id=result.get("position", {}).get("id"),
                details={"realized_pnl": result.get("position", {}).get("realized_pnl")},
            )
            db.commit()
        else:
            db.rollback()
        return result
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.post("/evaluate-exits", dependencies=[Depends(require_owner_action)])
def funding_arb_evaluate_exits():
    db = SessionLocal()
    try:
        result = FundingArbEngine().evaluate_exits(db)
        if result.get("closed") or result.get("close_required"):
            AuditLogService().record(
                db,
                action="funding_arb_exit_evaluated",
                resource_type="funding_arb_position",
                details={
                    "closed": len(result.get("closed", [])),
                    "close_required": len(result.get("close_required", [])),
                    "errors": len(result.get("errors", [])),
                },
            )
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.get("/{position_id}/unrealized", dependencies=[Depends(require_owner_action)])
def funding_arb_unrealized_pnl(position_id: int):
    db = SessionLocal()
    try:
        position = db.query(FundingArbPosition).filter(FundingArbPosition.id == position_id).first()
        if not position:
            return {"status": "error", "error": "position_not_found"}
        result = FundingArbEngine().estimate_unrealized_pnl(position)
        return {"status": "ok", "position_id": position_id, **result}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


@router.post("/{position_id}/close", dependencies=[Depends(require_owner_action)])
def funding_arb_close(position_id: int, payload: FundingArbCloseRequest):
    db = SessionLocal()
    try:
        position = FundingArbEngine().close_paper(
            db,
            position_id=position_id,
            spot_exit_price=payload.spot_exit_price,
            swap_exit_price=payload.swap_exit_price,
            funding_periods=payload.funding_periods,
            exit_funding_rate=payload.exit_funding_rate,
        )
        AuditLogService().record(
            db,
            action="funding_arb_closed",
            resource_type="funding_arb_position",
            resource_id=position.id,
            details={"symbol": position.symbol, "realized_pnl": position.realized_pnl},
        )
        db.commit()
        db.refresh(position)
        return {"status": "ok", "position": FundingArbEngine().serialize_position(position)}
    except Exception as e:
        db.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        db.close()
