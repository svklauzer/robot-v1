from fastapi import APIRouter, Depends
from core.db import SessionLocal
from core.security import require_owner_action
from services.report_service import ReportService

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/summary", dependencies=[Depends(require_owner_action)])
def report_summary(hours: int = 24):
    db = SessionLocal()
    try:
        service = ReportService()
        return service.collect_stats(db, hours)
    finally:
        db.close()


@router.post("/send-owner", dependencies=[Depends(require_owner_action)])
async def send_owner_report(hours: int = 24):
    db = SessionLocal()
    try:
        service = ReportService()
        stats = await service.send_owner_report(db, hours)
        return {"status": "sent", "stats": stats}
    finally:
        db.close()


@router.post("/send-free", dependencies=[Depends(require_owner_action)])
async def send_free_report(hours: int = 24):
    db = SessionLocal()
    try:
        service = ReportService()
        stats = await service.send_free_report(db, hours)
        return {"status": "sent", "stats": stats}
    finally:
        db.close()


@router.post("/send-vip", dependencies=[Depends(require_owner_action)])
async def send_vip_report(hours: int = 24):
    db = SessionLocal()
    try:
        service = ReportService()
        stats = await service.send_vip_report(db, hours)
        return {"status": "sent", "stats": stats}
    finally:
        db.close()


@router.post("/send-all", dependencies=[Depends(require_owner_action)])
async def send_all_reports(hours: int = 24):
    db = SessionLocal()
    try:
        service = ReportService()
        stats = await service.send_all_reports(db, hours)
        return {"status": "sent", "stats": stats}
    finally:
        db.close()
