from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from core.config import settings
from models.funding_arbitrage import FundingArbOpportunity, FundingArbPosition
from services.htx_client import HTXClient


@dataclass
class FundingSnapshot:
    symbol: str
    spot_symbol: str
    swap_symbol: str
    funding_rate: float
    annualized_rate_pct: float
    spot_price: float
    swap_price: float
    basis_pct: float
    estimated_edge_pct: float
    next_funding_at: datetime | None
    status: str
    raw: dict[str, Any]


class FundingSymbolMapper:
    @staticmethod
    def spot_symbol(symbol: str) -> str:
        return symbol.split(":", 1)[0]

    @staticmethod
    def swap_symbol(symbol: str) -> str:
        spot = FundingSymbolMapper.spot_symbol(symbol)
        if ":" in symbol:
            return symbol
        base, quote = spot.split("/", 1)
        return f"{base}/{quote}:{quote}"


class FundingMonitorService:
    """HTX-only funding-rate arbitrage monitor.

    Positive USDT swap funding means long perps pay shorts, therefore the
    conservative hedge is spot long + perpetual short inside the same HTX
    account/client. This is not latency arbitrage; it is an 8h funding carry.
    """

    def __init__(self, client: HTXClient | None = None):
        self.client = client or HTXClient()

    def _parse_next_funding_at(self, payload: dict[str, Any]) -> datetime | None:
        value = payload.get("nextFundingTimestamp") or payload.get("fundingTimestamp") or payload.get("timestamp")
        if value is None:
            return None
        try:
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric = numeric / 1000
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except Exception:
            return None

    def snapshot(self, symbol: str) -> FundingSnapshot:
        spot_symbol = FundingSymbolMapper.spot_symbol(symbol)
        swap_symbol = FundingSymbolMapper.swap_symbol(symbol)

        funding = self.client.fetch_funding_rate(swap_symbol) or {}
        spot_price = float(self.client.fetch_mark_price(spot_symbol))
        swap_price = float(self.client.fetch_mark_price(swap_symbol))
        funding_rate = float(funding.get("fundingRate") or funding.get("rate") or 0.0)
        funding_pct = funding_rate * 100
        annualized = funding_rate * 3 * 365 * 100
        basis_pct = ((swap_price - spot_price) / spot_price) * 100 if spot_price else 0.0
        estimated_edge_pct = funding_pct - abs(basis_pct)

        status = "candidate"
        if not settings.ENABLE_FUNDING_ARB:
            status = "disabled"
        elif funding_pct < settings.FUNDING_ARB_MIN_RATE_PCT:
            status = "below_funding_threshold"
        elif abs(basis_pct) > settings.FUNDING_ARB_MAX_BASIS_PCT:
            status = "basis_too_wide"
        elif estimated_edge_pct < settings.FUNDING_ARB_MIN_EDGE_PCT:
            status = "edge_too_low"

        return FundingSnapshot(
            symbol=spot_symbol,
            spot_symbol=spot_symbol,
            swap_symbol=swap_symbol,
            funding_rate=funding_rate,
            annualized_rate_pct=annualized,
            spot_price=spot_price,
            swap_price=swap_price,
            basis_pct=basis_pct,
            estimated_edge_pct=estimated_edge_pct,
            next_funding_at=self._parse_next_funding_at(funding),
            status=status,
            raw={"funding": funding},
        )

    def scan_interval_seconds(self) -> int:
        return max(int(getattr(settings, "FUNDING_ARB_SCAN_INTERVAL_HOURS", 8)), 1) * 60 * 60

    def scan(self, db: Session, symbols: list[str] | None = None) -> dict:
        items: list[FundingArbOpportunity] = []
        errors: list[dict] = []
        for symbol in symbols or settings.funding_arb_symbols:
            try:
                snapshot = self.snapshot(symbol)
                item = FundingArbOpportunity(
                    symbol=snapshot.symbol,
                    spot_symbol=snapshot.spot_symbol,
                    swap_symbol=snapshot.swap_symbol,
                    funding_rate=snapshot.funding_rate,
                    annualized_rate_pct=snapshot.annualized_rate_pct,
                    spot_price=snapshot.spot_price,
                    swap_price=snapshot.swap_price,
                    basis_pct=snapshot.basis_pct,
                    estimated_edge_pct=snapshot.estimated_edge_pct,
                    status=snapshot.status,
                    next_funding_at=snapshot.next_funding_at,
                    raw_json=snapshot.raw,
                )
                db.add(item)
                db.flush()
                items.append(item)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
        return {"items": [self.serialize_opportunity(item) for item in items], "errors": errors}

    def serialize_opportunity(self, item: FundingArbOpportunity) -> dict:
        return {
            "id": item.id,
            "symbol": item.symbol,
            "spot_symbol": item.spot_symbol,
            "swap_symbol": item.swap_symbol,
            "funding_rate": item.funding_rate,
            "funding_rate_pct": round(item.funding_rate * 100, 6),
            "annualized_rate_pct": round(item.annualized_rate_pct, 4),
            "spot_price": item.spot_price,
            "swap_price": item.swap_price,
            "basis_pct": round(item.basis_pct, 6),
            "estimated_edge_pct": round(item.estimated_edge_pct, 6),
            "status": item.status,
            "next_funding_at": item.next_funding_at.isoformat() if item.next_funding_at else None,
            "created_at": item.created_at.isoformat() if hasattr(item.created_at, "isoformat") else item.created_at,
        }


class HedgeBuilder:
    def build(self, opportunity: FundingArbOpportunity, notional_usdt: float | None = None) -> dict:
        notional = min(float(notional_usdt or settings.FUNDING_ARB_DEFAULT_NOTIONAL_USDT), settings.FUNDING_ARB_MAX_NOTIONAL_USDT)
        spot_qty = notional / float(opportunity.spot_price)
        swap_qty = notional / float(opportunity.swap_price)
        open_close_fees = notional * (settings.SPOT_TAKER_FEE + settings.FUTURES_TAKER_FEE) * 2
        expected_funding = notional * float(opportunity.funding_rate)
        break_even_periods = (open_close_fees / expected_funding) if expected_funding > 0 else None
        return {
            "hedge_side": "spot_long_perp_short",
            "notional_usdt": round(notional, 6),
            "spot_qty": round(spot_qty, 10),
            "swap_qty": round(swap_qty, 10),
            "expected_funding_per_period": round(expected_funding, 6),
            "estimated_round_trip_fees": round(open_close_fees, 6),
            "break_even_periods": round(break_even_periods, 4) if break_even_periods is not None else None,
        }


class ArbExitEngine:
    def should_close(self, position: FundingArbPosition, current_funding_rate: float | None = None, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        opened_at = position.opened_at
        if opened_at is not None and opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        held_hours = ((now - opened_at).total_seconds() / 3600) if opened_at else 0
        if current_funding_rate is not None and current_funding_rate * 100 <= settings.FUNDING_ARB_CLOSE_RATE_PCT:
            return {"close": True, "reason": "funding_rate_compressed", "held_hours": round(held_hours, 2)}
        if held_hours >= settings.FUNDING_ARB_MAX_HOLD_HOURS:
            return {"close": True, "reason": "max_hold_hours", "held_hours": round(held_hours, 2)}
        return {"close": False, "reason": "hold", "held_hours": round(held_hours, 2)}


class FundingArbEngine:
    def __init__(self, client: HTXClient | None = None):
        self.hedge_builder = HedgeBuilder()
        self.exit_engine = ArbExitEngine()
        self.client = client or HTXClient()

    def open_hedge(
        self,
        db: Session,
        opportunity_id: int,
        notional_usdt: float | None = None,
        mode: str = "paper",
    ) -> FundingArbPosition:
        opportunity = db.query(FundingArbOpportunity).filter(FundingArbOpportunity.id == opportunity_id).first()
        if not opportunity:
            raise ValueError("funding_arb_opportunity_not_found")
        if opportunity.status != "candidate":
            raise ValueError(f"funding_arb_opportunity_not_candidate:{opportunity.status}")
        hedge = self.hedge_builder.build(opportunity, notional_usdt=notional_usdt)
        raw_orders: dict[str, Any] = {}
        if mode == "live":
            if not settings.ENABLE_FUNDING_ARB or not settings.ENABLE_FUTURES or not settings.ENABLE_LIVE_ORDERS:
                raise RuntimeError("live funding arbitrage requires ENABLE_FUNDING_ARB, ENABLE_FUTURES and ENABLE_LIVE_ORDERS")
            raw_orders["spot_open"] = self.client.create_market_order(opportunity.spot_symbol, "buy", hedge["spot_qty"])
            raw_orders["swap_open"] = self.client.create_market_order(
                opportunity.swap_symbol,
                "sell",
                hedge["swap_qty"],
                params={"hedge": "funding_arb", "reduceOnly": False},
            )
        elif mode != "paper":
            raise ValueError("funding_arb_mode_must_be_paper_or_live")

        position = FundingArbPosition(
            opportunity_id=opportunity.id,
            symbol=opportunity.symbol,
            spot_symbol=opportunity.spot_symbol,
            swap_symbol=opportunity.swap_symbol,
            mode=mode,
            status="open",
            hedge_side=hedge["hedge_side"],
            notional_usdt=hedge["notional_usdt"],
            spot_qty=hedge["spot_qty"],
            swap_qty=hedge["swap_qty"],
            spot_entry_price=opportunity.spot_price,
            swap_entry_price=opportunity.swap_price,
            entry_funding_rate=opportunity.funding_rate,
            fees_paid=hedge["estimated_round_trip_fees"],
            raw_json={"hedge": hedge, "orders": raw_orders},
        )
        db.add(position)
        db.flush()
        return position

    def open_paper(self, db: Session, opportunity_id: int, notional_usdt: float | None = None) -> FundingArbPosition:
        return self.open_hedge(db, opportunity_id=opportunity_id, notional_usdt=notional_usdt, mode="paper")

    def close_paper(
        self,
        db: Session,
        position_id: int,
        spot_exit_price: float,
        swap_exit_price: float,
        funding_periods: int = 1,
        exit_funding_rate: float | None = None,
    ) -> FundingArbPosition:
        position = db.query(FundingArbPosition).filter(FundingArbPosition.id == position_id).first()
        if not position:
            raise ValueError("funding_arb_position_not_found")
        if position.status != "open":
            raise ValueError("funding_arb_position_not_open")
        spot_pnl = float(position.spot_qty) * (float(spot_exit_price) - float(position.spot_entry_price))
        swap_pnl = float(position.swap_qty) * (float(position.swap_entry_price) - float(swap_exit_price))
        funding_collected = float(position.notional_usdt) * float(position.entry_funding_rate) * int(funding_periods)
        realized = spot_pnl + swap_pnl + funding_collected - float(position.fees_paid or 0.0)
        position.status = "closed"
        position.spot_exit_price = spot_exit_price
        position.swap_exit_price = swap_exit_price
        position.exit_funding_rate = exit_funding_rate
        position.funding_periods = int(funding_periods)
        position.funding_collected = round(funding_collected, 6)
        position.realized_pnl = round(realized, 6)
        position.closed_at = datetime.now(timezone.utc)
        return position


    def _estimate_funding_periods(self, position: FundingArbPosition, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        opened_at = position.opened_at
        if opened_at is not None and opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        if not opened_at:
            return 1
        held_hours = max((now - opened_at).total_seconds() / 3600, 0)
        return max(int(held_hours // 8), 1)

    def evaluate_exits(self, db: Session) -> dict:
        """Evaluate open funding hedges and auto-close paper positions.

        Live positions are deliberately not closed by this helper yet: the method
        returns them as ``close_required`` so the owner can review/reconcile before
        sending reduce-only orders. Paper positions can be closed deterministically
        with current spot/swap marks to keep the P&L log complete.
        """
        positions = db.query(FundingArbPosition).filter(FundingArbPosition.status == "open").all()
        closed: list[dict[str, Any]] = []
        close_required: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for position in positions:
            try:
                funding = self.client.fetch_funding_rate(position.swap_symbol) or {}
                current_rate = float(funding.get("fundingRate") or funding.get("rate") or 0.0)
                decision = self.exit_engine.should_close(position, current_funding_rate=current_rate)
                decision["position_id"] = position.id
                decision["symbol"] = position.symbol
                decision["current_funding_rate_pct"] = round(current_rate * 100, 6)

                if not decision.get("close"):
                    held.append(decision)
                    continue

                if position.mode == "paper":
                    spot_exit = float(self.client.fetch_mark_price(position.spot_symbol))
                    swap_exit = float(self.client.fetch_mark_price(position.swap_symbol))
                    closed_position = self.close_paper(
                        db,
                        position.id,
                        spot_exit_price=spot_exit,
                        swap_exit_price=swap_exit,
                        funding_periods=self._estimate_funding_periods(position),
                        exit_funding_rate=current_rate,
                    )
                    closed.append({
                        "decision": decision,
                        "position": self.serialize_position(closed_position),
                    })
                else:
                    close_required.append(decision)
            except Exception as exc:
                errors.append({
                    "position_id": position.id,
                    "symbol": position.symbol,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        return {
            "evaluated": len(positions),
            "closed": closed,
            "close_required": close_required,
            "held": held,
            "errors": errors,
        }

    def serialize_position(self, item: FundingArbPosition) -> dict:
        return {
            "id": item.id,
            "opportunity_id": item.opportunity_id,
            "symbol": item.symbol,
            "spot_symbol": item.spot_symbol,
            "swap_symbol": item.swap_symbol,
            "mode": item.mode,
            "status": item.status,
            "hedge_side": item.hedge_side,
            "notional_usdt": item.notional_usdt,
            "spot_qty": item.spot_qty,
            "swap_qty": item.swap_qty,
            "spot_entry_price": item.spot_entry_price,
            "swap_entry_price": item.swap_entry_price,
            "spot_exit_price": item.spot_exit_price,
            "swap_exit_price": item.swap_exit_price,
            "entry_funding_rate": item.entry_funding_rate,
            "entry_funding_rate_pct": round(item.entry_funding_rate * 100, 6),
            "funding_periods": item.funding_periods,
            "funding_collected": item.funding_collected,
            "fees_paid": item.fees_paid,
            "realized_pnl": item.realized_pnl,
            "opened_at": item.opened_at.isoformat() if hasattr(item.opened_at, "isoformat") else item.opened_at,
            "closed_at": item.closed_at.isoformat() if item.closed_at and hasattr(item.closed_at, "isoformat") else item.closed_at,
        }


    def paper_cycle_smoke(
        self,
        db: Session,
        notional_usdt: float | None = None,
        funding_periods: int = 1,
    ) -> dict:
        """Run a deterministic paper funding-arb cycle in the DB session.

        The caller controls commit/rollback.  This is intended for owner/runbook
        smoke checks: create a candidate, open a paper hedge, simulate one or
        more 8h funding windows, close the hedge when funding compresses, and
        return the P&L log that would be visible in the owner UI.
        """
        notional = float(notional_usdt or settings.FUNDING_ARB_MAX_NOTIONAL_USDT or 100.0)
        funding_rate = max(float(settings.FUNDING_ARB_MIN_RATE_PCT) / 100, 0.001)
        spot_price = 100.0
        swap_price = 100.05
        basis_pct = ((swap_price - spot_price) / spot_price) * 100
        now = datetime.now(timezone.utc)

        opportunity = FundingArbOpportunity(
            symbol="BTC/USDT",
            spot_symbol="BTC/USDT",
            swap_symbol="BTC/USDT:USDT",
            funding_rate=funding_rate,
            annualized_rate_pct=funding_rate * 3 * 365 * 100,
            spot_price=spot_price,
            swap_price=swap_price,
            basis_pct=basis_pct,
            estimated_edge_pct=round(funding_rate * 100 - abs(basis_pct), 6),
            status="candidate",
            next_funding_at=now + timedelta(hours=8),
            raw_json={"smoke": True, "source": "paper_cycle_smoke"},
        )
        db.add(opportunity)
        db.flush()

        position = self.open_paper(db, opportunity.id, notional_usdt=notional)
        position.opened_at = now - timedelta(hours=8 * max(int(funding_periods), 1))
        db.flush()

        closed = self.close_paper(
            db,
            position_id=position.id,
            spot_exit_price=spot_price * 1.001,
            swap_exit_price=swap_price * 0.999,
            funding_periods=max(int(funding_periods), 1),
            exit_funding_rate=float(settings.FUNDING_ARB_CLOSE_RATE_PCT) / 100,
        )
        return {
            "status": "ok",
            "smoke": "funding_arb_paper_cycle",
            "opportunity": FundingMonitorService().serialize_opportunity(opportunity),
            "position": self.serialize_position(closed),
            "checks": {
                "scan_candidate_created": opportunity.status == "candidate",
                "paper_hedge_opened": position.mode == "paper",
                "funding_periods_logged": int(closed.funding_periods or 0) >= 1,
                "pnl_logged": closed.realized_pnl is not None,
                "closed_on_compression": closed.status == "closed",
            },
        }

    def summary(self, db: Session) -> dict:
        open_count = db.query(FundingArbPosition).filter(FundingArbPosition.status == "open").count()
        closed = db.query(FundingArbPosition).filter(FundingArbPosition.status == "closed").all()
        realized = sum(float(item.realized_pnl or 0.0) for item in closed)
        latest = db.query(FundingArbOpportunity).order_by(FundingArbOpportunity.id.desc()).limit(10).all()
        monitor = FundingMonitorService()
        return {
            "enabled": settings.ENABLE_FUNDING_ARB,
            "symbols": settings.funding_arb_symbols,
            "open_positions": open_count,
            "closed_positions": len(closed),
            "realized_pnl": round(realized, 6),
            "latest_opportunities": [monitor.serialize_opportunity(item) for item in latest],
        }
