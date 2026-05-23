from sqlalchemy import func
from models.signal import Signal

class AnalyticsService:
    def summary(self, db):
        total = db.query(func.count(Signal.id)).scalar() or 0
        wins = db.query(func.count(Signal.id)).filter(Signal.result_pct != None, Signal.result_pct > 0).scalar() or 0
        pnl = db.query(func.coalesce(func.sum(Signal.result_pct), 0)).scalar() or 0.0

        return {
            "total_signals": total,
            "wins": wins,
            "winrate": round((wins / total) * 100, 2) if total else 0.0,
            "pnl_pct": round(float(pnl), 2),
        }