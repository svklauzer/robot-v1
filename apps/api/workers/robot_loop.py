from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session
from core.config import settings

from services.market_data import MarketDataService
from services.news_filter import NewsFilter
from services.strategy_engine import StrategyEngine
from services.ml_scorer import MLScorer
from services.ml_outcome_stats import MLOutcomeStatsService
from services.risk_engine import RiskEngine
from services.portfolio_engine import PortfolioEngine
from services.execution_engine import ExecutionEngine
from services.signal_broadcaster import SignalBroadcaster
from services.signal_lifecycle import SignalLifecycleManager
from services.signal_quality import SignalQualityService
from services.telegram_router import TelegramRouter
from services.trade_plan import TradePlanBuilder
from services.market_intelligence import MarketIntelligenceEngine
from services.exposure_guard import ExposureGuard
from services.symbol_performance_guard import SymbolPerformanceGuard
from services.production_entry_gate import ProductionEntryGate
from services.anti_drain_guard import AntiDrainConfig, should_open_signal
from services.ml_trade_logger import MLTradeLogger

from models.signal import Signal
from models.position import Position
from models.intelligence_event import IntelligenceEvent

class RobotLoop:
    def __init__(self):
        self.market = MarketDataService()
        self.news = NewsFilter()
        self.strategy = StrategyEngine()
        self.ml = MLScorer()
        self.risk = RiskEngine()
        self.portfolio = PortfolioEngine()

        self.intelligence = MarketIntelligenceEngine()
        self.trade_plan_builder = TradePlanBuilder()
        self.exposure_guard = ExposureGuard()
        self.symbol_performance_guard = SymbolPerformanceGuard()
        self.production_gate = ProductionEntryGate()

        self.execution = ExecutionEngine()
        self.broadcast = SignalBroadcaster()
        self.lifecycle = SignalLifecycleManager()
        self.quality = SignalQualityService()
        self.telegram_router = TelegramRouter()
        self.ml_trade_logger = MLTradeLogger()
        self.ml_outcome_stats = MLOutcomeStatsService()

        # Grade stats cache: refreshed every 20 loop iterations (~20 min)
        # to pick up new closed trades without hitting disk every 60s.
        self._grade_stats_cache: dict | None = None
        self._grade_stats_loop_counter: int = 0
        self._GRADE_STATS_REFRESH_EVERY = 20

    async def step(
        self,
        db: Session,
        bot,
        headlines: list[str],
        balance_usdt: float,
        daily_loss_pct: float,
        drawdown_pct: float,
    ):
        """
        Основной цикл робота.

        Правильная ответственность этого слоя:
        1. Сопровождать уже опубликованные сигналы через lifecycle.
        2. Проверять news/risk.
        3. Генерировать новые качественные сигналы.
        4. Публиковать сигналы в FREE/VIP.
        5. Не открывать order напрямую здесь.

        Открытие paper-position происходит в SignalLifecycleManager,
        когда цена входит в entry zone.
        """

        news_state = self.news.classify(headlines)

        # Сначала сопровождаем уже существующие сигналы:
        # published → opened → tp1 → closed/expired
        await self.lifecycle.process_open_signals(db, bot)

        # Если новости плохие — новые входы не создаём,
        # но сопровождение выше уже произошло.
        if news_state["state"] == "block_new_entries":
            await self.broadcast.send_owner_alert(
                "TRADING BLOCKED",
                f"News block: {news_state['reasons']}"
            )
            return

        open_positions = (
            db.query(Position)
            .filter(
                Position.bot_id == bot.id,
                Position.status == "open"
            )
            .all()
        )

        for symbol in bot.config_json.get("symbols", []):
            result = self.intelligence.analyze_symbol(symbol)

            if result.action == "hold":
                continue

            if result.setup_decision != "approve":
                continue

            if result.action == "short" and not settings.ALLOW_SHORTS:
                if self._should_send_short_block_alert(db, symbol):
                    await self.broadcast.send_owner_alert(
                        "SHORT CANDIDATE OBSERVED",
                        (
                            f"{symbol}\n"
                            f"Short-кандидат найден, но текущий режим исполнения "
                            f"{getattr(settings, 'EXECUTION_MARKET', 'spot')} / shorts_disabled.\n"
                            f"Short-сигнал не публикуется до подключения margin/futures short execution module.\n"
                            f"Regime: {result.regime}\n"
                            f"Confidence: {result.confidence_hint}\n"
                            f"Scores: {result.scores}"
                        )
                    )

                    event = IntelligenceEvent(
                        symbol=symbol,
                        status="blocked",
                        decision="short_candidate_but_shorts_disabled",
                        action=result.action,
                        regime=result.regime,
                        radar_state=result.radar_state,
                        confidence_hint=result.confidence_hint,
                        setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                        payload_json={
                            "symbol": symbol,
                            "status": "blocked",
                            "decision": "short_candidate_but_shorts_disabled",
                            "alert_decision": "robot_short_candidate_alert_sent",
                            "block_reason": f"current_execution_mode_{getattr(settings, 'EXECUTION_MARKET', 'spot')}_shorts_disabled",
                            "action": result.action,
                            "regime": result.regime,
                            "radar_state": result.radar_state,
                            "confidence_hint": result.confidence_hint,
                            "scores": result.scores,
                            "setup_quality": result.setup_quality,
                            "setup_decision": result.setup_decision,
                            "reason": result.reason,
                        },
                    )

                    db.add(event)
                    db.flush()

                continue

            entry_from = float(result.entry_zone[0])
            entry_to = float(result.entry_zone[1])
            entry_price = round((entry_from + entry_to) / 2, 2)

            stop = float(result.stop_price)
            tp1 = float(result.tp["tp1"])
            tp2 = float(result.tp["tp2"])

            signal_payload = {
                "action": result.action,
                "symbol": symbol,
                "entry_zone": [entry_from, entry_to],
                "stop_price": stop,
                "tp": {
                    "tp1": tp1,
                    "tp2": tp2,
                },
                "reason": f"intelligence_{result.reason}",
            }

            effective_confidence = self._intelligence_effective_confidence(result)

            setup_quality = result.setup_quality if isinstance(result.setup_quality, dict) else {}
            setup_score = setup_quality.get("final_score")
            setup_decision = result.setup_decision

            grade = self.quality.grade(
                confidence=result.confidence_hint,
                rationale=signal_payload["reason"],
                regime=result.regime,
                setup_score=setup_score,
                effective_confidence=effective_confidence,
            )

            expires_at = self.quality.expiry_time(grade)

            should_publish = self.quality.should_publish_to_clients(
                grade=grade,
                setup_score=setup_score,
                effective_confidence=effective_confidence,
                setup_decision=setup_decision,
                setup_quality=setup_quality,
            )

            if not should_publish:
                await self.broadcast.send_owner_alert(
                    "INTELLIGENCE LOW QUALITY",
                    (
                        f"{symbol} {result.action}\n"
                        f"Grade: {grade}\n"
                        f"Confidence: {result.confidence_hint}\n"
                        f"Effective confidence: {effective_confidence}\n"
                        f"Setup score: {setup_score}\n"
                        f"Setup decision: {setup_decision}\n"
                        f"Regime: {result.regime}\n"
                        f"Reason: {result.reason}\n"
                        f"Scores: {result.scores}"
                    )
                )
                continue

            plan = self.trade_plan_builder.build_plan(
                symbol=symbol,
                side=result.action,
                entry_price=entry_price,
                stop_price=stop,
                tp1=tp1,
                tp2=tp2,
                balance_usdt=balance_usdt,
            )

            if not plan.is_valid:
                if self._should_send_plan_reject_alert(db, symbol, str(plan.reject_reason or "unknown")):
                    await self.broadcast.send_owner_alert(
                        "INTELLIGENCE TRADE PLAN REJECTED",
                        (
                            f"{symbol} {result.action}\n"
                            f"Reason: {plan.reject_reason}\n"
                            f"Regime: {result.regime}\n"
                            f"Confidence: {result.confidence_hint}\n"
                            f"Entry: {entry_price}\n"
                            f"Stop: {stop}\n"
                            f"TP1: {tp1}\n"
                            f"TP2: {tp2}\n"
                            f"Net TP1: {plan.net_pnl_tp1} USDT\n"
                            f"Net TP2: {plan.net_pnl_tp2} USDT\n"
                            f"Net Stop: {plan.net_pnl_stop} USDT\n"
                            f"RR TP2: {plan.net_rr_tp2}\n"
                            f"Scores: {result.scores}"
                        )
                    )

                db.add(
                    IntelligenceEvent(
                        symbol=symbol,
                        status="rejected",
                        decision=str(plan.reject_reason or "trade_plan_invalid"),
                        action=result.action,
                        regime=result.regime,
                        radar_state=result.radar_state,
                        confidence_hint=result.confidence_hint,
                        setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                        payload_json={
                            "symbol": symbol,
                            "status": "rejected",
                            "decision": str(plan.reject_reason or "trade_plan_invalid"),
                            "action": result.action,
                            "regime": result.regime,
                            "radar_state": result.radar_state,
                            "confidence_hint": result.confidence_hint,
                            "scores": result.scores,
                            "setup_quality": result.setup_quality,
                            "setup_decision": result.setup_decision,
                            "reason": result.reason,
                            "plan": {
                                "net_pnl_tp1": plan.net_pnl_tp1,
                                "net_pnl_tp2": plan.net_pnl_tp2,
                                "net_pnl_stop": plan.net_pnl_stop,
                                "net_rr_tp2": plan.net_rr_tp2,
                                "required_margin": plan.required_margin,
                                "reject_reason": plan.reject_reason,
                            },
                        },
                    )
                )
                db.flush()
                continue

            performance = self.symbol_performance_guard.analyze(
                db=db,
                bot_id=bot.id,
                symbol=symbol,
                lookback=int(getattr(settings, "SYMBOL_PERF_LOOKBACK", 12)),
            )

            if not performance.allowed:
                await self.broadcast.send_owner_alert(
                    "INTELLIGENCE PERFORMANCE BLOCKED",
                    (
                        f"{symbol} {result.action}\n"
                        f"Reason: {performance.reason}\n"
                        f"Closed: {performance.closed_count}\n"
                        f"Winrate: {performance.winrate}%\n"
                        f"Total net PnL: {performance.total_net_pnl} USDT\n"
                        f"Losing streak: {performance.losing_streak}\n"
                        f"Failed setup count: {performance.failed_setup_count}\n"
                        f"Positive→negative: {performance.positive_then_negative_count}"
                    ),
                )
                db.add(
                    IntelligenceEvent(
                        symbol=symbol,
                        status="blocked",
                        decision=performance.reason,
                        action=result.action,
                        regime=result.regime,
                        radar_state=result.radar_state,
                        confidence_hint=result.confidence_hint,
                        setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                        payload_json={
                            "symbol": symbol,
                            "status": "blocked",
                            "decision": performance.reason,
                            "action": result.action,
                            "regime": result.regime,
                            "radar_state": result.radar_state,
                            "confidence_hint": result.confidence_hint,
                            "scores": result.scores,
                            "setup_quality": result.setup_quality,
                            "setup_decision": result.setup_decision,
                            "reason": result.reason,
                            "performance_guard": performance.to_payload(),
                        },
                    )
                )
                db.flush()
                continue

            production_decision = self.production_gate.check(
                grade=grade,
                setup_score=setup_score,
                effective_confidence=effective_confidence,
                net_rr_tp1=plan.net_rr_tp1,
                net_rr_tp2=plan.net_rr_tp2,
                priority_score=100.0,
            )

            if not production_decision.allowed:
                db.add(
                    IntelligenceEvent(
                        symbol=symbol,
                        status="rejected",
                        decision=production_decision.reason,
                        action=result.action,
                        regime=result.regime,
                        radar_state=result.radar_state,
                        confidence_hint=result.confidence_hint,
                        setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                        payload_json={
                            "symbol": symbol,
                            "status": "rejected",
                            "decision": production_decision.reason,
                            "action": result.action,
                            "regime": result.regime,
                            "radar_state": result.radar_state,
                            "confidence_hint": result.confidence_hint,
                            "scores": result.scores,
                            "setup_quality": result.setup_quality,
                            "setup_decision": result.setup_decision,
                            "reason": result.reason,
                            "plan": {
                                "qty": plan.qty,
                                "required_margin": plan.required_margin,
                                "net_pnl_tp1": plan.net_pnl_tp1,
                                "net_pnl_tp2": plan.net_pnl_tp2,
                                "net_pnl_stop": plan.net_pnl_stop,
                                "net_rr_tp1": plan.net_rr_tp1,
                                "net_rr_tp2": plan.net_rr_tp2,
                            },
                            "production_gate": production_decision.payload,
                        },
                    )
                )
                db.flush()
                continue

            policy_profile = self.symbol_performance_guard.policy_profile(performance)
            policy_decision = self._check_symbol_policy_profile(policy_profile, production_decision.payload)
            if not policy_decision["allowed"]:
                db.add(
                    IntelligenceEvent(
                        symbol=symbol,
                        status="rejected",
                        decision=policy_decision["reason"],
                        action=result.action,
                        regime=result.regime,
                        radar_state=result.radar_state,
                        confidence_hint=result.confidence_hint,
                        setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                        payload_json={
                            "symbol": symbol,
                            "status": "rejected",
                            "decision": policy_decision["reason"],
                            "action": result.action,
                            "regime": result.regime,
                            "radar_state": result.radar_state,
                            "confidence_hint": result.confidence_hint,
                            "scores": result.scores,
                            "setup_quality": result.setup_quality,
                            "setup_decision": result.setup_decision,
                            "reason": result.reason,
                            "performance_guard": performance.to_payload(),
                            "symbol_policy_profile": policy_profile,
                            "symbol_policy_check": policy_decision,
                            "production_gate": production_decision.payload,
                        },
                    )
                )
                db.flush()
                continue

            performance_adjustment = self._apply_symbol_performance_adjustment(plan, performance)
            performance_adjustment["policy_profile"] = policy_profile
            if float(plan.qty or 0) <= 0:
                continue

            exposure_result = self.exposure_guard.check_before_publish(
                db=db,
                bot_id=bot.id,
                symbol=symbol,
                required_margin=float(plan.required_margin or 0),
                equity_usdt=float(getattr(settings, "RISK_EQUITY_USDT", balance_usdt)),
                max_used_margin_pct=float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85)),
                max_active_signals=int(getattr(settings, "MAX_ACTIVE_SIGNALS", 2)),
                max_active_per_symbol=int(getattr(settings, "MAX_ACTIVE_SIGNALS_PER_SYMBOL", 1)),
            )

            if bool(getattr(settings, "ANTI_DRAIN_ENABLED", True)):
                anti_cfg = AntiDrainConfig(
                    min_confidence=float(getattr(settings, "ANTI_DRAIN_MIN_CONFIDENCE", 55.0)),
                    min_net_rr_tp1=float(getattr(settings, "ANTI_DRAIN_MIN_NET_RR_TP1", 0.40)),
                    min_net_rr_tp2=float(getattr(settings, "ANTI_DRAIN_MIN_NET_RR_TP2", 0.85)),
                    min_expected_edge_after_costs_usdt=float(getattr(settings, "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 0.80)),
                    max_position_margin_pct=float(getattr(settings, "ANTI_DRAIN_MAX_POSITION_MARGIN_PCT", 12.0)),
                    max_used_margin_pct=float(getattr(settings, "ANTI_DRAIN_MAX_USED_MARGIN_PCT", 30.0)),
                    max_open_positions=int(getattr(settings, "ANTI_DRAIN_MAX_OPEN_POSITIONS", 2)),
                    max_active_signals_per_symbol=int(getattr(settings, "ANTI_DRAIN_MAX_ACTIVE_PER_SYMBOL", 1)),
                    max_daily_loss_pct=float(getattr(settings, "ANTI_DRAIN_MAX_DAILY_LOSS_PCT", 3.0)),
                    max_drawdown_pct=float(getattr(settings, "ANTI_DRAIN_MAX_DRAWDOWN_PCT", 12.0)),
                )
                anti_allowed, anti_reason = should_open_signal(
                    {
                        "symbol": symbol,
                        "side": result.action,
                        "grade": grade,
                        "confidence": effective_confidence,
                        "rationale": str(result.reason or ""),
                        "required_margin": plan.required_margin,
                        "net_rr_tp1": plan.net_rr_tp1,
                        "net_rr_tp2": plan.net_rr_tp2,
                        "net_pnl_tp1": plan.net_pnl_tp1,
                        "net_pnl_stop": plan.net_pnl_stop,
                    },
                    {
                        "equity_usdt": float(getattr(settings, "RISK_EQUITY_USDT", balance_usdt)),
                        "used_margin_usdt": float(exposure_result.used_margin or 0),
                        "daily_pnl_usdt": -abs(float(daily_loss_pct or 0)) * float(balance_usdt) / 100.0,
                        "drawdown_pct": float(drawdown_pct or 0),
                        "open_positions_count": len(open_positions),
                        "active_signals_by_symbol": {symbol: int(exposure_result.active_symbol_signals_count or 0)},
                    },
                    anti_cfg,
                )
                if not anti_allowed:
                    db.add(
                        IntelligenceEvent(
                            symbol=symbol,
                            status="blocked",
                            decision=anti_reason,
                            action=result.action,
                            regime=result.regime,
                            radar_state=result.radar_state,
                            confidence_hint=result.confidence_hint,
                            setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                            payload_json={"symbol": symbol, "status": "blocked", "decision": anti_reason, "anti_drain": True},
                        )
                    )
                    db.flush()
                    continue

            if not exposure_result.allowed:
                await self.broadcast.send_owner_alert(
                    "INTELLIGENCE EXPOSURE BLOCKED",
                    (
                        f"{symbol} {result.action}\n"
                        f"Reason: {exposure_result.reason}\n"
                        f"Required margin: {exposure_result.required_margin} USDT\n"
                        f"Used margin: {exposure_result.used_margin} USDT\n"
                        f"Free margin: {exposure_result.free_margin} USDT\n"
                        f"Max allowed margin: {exposure_result.max_allowed_margin} USDT\n"
                        f"Active signals: {exposure_result.active_signals_count}\n"
                        f"Active symbol signals: {exposure_result.active_symbol_signals_count}"
                    )
                )

                event = IntelligenceEvent(
                    symbol=symbol,
                    status="blocked",
                    decision=exposure_result.reason,
                    action=result.action,
                    regime=result.regime,
                    radar_state=result.radar_state,
                    confidence_hint=result.confidence_hint,
                    setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                    payload_json={
                        "symbol": symbol,
                        "status": "blocked",
                        "decision": exposure_result.reason,
                        "action": result.action,
                        "regime": result.regime,
                        "radar_state": result.radar_state,
                        "confidence_hint": result.confidence_hint,
                        "scores": result.scores,
                        "setup_quality": result.setup_quality,
                        "setup_decision": result.setup_decision,
                        "reason": result.reason,
                        "exposure": {
                            "allowed": exposure_result.allowed,
                            "reason": exposure_result.reason,
                            "active_signals_count": exposure_result.active_signals_count,
                            "active_symbol_signals_count": exposure_result.active_symbol_signals_count,
                            "used_margin": exposure_result.used_margin,
                            "max_allowed_margin": exposure_result.max_allowed_margin,
                            "free_margin": exposure_result.free_margin,
                            "required_margin": exposure_result.required_margin,
                        },
                    },
                )

                db.add(event)
                db.flush()
                continue

            sig = Signal(
                bot_id=bot.id,
                symbol=symbol,
                side=result.action,
                status="published",
                entry_zone_json={"from": entry_from, "to": entry_to},
                stop_price=stop,
                tp_json=signal_payload["tp"],
                confidence=effective_confidence,
                rationale=signal_payload["reason"],
                grade=grade,
                is_public=should_publish,
                expires_at=expires_at,

                qty=plan.qty,
                required_margin=plan.required_margin,
                net_rr_tp1=plan.net_rr_tp1,
                net_rr_tp2=plan.net_rr_tp2,
                net_pnl_tp1=plan.net_pnl_tp1,
                net_pnl_tp2=plan.net_pnl_tp2,
                net_pnl_stop=plan.net_pnl_stop,
                plan_json={
                    "qty": plan.qty,
                    "required_margin": plan.required_margin,
                    "net_pnl_tp1": plan.net_pnl_tp1,
                    "net_pnl_tp2": plan.net_pnl_tp2,
                    "net_pnl_stop": plan.net_pnl_stop,
                    "net_rr_tp1": plan.net_rr_tp1,
                    "net_rr_tp2": plan.net_rr_tp2,
                    "is_valid": plan.is_valid,
                    "reject_reason": plan.reject_reason,
                    "performance_guard": performance_adjustment,
                    # Режим сделки для exit-политики: trend → ride (едем движение),
                    # scalp → быстрый выход. Range-вход (Phase 2) проставит "scalp".
                    "trade_mode": "scalp" if "range" in str(result.regime or "") else "trend",
                },
            )

            db.add(sig)
            db.flush()
            db.refresh(sig)

            await self._publish_new_signal_safely(
                db=db,
                sig=sig,
                signal_payload=signal_payload,
                effective_confidence=effective_confidence,
                grade=grade,
                is_public=should_publish,
                result=result,
            )


    def _check_symbol_policy_profile(self, policy_profile: dict, gate_payload: dict) -> dict:
        if not bool(policy_profile.get("publish_allowed", True)):
            return {
                "allowed": False,
                "reason": "symbol_policy_publish_blocked",
                "policy_profile": policy_profile,
            }

        thresholds = gate_payload.get("thresholds") or {}
        confidence = float(gate_payload.get("effective_confidence") or 0.0)
        rr1 = float(gate_payload.get("net_rr_tp1") or 0.0)
        rr2 = float(gate_payload.get("net_rr_tp2") or 0.0)
        confidence_delta = float(policy_profile.get("min_confidence_delta") or 0.0)
        rr_delta = float(policy_profile.get("min_rr_delta") or 0.0)

        required_confidence = float(thresholds.get("min_confidence") or 0.0) + confidence_delta
        required_rr1 = float(thresholds.get("min_rr_tp1") or 0.0) + rr_delta
        required_rr2 = float(thresholds.get("min_rr_tp2") or 0.0) + rr_delta

        payload = {
            "allowed": True,
            "reason": "symbol_policy_passed",
            "policy_profile": policy_profile,
            "required_confidence": round(required_confidence, 4),
            "required_rr_tp1": round(required_rr1, 4),
            "required_rr_tp2": round(required_rr2, 4),
            "actual_confidence": confidence,
            "actual_rr_tp1": rr1,
            "actual_rr_tp2": rr2,
        }

        if confidence < required_confidence:
            return {**payload, "allowed": False, "reason": "symbol_policy_confidence_too_low"}
        if rr1 < required_rr1:
            return {**payload, "allowed": False, "reason": "symbol_policy_rr_tp1_too_low"}
        if rr2 < required_rr2:
            return {**payload, "allowed": False, "reason": "symbol_policy_rr_tp2_too_low"}

        return payload

    def _apply_symbol_performance_adjustment(self, plan, performance) -> dict:
        multiplier = float(getattr(performance, "risk_multiplier", 1.0) or 1.0)
        original = {
            "qty": float(plan.qty or 0),
            "required_margin": float(plan.required_margin or 0),
            "net_pnl_tp1": float(plan.net_pnl_tp1 or 0),
            "net_pnl_tp2": float(plan.net_pnl_tp2 or 0),
            "net_pnl_stop": float(plan.net_pnl_stop or 0),
        }

        plan.qty = round(original["qty"] * multiplier, 6)
        plan.required_margin = round(original["required_margin"] * multiplier, 6)
        plan.net_pnl_tp1 = round(original["net_pnl_tp1"] * multiplier, 6)
        plan.net_pnl_tp2 = round(original["net_pnl_tp2"] * multiplier, 6)
        plan.net_pnl_stop = round(original["net_pnl_stop"] * multiplier, 6)

        return {
            "allowed": bool(getattr(performance, "allowed", True)),
            "reason": getattr(performance, "reason", "symbol_performance_ok"),
            "risk_multiplier": multiplier,
            "symbol": getattr(performance, "symbol", None),
            "classification": "reduced" if multiplier < 1.0 else "ok",
            "original": original,
            "adjusted": {
                "qty": plan.qty,
                "required_margin": plan.required_margin,
                "net_pnl_tp1": plan.net_pnl_tp1,
                "net_pnl_tp2": plan.net_pnl_tp2,
                "net_pnl_stop": plan.net_pnl_stop,
            },
        }

    async def _publish_new_signal_safely(
        self,
        *,
        db: Session,
        sig: Signal,
        signal_payload: dict,
        effective_confidence: float,
        grade: str,
        is_public: bool,
        result=None,
    ) -> bool:
        """
        Publish Telegram notification without rolling back the trading state.

        In paper mode Telegram delivery is operationally important, but it must not
        erase a valid published signal: otherwise the lifecycle never gets a
        chance to open a paper position or write ML outcomes. In live modes we
        still keep the DB transaction safe and mark the signal as telegram_failed.
        """
        try:
            await self.telegram_router.publish_new_signal(
                signal=signal_payload,
                confidence=effective_confidence,
                grade=grade,
                signal_id=sig.id,
                is_public=is_public,
            )
            return True

        except Exception as telegram_error:
            error_text = f"{type(telegram_error).__name__}: {repr(telegram_error)}"
            live_delivery_required = bool(getattr(settings, "is_live_enabled", False)) or str(
                getattr(settings, "TRADING_MODE", "paper_signal")
            ).lower().startswith("live")
            vip_delivery_required = bool(is_public)
            # In paper modes Telegram is delivery/SLA telemetry, not a trading-state
            # gate. Keep public paper signals active so the lifecycle can open and
            # close paper positions while Telegram retry worker restores delivery.
            delivery_required = live_delivery_required

            plan_json = sig.plan_json or {}
            plan_json["telegram_delivery"] = {
                "ok": False,
                "error": error_text,
                "mode": "required_live" if delivery_required else "non_blocking_paper",
                "vip_delivery_required": vip_delivery_required,
                "live_delivery_required": live_delivery_required,
            }
            sig.plan_json = plan_json

            if delivery_required:
                sig.status = "telegram_failed"
                sig.closed_reason = "initial_vip_telegram_publish_failed" if vip_delivery_required else "initial_telegram_publish_failed"
                event_status = "telegram_failed"
                event_decision = sig.closed_reason
            else:
                # Paper diagnostics and public paper signals continue even if
                # Telegram delivery fails; delivery retry/SLA is tracked separately.
                event_status = "warning"
                event_decision = "telegram_delivery_failed_signal_kept_published"

            db.add(
                IntelligenceEvent(
                    symbol=sig.symbol,
                    status=event_status,
                    decision=event_decision,
                    action=sig.side,
                    regime=getattr(result, "regime", None),
                    radar_state=getattr(result, "radar_state", None),
                    confidence_hint=getattr(result, "confidence_hint", None),
                    setup_score=(getattr(result, "setup_quality", {}) or {}).get("final_score", 0.0)
                    if result is not None
                    else None,
                    payload_json={
                        "signal_id": sig.id,
                        "symbol": sig.symbol,
                        "side": sig.side,
                        "grade": grade,
                        "is_public": is_public,
                        "telegram_error": error_text,
                        "delivery_required": delivery_required,
                        "vip_delivery_required": vip_delivery_required,
                        "live_delivery_required": live_delivery_required,
                    },
                )
            )
            db.flush()
            return False

    def _intelligence_effective_confidence(self, result) -> float:
        """
        Калибрует confidence для Intelligence-сигналов перед grade/publish.

        Этапы калибровки:
        1. Base = confidence_hint из MarketIntelligenceEngine
        2. Setup quality adjustment (setup_score / approve / wait)
        3. MLScorer v2 adjustment — выравнивает через multi-factor features
        """

        base = float(result.confidence_hint or 0)

        setup_quality = result.setup_quality if isinstance(result.setup_quality, dict) else {}
        setup_score = float(setup_quality.get("final_score") or 0)
        setup_decision = str(setup_quality.get("decision") or result.setup_decision or "")

        if setup_decision == "approve" and setup_score >= 70:
            calibrated = max(base, setup_score * 0.90)
            calibrated = round(min(calibrated, 88.0), 2)
        elif setup_decision == "wait" and setup_score >= 55:
            calibrated = max(base, setup_score * 0.75)
            calibrated = round(min(calibrated, 72.0), 2)
        else:
            calibrated = round(base, 2)

        # ── MLScorer v2 secondary calibration ────────────────────────────────
        # Extract features from the primary signal timeframe (15m preferred, else 5m).
        try:
            timeframes = result.timeframes if isinstance(result.timeframes, dict) else {}
            tf_ctx = timeframes.get("15m") or timeframes.get("5m") or {}
            if isinstance(tf_ctx, dict) and tf_ctx:
                ml_features = {
                    "last_close":   tf_ctx.get("last_close", 0),
                    "ema20":        tf_ctx.get("ema20", 0),
                    "ema50":        tf_ctx.get("ema50", 0),
                    "volume":       tf_ctx.get("volume", 0),
                    "volume_ma":    tf_ctx.get("volume_ma20", 1) or 1,
                    "rsi":          tf_ctx.get("rsi14", 50.0),
                    "macd_hist":    tf_ctx.get("macd_hist", 0),
                    "macd_hist_prev": 0.0,  # not stored in ctx; neutral
                }
                grade_stats = self._get_grade_stats()
                ml_result = self.ml.score(
                    ml_features,
                    regime=result.regime,
                    grade=getattr(result, "grade", None),
                    grade_stats=grade_stats,
                )
                ml_confidence = ml_result.confidence  # [35, 95]

                # Blend: 70% calibrated (from intelligence) + 30% MLScorer
                blended = calibrated * 0.70 + ml_confidence * 0.30
                calibrated = round(min(blended, 92.0), 2)
        except Exception:
            pass  # MLScorer errors must never block signal generation

        return calibrated

    def _get_grade_stats(self) -> dict | None:
        """Return cached grade stats, refreshing every N loop iterations."""
        self._grade_stats_loop_counter += 1
        if (
            self._grade_stats_cache is None
            or self._grade_stats_loop_counter >= self._GRADE_STATS_REFRESH_EVERY
        ):
            try:
                self._grade_stats_cache = self.ml_outcome_stats.grade_stats(min_count=3)
                self._grade_stats_loop_counter = 0
            except Exception:
                pass  # stale cache is fine; never block the loop
        return self._grade_stats_cache

    def _should_send_short_block_alert(self, db: Session, symbol: str) -> bool:
        minutes = getattr(settings, "SHORT_ALERT_THROTTLE_MINUTES", 60)
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)

        recent = (
            db.query(IntelligenceEvent)
            .filter(
                IntelligenceEvent.symbol == symbol,
                IntelligenceEvent.status == "blocked",
                IntelligenceEvent.decision == "robot_short_candidate_alert_sent",
                IntelligenceEvent.created_at >= since,
            )
            .first()
        )

        return recent is None

    def _should_send_plan_reject_alert(self, db: Session, symbol: str, reject_reason: str) -> bool:
        minutes = int(getattr(settings, "PLAN_REJECT_ALERT_THROTTLE_MINUTES", 30))
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)

        recent = (
            db.query(IntelligenceEvent)
            .filter(
                IntelligenceEvent.symbol == symbol,
                IntelligenceEvent.status == "rejected",
                IntelligenceEvent.decision == reject_reason,
                IntelligenceEvent.created_at >= since,
            )
            .first()
        )

        return recent is None