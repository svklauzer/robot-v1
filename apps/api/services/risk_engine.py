from core.config import settings

class RiskEngine:
    def allow(self, signal: dict, open_positions: list, daily_loss_pct: float, drawdown_pct: float) -> tuple[bool, str]:
        if len(open_positions) >= settings.MAX_OPEN_POSITIONS:
            return False, "max_open_positions"

        if daily_loss_pct >= settings.MAX_DAILY_LOSS_PCT:
            return False, "max_daily_loss"

        if drawdown_pct >= settings.MAX_DRAWDOWN_PCT:
            return False, "max_drawdown"

        return True, "ok"