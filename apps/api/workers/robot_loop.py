from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session
from core.config import settings

from services.market_data import MarketDataService
from services.news_filter import NewsFilter
from services.strategy_engine import StrategyEngine
from services.ml_scorer import MLScorer
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

        self.execution = ExecutionEngine()
        self.broadcast = SignalBroadcaster()
        self.lifecycle = SignalLifecycleManager()
        self.quality = SignalQualityService()
        self.telegram_router = TelegramRouter()

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
                },
            )

            db.add(sig)
            db.flush()

            await self.telegram_router.publish_new_signal(
                signal=signal_payload,
                confidence=effective_confidence,
                grade=grade,
                signal_id=sig.id,
            )            

    def _intelligence_effective_confidence(self, result) -> float:
        """
        Калибрует confidence для Intelligence-сигналов перед grade/publish.
        """

        base = float(result.confidence_hint or 0)

        setup_quality = result.setup_quality if isinstance(result.setup_quality, dict) else {}
        setup_score = float(setup_quality.get("final_score") or 0)
        setup_decision = str(setup_quality.get("decision") or result.setup_decision or "")

        if setup_decision == "approve" and setup_score >= 70:
            calibrated = max(base, setup_score * 0.90)
            return round(min(calibrated, 88.0), 2)

        if setup_decision == "wait" and setup_score >= 55:
            calibrated = max(base, setup_score * 0.75)
            return round(min(calibrated, 72.0), 2)

        return round(base, 2)

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