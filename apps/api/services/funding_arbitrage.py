"""
HTX Funding Rate Arbitrage Service.

Strategy: spot long + USDT perpetual short inside the same HTX account.
Income source: 8h funding payments (when funding rate > 0, longs pay shorts).
Risk: basis change (perp price drift vs spot), exchange risk, liquidity.

Economics per position:
  Gross income (per period)  = notional × funding_rate
  Round-trip fees (one-time) = notional × (spot_taker + futures_taker) × 2
  Break-even periods         = round_trip_fees / income_per_period
  Net yield (N periods)      = gross_income × N - round_trip_fees

Entry filter:
  - funding_rate > FUNDING_ARB_MIN_RATE_PCT  (positive income per period)
  - abs(basis) < FUNDING_ARB_MAX_BASIS_PCT   (perp price not too far from spot)
  - net_yield_per_period > FUNDING_ARB_MIN_NET_YIELD_PCT  (after fee amortization)

Exit triggers:
  - funding_rate compresses below FUNDING_ARB_CLOSE_RATE_PCT
  - held > FUNDING_ARB_MAX_HOLD_HOURS
"""
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
    funding_rate_pct: float
    annualized_rate_pct: float
    spot_price: float
    swap_price: float
    basis_pct: float          # (swap - spot) / spot × 100; positive = perp premium (good for us)
    fee_round_trip_pct: float  # total round-trip fees as % of notional
    # Per-period net yield = funding_pct - fee_amortized (assuming ASSUMED_HOLD_PERIODS)
    net_yield_per_period_pct: float
    # Periods needed to break even on fees
    break_even_periods: float | None
    # Annualized net return assuming ASSUMED_HOLD_PERIODS hold
    annualized_net_yield_pct: float
    estimated_edge_pct: float  # legacy field; same as net_yield_per_period_pct
    next_funding_at: datetime | None
    status: str
    reject_reason: str | None
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
    """HTX-only funding-rate arbitrage monitor."""

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

    def _compute_fee_pct(self) -> float:
        """Round-trip fee as % of notional: (spot_taker + futures_taker) × 2 × 100."""
        return (float(settings.SPOT_TAKER_FEE) + float(settings.FUTURES_TAKER_FEE)) * 2 * 100

    def snapshot(self, symbol: str) -> FundingSnapshot:
        spot_symbol = FundingSymbolMapper.spot_symbol(symbol)
        swap_symbol = FundingSymbolMapper.swap_symbol(symbol)

        funding = self.client.fetch_funding_rate(swap_symbol) or {}
        spot_price = float(self.client.fetch_mark_price(spot_symbol))
        swap_price = float(self.client.fetch_mark_price(swap_symbol))

        funding_rate = float(funding.get("fundingRate") or funding.get("rate") or 0.0)
        funding_rate_pct = funding_rate * 100
        annualized = funding_rate * 3 * 365 * 100   # 3 periods/day × 365 days

        # Basis: positive means swap > spot (perp premium — favorable for our hedge)
        basis_pct = ((swap_price - spot_price) / spot_price) * 100 if spot_price else 0.0

        # Fees
        fee_round_trip_pct = self._compute_fee_pct()

        # Net yield per period, amortizing fees over the assumed hold duration.
        assumed_hold = int(getattr(settings, "FUNDING_ARB_ASSUMED_HOLD_PERIODS", 10))
        fee_per_period_pct = fee_round_trip_pct / max(assumed_hold, 1)
        # Basis contributes positively if perp > spot (will converge upward when we close)
        # We conservatively weight it at 30% since convergence is not guaranteed.
        basis_contribution = basis_pct * 0.30 if basis_pct > 0 else basis_pct * 0.50

        net_yield_per_period_pct = funding_rate_pct + basis_contribution - fee_per_period_pct

        # Break-even: how many 8h periods to recover fees
        if funding_rate_pct > 0:
            break_even_periods = round(fee_round_trip_pct / funding_rate_pct, 1)
        else:
            break_even_periods = None

        # Annualized return for the assumed hold period
        hold_hours = assumed_hold * 8
        hold_years = hold_hours / (365 * 24)
        if hold_years > 0:
            gross_hold = funding_rate_pct * assumed_hold
            net_hold = gross_hold + basis_pct * 0.30 - fee_round_trip_pct
            annualized_net_yield_pct = round(net_hold / hold_years / 100, 2) * 100
        else:
            annualized_net_yield_pct = 0.0

        # Status determination
        min_rate = float(getattr(settings, "FUNDING_ARB_MIN_RATE_PCT", 0.015))
        max_basis = float(getattr(settings, "FUNDING_ARB_MAX_BASIS_PCT", 0.50))
        min_net_yield = float(getattr(settings, "FUNDING_ARB_MIN_NET_YIELD_PCT", 0.005))

        reject_reason = None
        status = "candidate"

        if not settings.ENABLE_FUNDING_ARB:
            status = "disabled"
            reject_reason = "funding_arb_disabled"
        elif funding_rate_pct <= 0:
            status = "negative_funding"
            reject_reason = f"funding_rate={funding_rate_pct:.4f}% (≤0, longs would pay you)"
        elif funding_rate_pct < min_rate:
            status = "below_funding_threshold"
            reject_reason = f"funding_rate={funding_rate_pct:.4f}% < min={min_rate}%"
        elif abs(basis_pct) > max_basis:
            status = "basis_too_wide"
            reject_reason = f"abs(basis)={abs(basis_pct):.4f}% > max={max_basis}%"
        elif net_yield_per_period_pct < min_net_yield:
            status = "edge_too_low"
            reject_reason = (
                f"net_yield={net_yield_per_period_pct:.4f}%/period < min={min_net_yield}% "
                f"(fees eat funding at {break_even_periods} periods break-even)"
            )

        return FundingSnapshot(
            symbol=spot_symbol,
            spot_symbol=spot_symbol,
            swap_symbol=swap_symbol,
            funding_rate=funding_rate,
            funding_rate_pct=funding_rate_pct,
            annualized_rate_pct=annualized,
            spot_price=spot_price,
            swap_price=swap_price,
            basis_pct=basis_pct,
            fee_round_trip_pct=fee_round_trip_pct,
            net_yield_per_period_pct=net_yield_per_period_pct,
            break_even_periods=break_even_periods,
            annualized_net_yield_pct=annualized_net_yield_pct,
            estimated_edge_pct=net_yield_per_period_pct,  # legacy alias
            next_funding_at=self._parse_next_funding_at(funding),
            status=status,
            reject_reason=reject_reason,
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
                    raw_json={
                        **snapshot.raw,
                        "funding_rate_pct": snapshot.funding_rate_pct,
                        "fee_round_trip_pct": snapshot.fee_round_trip_pct,
                        "net_yield_per_period_pct": snapshot.net_yield_per_period_pct,
                        "break_even_periods": snapshot.break_even_periods,
                        "annualized_net_yield_pct": snapshot.annualized_net_yield_pct,
                        "reject_reason": snapshot.reject_reason,
                    },
                )
                db.add(item)
                db.flush()
                items.append(item)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
        return {"items": [self.serialize_opportunity(item) for item in items], "errors": errors}

    def serialize_opportunity(self, item: FundingArbOpportunity) -> dict:
        raw = item.raw_json or {}
        return {
            "id": item.id,
            "symbol": item.symbol,
            "spot_symbol": item.spot_symbol,
            "swap_symbol": item.swap_symbol,
            "funding_rate": item.funding_rate,
            "funding_rate_pct": round(raw.get("funding_rate_pct", item.funding_rate * 100), 4),
            "annualized_rate_pct": round(item.annualized_rate_pct, 4),
            "spot_price": item.spot_price,
            "swap_price": item.swap_price,
            "basis_pct": round(item.basis_pct, 4),
            "fee_round_trip_pct": round(raw.get("fee_round_trip_pct", 0.5), 4),
            "net_yield_per_period_pct": round(raw.get("net_yield_per_period_pct", item.estimated_edge_pct), 4),
            "break_even_periods": raw.get("break_even_periods"),
            "annualized_net_yield_pct": round(raw.get("annualized_net_yield_pct", 0.0), 2),
            "estimated_edge_pct": round(item.estimated_edge_pct, 4),
            "status": item.status,
            "reject_reason": raw.get("reject_reason"),
            "next_funding_at": item.next_funding_at.isoformat() if item.next_funding_at else None,
            "created_at": item.created_at.isoformat() if hasattr(item.created_at, "isoformat") else item.created_at,
        }


class HedgeBuilder:
    def build(self, opportunity: FundingArbOpportunity, notional_usdt: float | None = None) -> dict:
        notional = min(
            float(notional_usdt or settings.FUNDING_ARB_DEFAULT_NOTIONAL_USDT),
            settings.FUNDING_ARB_MAX_NOTIONAL_USDT,
        )
        spot_qty = notional / float(opportunity.spot_price)
        swap_qty = notional / float(opportunity.swap_price)
        fee_round_trip = notional * (settings.SPOT_TAKER_FEE + settings.FUTURES_TAKER_FEE) * 2
        income_per_period = notional * float(opportunity.funding_rate)
        break_even = round(fee_round_trip / income_per_period, 1) if income_per_period > 0 else None

        assumed_hold = int(getattr(settings, "FUNDING_ARB_ASSUMED_HOLD_PERIODS", 10))
        expected_gross = income_per_period * assumed_hold
        expected_net = expected_gross - fee_round_trip

        return {
            "hedge_side": "spot_long_perp_short",
            "notional_usdt": round(notional, 6),
            "spot_qty": round(spot_qty, 10),
            "swap_qty": round(swap_qty, 10),
            "expected_funding_per_period": round(income_per_period, 6),
            "estimated_round_trip_fees": round(fee_round_trip, 6),
            "break_even_periods": break_even,
            "expected_net_pnl_at_assumed_hold": round(expected_net, 6),
        }


class ArbExitEngine:
    def should_close(
        self,
        position: FundingArbPosition,
        current_funding_rate: float | None = None,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        opened_at = position.opened_at
        if opened_at is not None and opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        held_hours = ((now - opened_at).total_seconds() / 3600) if opened_at else 0
        held_periods = held_hours / 8

        min_hold = int(getattr(settings, "FUNDING_ARB_MIN_HOLD_PERIODS", 3))

        # Don't close before minimum hold periods regardless of other conditions
        if held_periods < min_hold:
            return {
                "close": False,
                "reason": "min_hold_not_reached",
                "held_hours": round(held_hours, 2),
                "held_periods": round(held_periods, 1),
                "min_hold_periods": min_hold,
            }

        if current_funding_rate is not None and current_funding_rate * 100 <= settings.FUNDING_ARB_CLOSE_RATE_PCT:
            return {
                "close": True,
                "reason": "funding_rate_compressed",
                "held_hours": round(held_hours, 2),
                "held_periods": round(held_periods, 1),
                "current_funding_rate_pct": round(current_funding_rate * 100, 4),
            }
        if held_hours >= settings.FUNDING_ARB_MAX_HOLD_HOURS:
            return {
                "close": True,
                "reason": "max_hold_hours",
                "held_hours": round(held_hours, 2),
                "held_periods": round(held_periods, 1),
            }
        return {
            "close": False,
            "reason": "hold",
            "held_hours": round(held_hours, 2),
            "held_periods": round(held_periods, 1),
        }


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
                raise RuntimeError(
                    "live funding arbitrage requires ENABLE_FUNDING_ARB, ENABLE_FUTURES and ENABLE_LIVE_ORDERS"
                )
            raw_orders["spot_open"] = self.client.create_market_order(
                opportunity.spot_symbol, "buy", hedge["spot_qty"]
            )
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

    def open_paper(
        self, db: Session, opportunity_id: int, notional_usdt: float | None = None
    ) -> FundingArbPosition:
        return self.open_hedge(db, opportunity_id=opportunity_id, notional_usdt=notional_usdt, mode="paper")

    def auto_open_candidates(self, db: Session) -> dict:
        """Automatically open paper positions for qualifying candidates.

        Called from the background funding arb loop when ENABLE_FUNDING_ARB=True
        and FUNDING_ARB_AUTO_OPEN_PAPER=True.

        Rules:
        - Only opens if fewer than FUNDING_ARB_MAX_OPEN_HEDGES positions are currently open
        - Only opens for candidates (status == "candidate")
        - Uses FUNDING_ARB_DEFAULT_NOTIONAL_USDT
        - Does not open a second position for the same symbol
        """
        if not getattr(settings, "FUNDING_ARB_AUTO_OPEN_PAPER", True):
            return {"auto_open": False, "reason": "auto_open_paper_disabled"}

        max_hedges = int(getattr(settings, "FUNDING_ARB_MAX_OPEN_HEDGES", 2))
        open_count = db.query(FundingArbPosition).filter(FundingArbPosition.status == "open").count()

        if open_count >= max_hedges:
            return {
                "auto_open": False,
                "reason": f"max_open_hedges_reached ({open_count}/{max_hedges})",
            }

        # Symbols with already-open positions
        open_symbols = {
            row.symbol
            for row in db.query(FundingArbPosition).filter(FundingArbPosition.status == "open").all()
        }

        # Latest candidate opportunities (most recent scan)
        candidates = (
            db.query(FundingArbOpportunity)
            .filter(FundingArbOpportunity.status == "candidate")
            .order_by(FundingArbOpportunity.id.desc())
            .limit(20)
            .all()
        )

        # Deduplicate by symbol — keep newest
        seen: set[str] = set()
        unique_candidates = []
        for c in candidates:
            if c.symbol not in seen and c.symbol not in open_symbols:
                seen.add(c.symbol)
                unique_candidates.append(c)

        # Распределяем капитал в ПЕРВУЮ очередь в самые доходные возможности:
        # ранжируем по чистому выходу за период (после амортизации комиссий),
        # а не по «самый свежий». При лимите max_hedges это прямо повышает доход.
        def _net_yield(c) -> float:
            try:
                return float((c.raw_json or {}).get("net_yield_per_period_pct",
                                                     c.estimated_edge_pct or 0.0))
            except (TypeError, ValueError):
                return 0.0
        unique_candidates.sort(key=_net_yield, reverse=True)

        opened = []
        errors = []
        for opp in unique_candidates:
            if open_count >= max_hedges:
                break
            try:
                pos = self.open_paper(db, opp.id)
                db.flush()
                opened.append({
                    "position_id": pos.id,
                    "opportunity_id": opp.id,
                    "symbol": opp.symbol,
                    "notional_usdt": float(pos.notional_usdt),
                    "entry_funding_rate_pct": round(float(opp.funding_rate) * 100, 4),
                })
                open_count += 1
            except Exception as exc:
                errors.append({"opportunity_id": opp.id, "symbol": opp.symbol, "error": str(exc)})

        return {
            "auto_open": True,
            "candidates_found": len(unique_candidates),
            "opened": opened,
            "errors": errors,
        }

    def estimate_unrealized_pnl(self, position: FundingArbPosition, now: datetime | None = None) -> dict:
        """Estimate current unrealized P&L for an open position."""
        try:
            now = now or datetime.now(timezone.utc)
            spot_current = float(self.client.fetch_mark_price(position.spot_symbol))
            swap_current = float(self.client.fetch_mark_price(position.swap_symbol))

            spot_pnl = float(position.spot_qty) * (spot_current - float(position.spot_entry_price))
            swap_pnl = float(position.swap_qty) * (float(position.swap_entry_price) - swap_current)

            opened_at = position.opened_at
            if opened_at is not None and opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            held_hours = ((now - opened_at).total_seconds() / 3600) if opened_at else 0
            est_periods = max(held_hours / 8, 0)
            est_funding = float(position.notional_usdt) * float(position.entry_funding_rate) * est_periods

            unrealized = spot_pnl + swap_pnl + est_funding - float(position.fees_paid or 0)

            return {
                "ok": True,
                "spot_current": spot_current,
                "swap_current": swap_current,
                "spot_pnl": round(spot_pnl, 6),
                "swap_pnl": round(swap_pnl, 6),
                "estimated_funding_collected": round(est_funding, 6),
                "estimated_periods": round(est_periods, 1),
                "held_hours": round(held_hours, 2),
                "unrealized_pnl": round(unrealized, 6),
                "fees_paid": float(position.fees_paid or 0),
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

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
        """Evaluate open positions and auto-close paper positions that meet exit criteria."""
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
                decision["current_funding_rate_pct"] = round(current_rate * 100, 4)

                if not decision.get("close"):
                    # Add unrealized P&L estimate for held positions
                    unrealized = self.estimate_unrealized_pnl(position)
                    decision["unrealized"] = unrealized
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
            "entry_funding_rate_pct": round(item.entry_funding_rate * 100, 4),
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
        """Run a deterministic paper funding-arb cycle."""
        notional = float(notional_usdt or settings.FUNDING_ARB_MAX_NOTIONAL_USDT or 100.0)
        # Use a realistic positive funding rate for smoke (0.03% per period)
        funding_rate = 0.0003
        spot_price = 100.0
        swap_price = 100.05   # positive basis — perp slightly above spot
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
        monitor = FundingMonitorService()
        return {
            "status": "ok",
            "smoke": "funding_arb_paper_cycle",
            "opportunity": monitor.serialize_opportunity(opportunity),
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
        open_positions = db.query(FundingArbPosition).filter(FundingArbPosition.status == "open").all()
        closed = db.query(FundingArbPosition).filter(FundingArbPosition.status == "closed").all()
        realized = sum(float(item.realized_pnl or 0.0) for item in closed)
        latest = db.query(FundingArbOpportunity).order_by(FundingArbOpportunity.id.desc()).limit(10).all()
        monitor = FundingMonitorService()

        # Unrealized P&L for open positions
        unrealized_total = 0.0
        for pos in open_positions:
            try:
                est = self.estimate_unrealized_pnl(pos)
                if est.get("ok"):
                    unrealized_total += float(est.get("unrealized_pnl", 0))
            except Exception:
                pass

        return {
            "enabled": settings.ENABLE_FUNDING_ARB,
            "auto_open_paper": bool(getattr(settings, "FUNDING_ARB_AUTO_OPEN_PAPER", True)),
            "symbols": settings.funding_arb_symbols,
            "open_positions": len(open_positions),
            "closed_positions": len(closed),
            "realized_pnl": round(realized, 6),
            "unrealized_pnl_estimate": round(unrealized_total, 6),
            "total_pnl_estimate": round(realized + unrealized_total, 6),
            "latest_opportunities": [monitor.serialize_opportunity(item) for item in latest],
        }
