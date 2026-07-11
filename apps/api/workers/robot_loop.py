from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session
from core.config import settings

from services.market_data import MarketDataService
from services.news_filter import NewsFilter
from services.strategy_engine import StrategyEngine
from services.ml_scorer import MLScorer
from services.ml_outcome_stats import MLOutcomeStatsService
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
from services.reentry_cooldown import ReEntryCooldownGuard
from services.anti_drain_guard import AntiDrainConfig, should_open_signal
from services.orderbook_analyzer import OrderBookAnalyzer
from services.orderbook_feed import ORDERBOOK_STORE
from services.ml_trade_logger import MLTradeLogger
from services.ml_controller import MLController

from models.signal import Signal
from models.position import Position
from models.intelligence_event import IntelligenceEvent

class RobotLoop:
    def __init__(self):
        self.market = MarketDataService()
        self.news = NewsFilter()
        self.strategy = StrategyEngine()
        self.ml = MLScorer()
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
        self.ml_controller = MLController()
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

        # ── Динамический бюджет маржи на сделку (пред-проход) ─────────────────
        # Считаем готовых кандидатов цикла и делим свободную маржу. analyses_cache
        # переиспользуем в основном цикле, чтобы analyze_symbol не считался дважды.
        dyn_alloc = bool(getattr(settings, "ENABLE_DYNAMIC_MARGIN_ALLOC", True))
        dyn_budget = None
        analyses_cache: dict = {}
        _dyn_free = 0.0
        if dyn_alloc:
            try:
                dyn_budget, analyses_cache, _dyn_ready, _dyn_free = self._compute_dynamic_budget(db, bot, balance_usdt)
            except Exception as exc:  # noqa: BLE001
                print(f"[DYNAMIC MARGIN ERROR] {exc}")
                dyn_budget, analyses_cache, _dyn_free = None, {}, 0.0

        for symbol in bot.config_json.get("symbols", []):
            result = analyses_cache[symbol] if symbol in analyses_cache else self.intelligence.analyze_symbol(symbol)
            if result is None:
                continue

            if result.action == "hold":
                continue

            if result.setup_decision != "approve":
                continue

            # RANGE-скальп идёт по своему скорингу. Он обходит трендовые гейты
            # (grade-публикация, production_gate, symbol-policy), но проходит
            # генерик-защиту: plan/RR, symbol-performance, exposure, anti-drain.
            # range и crt идут одним «альт-стратегия» путём: байпас quality/production
            # гейтов + scalp-сайзинг (чтобы проходили anti-drain). Режим выхода
            # разводится отдельно через trade_mode (range→scalp, crt→trend-ride).
            is_range = str(getattr(result, "regime", "")) in ("range", "crt", "scalp")

            # Range-шорт включается своим флагом RANGE_ALLOW_SHORT и не зависит
            # от трендового ALLOW_SHORTS — поэтому range его не блокирует.
            if result.action == "short" and not settings.ALLOW_SHORTS and not is_range:
                if self._should_send_short_block_alert(db, symbol):
                    await self.broadcast.send_owner_alert(
                        "SHORT CANDIDATE OBSERVED",
                        (
                            f"{symbol}\n"
                            f"Short-кандидат найден, но текущий режим исполнения "
                            f"{settings.execution_market_type} / shorts_disabled.\n"
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
                            "block_reason": f"current_execution_mode_{settings.execution_market_type}_shorts_disabled",
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
            # ФИКС (#4): round(...,2) ломал суб-долларовые символы. Для ADA
            # (~0.166) середина зоны 0.1662 округлялась до 0.17 — это ВЫШЕ стопа
            # шорта (0.1686), поэтому проверка tp2<tp1<entry<stop падала и КАЖДЫЙ
            # цикл выдавал invalid_short_directional_levels, отсекая лучшие сетапы.
            # Используем точность цены биржи вместо жёсткого round(.,2).
            entry_mid = (entry_from + entry_to) / 2.0
            try:
                entry_price = float(
                    self.trade_plan_builder.htx.price_to_precision(symbol, entry_mid)
                )
            except Exception:
                entry_price = entry_mid

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

            should_publish = is_range or self.quality.should_publish_to_clients(
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

            # ── Post-loss cooldown (только range-скальп) ────────────────────
            # После убыточного закрытия по символу+стороне не лезем повторно
            # N минут — режет churn. CRT/тренд не трогаем.
            if (
                str(getattr(result, "regime", "")) == "range"
                and bool(getattr(settings, "POST_LOSS_COOLDOWN_ENABLED", True))
            ):
                cd_min = float(getattr(settings, "POST_LOSS_COOLDOWN_MIN", 25.0))
                last_loss = (
                    db.query(Signal)
                    .filter(
                        Signal.symbol == symbol,
                        Signal.side == result.action,
                        Signal.status == "closed",
                        Signal.closed_net_pnl < 0,
                    )
                    .order_by(Signal.closed_at.desc())
                    .first()
                )
                if last_loss is not None and last_loss.closed_at is not None:
                    closed_at = last_loss.closed_at
                    if closed_at.tzinfo is None:
                        closed_at = closed_at.replace(tzinfo=timezone.utc)
                    age_min = (datetime.now(timezone.utc) - closed_at).total_seconds() / 60.0
                    if age_min < cd_min:
                        if not self._should_record_block_event(db, symbol, "blocked_post_loss_cooldown"):
                            continue
                        db.add(
                            IntelligenceEvent(
                                symbol=symbol,
                                status="blocked",
                                decision="blocked_post_loss_cooldown",
                                action=result.action,
                                regime=result.regime,
                                radar_state=result.radar_state,
                                confidence_hint=result.confidence_hint,
                                setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                                payload_json={
                                    "symbol": symbol,
                                    "status": "blocked",
                                    "decision": "blocked_post_loss_cooldown",
                                    "side": result.action,
                                    "cooldown_min": cd_min,
                                    "since_loss_min": round(age_min, 1),
                                    "last_loss_signal_id": last_loss.id,
                                    "last_loss_reason": last_loss.closed_reason,
                                },
                            )
                        )
                        db.flush()
                        continue

            # ── Depth-гейт (стакан): спред + OBI/стенки подтверждают вход ──────
            # Нет свежих WS-данных → pass-through (не блокируем торговлю).
            if bool(getattr(settings, "ENABLE_ORDERBOOK_ENGINE", False)) and bool(getattr(settings, "OB_GATE_ENTRIES", True)):
                ob_snap = ORDERBOOK_STORE.snapshot(symbol)
                if ob_snap and ob_snap.get("age_sec", 1e9) > float(getattr(settings, "OB_DATA_MAX_AGE_SEC", 15.0)):
                    ob_snap = None
                ob_sig = OrderBookAnalyzer.analyze(ob_snap, levels=int(getattr(settings, "OB_DEPTH_LEVELS", 10)))
                # LiquidityGuard: кормим адаптивную базу спредом символа и блокируем
                # вход при аномально широком спреде (свип-риск тонкой ликвидности).
                try:
                    from services.liquidity_guard import LIQUIDITY_GUARD
                    _sp_bps = (ob_sig.spread_pct * 100.0) if ob_sig and ob_sig.spread_pct is not None else None
                    _liq_block, _liq_reason, _ = LIQUIDITY_GUARD.entry_blocked(symbol, sp_bps=_sp_bps)
                except Exception:
                    _liq_block, _liq_reason = False, ""
                # Профиль-зависимый спред-кап: scalp/range — туго (фил критичен),
                # trend/crt (POSITION) — шире (едем 1.5–3%, спред 0.1–0.2% = шум).
                _is_position = str(result.regime or "").lower() not in ("range", "scalp")
                _max_spread = float(getattr(
                    settings,
                    "OB_POSITION_MAX_SPREAD_PCT" if _is_position else "OB_MAX_SPREAD_PCT",
                    0.20 if _is_position else 0.08,
                ))
                ob_ok, ob_reason = OrderBookAnalyzer.entry_gate(
                    result.action, ob_sig,
                    max_spread_pct=_max_spread,
                    obi_confirm=float(getattr(settings, "OB_OBI_CONFIRM", 0.15)),
                    wall_confirm=float(getattr(settings, "OB_WALL_CONFIRM_SHARE", 0.30)),
                    # CVD на входе: не входим против агрессивного исполненного потока.
                    cvd_block_ratio=float(getattr(settings, "OB_CVD_ENTRY_BLOCK_RATIO", 0.6)),
                    cvd_min_trades=int(getattr(settings, "OB_CVD_MIN_TRADES", 25)),
                    # Жёсткое вето при подавляющем OBI против входа (стенка не спасает).
                    obi_hard_veto=float(getattr(settings, "OB_OBI_HARD_VETO", 0.75)),
                    # (#leak-A) стенка не спасает при встречном OBI глубже порога;
                    # CVD режет и на тонкой выборке при ~100% потоке против.
                    wall_rescue_max_adverse_obi=float(getattr(settings, "OB_WALL_RESCUE_MAX_ADVERSE_OBI", 0.35)),
                    cvd_thin_ratio=float(getattr(settings, "OB_CVD_THIN_RATIO", 0.9)),
                    cvd_thin_min_trades=int(getattr(settings, "OB_CVD_THIN_MIN_TRADES", 1)),
                )
                if not ob_ok or _liq_block:
                    _block_reason = ob_reason if not ob_ok else f"liq_spread:{_liq_reason}"
                    if not self._should_record_block_event(db, symbol, "blocked_depth_gate"):
                        continue
                    db.add(
                        IntelligenceEvent(
                            symbol=symbol,
                            status="blocked",
                            decision="blocked_depth_gate",
                            action=result.action,
                            regime=result.regime,
                            radar_state=result.radar_state,
                            confidence_hint=result.confidence_hint,
                            setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                            payload_json={"symbol": symbol, "status": "blocked", "decision": "blocked_depth_gate", "reason": _block_reason, "depth": ob_sig.as_dict()},
                        )
                    )
                    db.flush()
                    continue

            # Динамический бюджет применяем к ТРЕНДОВЫМ позициям (не range/scalp/crt —
            # у них свой малый scalp-сайзинг). Кандидат один → весь free; несколько →
            # поровну. None → старый %-сайзинг.
            _pos_margin_cap = dyn_budget if (dyn_alloc and dyn_budget and not is_range) else None
            # (#grade-ml-2026-07-06) Кэп «одинокого кандидата» больше НЕ по грейду
            # (грейд по факту всегда B — пороги A/A+ недостижимы). Базовый план строим
            # на полный бюджет (dyn_budget), а conviction-сайзинг по ml_score применяем
            # ПОСЛЕ ML-оценки (там ml_score уже известен). ML off → откат на grade.
            # См. блок «Conviction sizing» ниже.
            plan = self.trade_plan_builder.build_plan(
                symbol=symbol,
                side=result.action,
                entry_price=entry_price,
                stop_price=stop,
                tp1=tp1,
                tp2=tp2,
                balance_usdt=balance_usdt,
                scalp=is_range,
                position_margin_usdt_cap=_pos_margin_cap,
            )

            if not plan.is_valid:
                # (а) Тихий скип: при динамическом сплите низкая свободная маржа даёт
                # qty ниже биржевого минимума — это «нет места», а не брак сетапа.
                # Не засоряем ленту rejected-событиями и алертами.
                if str(plan.reject_reason or "") == "qty_below_exchange_min_amount":
                    continue
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

            if not is_range and not production_decision.allowed:
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
            if not is_range and not policy_decision["allowed"]:
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

            # Эквити для сайзинга/экспозиции: в LIVE — реальный свободный USDT счёта
            # исполнения (растёт с пополнениями/прибылью); в paper — RISK_EQUITY_USDT.
            try:
                from services.live_executor import LIVE_EXECUTOR
                _equity_usdt = LIVE_EXECUTOR.effective_equity_usdt(settings.execution_market_type)
            except Exception:
                _equity_usdt = float(getattr(settings, "RISK_EQUITY_USDT", balance_usdt))

            exposure_result = self.exposure_guard.check_before_publish(
                db=db,
                bot_id=bot.id,
                symbol=symbol,
                required_margin=float(plan.required_margin or 0),
                equity_usdt=_equity_usdt,
                max_used_margin_pct=float(getattr(settings, "MAX_USED_MARGIN_PCT", 0.85)),
                max_active_signals=int(getattr(settings, "MAX_ACTIVE_SIGNALS", 2)),
                max_active_per_symbol=int(getattr(settings, "MAX_ACTIVE_SIGNALS_PER_SYMBOL", 1)),
                side=result.action,
                max_same_direction_cluster=(
                    int(getattr(settings, "CORR_CLUSTER_MAX_SAME_DIR", 2))
                    if bool(getattr(settings, "CORR_CLUSTER_ENABLED", True)) else 0
                ),
                cluster_symbols=(
                    {s.strip().upper() for s in str(getattr(settings, "CORR_CLUSTER_SYMBOLS", "")).split(",") if s.strip()}
                    or None
                ),
            )

            if bool(getattr(settings, "ANTI_DRAIN_ENABLED", True)):
                anti_cfg = AntiDrainConfig(
                    min_confidence=float(getattr(settings, "ANTI_DRAIN_MIN_CONFIDENCE", 55.0)),
                    min_net_rr_tp1=float(getattr(settings, "SCALP_ANTI_DRAIN_MIN_NET_RR_TP1", 0.40) if is_range else getattr(settings, "ANTI_DRAIN_MIN_NET_RR_TP1", 0.40)),
                    min_net_rr_tp2=float(getattr(settings, "SCALP_ANTI_DRAIN_MIN_NET_RR_TP2", 0.85) if is_range else getattr(settings, "ANTI_DRAIN_MIN_NET_RR_TP2", 0.85)),
                    min_expected_edge_after_costs_usdt=float(getattr(settings, "SCALP_ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 0.0) if is_range else getattr(settings, "ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT", 0.80)),
                    # Динамический бюджет ВЛАДЕЕТ пер-позишн размером: одинокий
                    # кандидат может занять всю free (до 70%), поэтому пер-позишн
                    # кап поднимаем до общего потолка — иначе anti-drain срежет его
                    # как blocked_position_margin_limit. Сумму по-прежнему держит
                    # общий max_used_margin_pct (70%).
                    max_position_margin_pct=(
                        float(getattr(settings, "ANTI_DRAIN_POSITION_MAX_USED_MARGIN_PCT", 70.0))
                        if (dyn_alloc and dyn_budget and not is_range)
                        else float(getattr(settings, "SCALP_ANTI_DRAIN_MAX_POSITION_MARGIN_PCT", 20.0) if is_range else getattr(settings, "ANTI_DRAIN_POSITION_MAX_MARGIN_PCT", 35.0))
                    ),
                    max_used_margin_pct=float(getattr(settings, "ANTI_DRAIN_MAX_USED_MARGIN_PCT", 30.0) if is_range else getattr(settings, "ANTI_DRAIN_POSITION_MAX_USED_MARGIN_PCT", 70.0)),
                    max_open_positions=int(getattr(settings, "ANTI_DRAIN_MAX_OPEN_POSITIONS", 2)),
                    max_active_signals_per_symbol=int(getattr(settings, "ANTI_DRAIN_MAX_ACTIVE_PER_SYMBOL", 1)),
                    max_daily_loss_pct=float(getattr(settings, "ANTI_DRAIN_MAX_DAILY_LOSS_PCT", 3.0)),
                    max_drawdown_pct=float(getattr(settings, "ANTI_DRAIN_MAX_DRAWDOWN_PCT", 12.0)),
                    # POSITION (trend): тренд растянут и перегрет by design, награда
                    # на TP2 → снимаем scalp-эровские блоки. Для range/scalp/crt — как было.
                    block_weak_structure=bool(is_range),
                    # НЕ покупаем перегретую вершину / не шортим перепроданное дно
                    # ни в одном профиле — рынок доказал (6 трендовых лонгов на
                    # RSI 77–88 → −17.5 при развороте). Тренд берём на откатах.
                    block_long_overheated=True,
                    block_short_oversold=True,
                    # Экономику судим по TP2 (runner) для ВСЕХ профилей: TP1 = точка
                    # де-риска, она BY DESIGN < стопа (особенно у скальпа: TP1 0.40 <
                    # стоп 0.65). Раньше is_range судился по TP1 → каждый скальп падал
                    # blocked_bad_trade_economics даже после софта $-флоров. Награда
                    # везде на TP2 → его и судим.
                    economics_use_tp2=True,
                    # (#leak-cost-bleed) TP1-нетто не под водой после издержек.
                    min_net_pnl_tp1_usdt=float(getattr(settings, "ANTI_DRAIN_MIN_NET_PNL_TP1_USDT", 0.0)),
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
                        "net_pnl_tp2": plan.net_pnl_tp2,
                        "net_pnl_stop": plan.net_pnl_stop,
                    },
                    {
                        "equity_usdt": _equity_usdt,
                        "used_margin_usdt": float(exposure_result.used_margin or 0),
                        "daily_pnl_usdt": -abs(float(daily_loss_pct or 0)) * float(balance_usdt) / 100.0,
                        "drawdown_pct": float(drawdown_pct or 0),
                        "open_positions_count": len(open_positions),
                        "active_signals_by_symbol": {symbol: int(exposure_result.active_symbol_signals_count or 0)},
                    },
                    anti_cfg,
                )
                if not anti_allowed:
                    if not self._should_record_block_event(db, symbol, anti_reason):
                        continue
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

            # ── Re-entry cooldown (#churn) ────────────────────────────────────
            # Не открываем ту же сторону того же символа сразу после закрытия
            # (особенно после стопа): закрылись в минус → тут же снова → churn,
            # двойные комиссии, возврат к началу. Машинка ReEntryCooldownGuard
            # уже была, но висела только на /priority — подключаем в авто-цикл.
            if bool(getattr(settings, "REENTRY_COOLDOWN_ENABLED", True)):
                cooldown = ReEntryCooldownGuard().check(
                    db=db,
                    bot_id=bot.id,
                    symbol=symbol,
                    side=result.action,
                    current_priority_score=float(production_decision.payload.get("priority_score", 0) or 0),
                    current_setup_score=float(result.setup_quality.get("final_score", 0) if result.setup_quality else 0),
                    current_rr_tp2=float(plan.net_rr_tp2 or 0),
                )
                if not cooldown.allowed:
                    if not self._should_record_block_event(db, symbol, "reentry_cooldown_active"):
                        continue
                    db.add(
                        IntelligenceEvent(
                            symbol=symbol,
                            status="blocked",
                            decision="reentry_cooldown_active",
                            action=result.action,
                            regime=result.regime,
                            radar_state=result.radar_state,
                            confidence_hint=result.confidence_hint,
                            setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                            payload_json={"symbol": symbol, "status": "blocked", "decision": "reentry_cooldown_active", "reentry_cooldown": cooldown.payload},
                        )
                    )
                    db.flush()
                    continue

            # ── ML-слой (control plane, fail-open, default ML_MODE=off) ───────
            # off → passthrough (ничего). shadow/advisory → только лог ml_score.
            # full_auto → block/масштаб размера в guardrails. Любой сбой → как
            # rule-based (ML не на крит-пути, не мешает live).
            ml_eval = {"mode": "off", "ml_score": None, "action": "passthrough",
                       "allow": True, "size_multiplier": 1.0}
            try:
                _ml_depth = {}
                try:
                    _ml_depth = OrderBookAnalyzer.analyze(
                        ORDERBOOK_STORE.snapshot(symbol),
                        levels=int(getattr(settings, "OB_DEPTH_LEVELS", 10)),
                    ).as_dict()
                except Exception:
                    _ml_depth = {}
                ml_eval = self.ml_controller.evaluate_candidate({
                    "confidence": effective_confidence,
                    "grade": grade,
                    "side": result.action,
                    "regime": result.regime,
                    "net_rr_tp1": plan.net_rr_tp1,
                    "net_rr_tp2": plan.net_rr_tp2,
                    "entry_depth": _ml_depth,
                })
            except Exception:
                pass

            if ml_eval.get("action") == "block" and not ml_eval.get("allow", True):
                # (#ml-explore-2026-07-09) Exploration-квота НА PAPER: каждый N-й
                # заблокированный кандидат открывается микро-пробой ради разметки —
                # иначе датасет пополняется только score>=порога (селекционное
                # смещение) и retrain деградирует. На live — гейт без исключений.
                _explore = False
                if (
                    bool(getattr(settings, "ML_EXPLORE_ENABLED", True))
                    and not settings.is_live_enabled
                ):
                    self._ml_block_seq = int(getattr(self, "_ml_block_seq", 0)) + 1
                    _every_n = max(int(getattr(settings, "ML_EXPLORE_EVERY_N", 3)), 1)
                    _explore = (self._ml_block_seq % _every_n) == 0
                if not _explore:
                    db.add(IntelligenceEvent(
                        symbol=symbol, status="blocked", decision="blocked_by_ml",
                        action=result.action, regime=result.regime, radar_state=result.radar_state,
                        confidence_hint=result.confidence_hint,
                        setup_score=result.setup_quality.get("final_score", 0.0) if result.setup_quality else 0.0,
                        payload_json={"symbol": symbol, "status": "blocked", "decision": "blocked_by_ml", "ml": ml_eval},
                    ))
                    db.flush()
                    continue
                # Проба: пропускаем с микро-размером и явной пометкой в plan_json.ml
                ml_eval = {
                    **ml_eval,
                    "action": "size",
                    "allow": True,
                    "size_multiplier": float(getattr(settings, "ML_EXPLORE_SIZE_MULT", 0.5)),
                    "explore": True,
                    "reason": f"ml_explore_probe:{ml_eval.get('reason')}",
                }
            # ── Conviction sizing: ГРЕЙД × ML (#grade-ml-sync-2026-07-09)
            # Грейд — ПУБЛИЧНАЯ ось уверенности (виден подписчикам в Telegram),
            # поэтому эквити обязано следовать ему, как и было задумано: одинокий
            # A/A+ забирает весь free (dyn_budget уже = free при одном кандидате),
            # несколько кандидатов — поровну; B капается долей free
            # (DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE). Прежний код (#grade-ml-2026-07-06)
            # полностью игнорировал грейд при живом ml_score — B со score 0.46
            # получал столько же эквити, сколько A, и канал врал о размере ставки.
            # После рекалибровки грейдов (#grade-fix-2026-07-06) ладдер A/A+/B
            # реально разлипся — ось снова информативна.
            # ML остаётся ПРИВАТНЫМ модулятором риска: слабый score ужимает размер
            # (в full_auto score<min вообще блокируется выше). Итог =
            # min(grade_mult, ml_mult) — размер задаёт слабейшая ось: A не несёт
            # полный размер против мнения ML, B не получает полный бюджет только
            # за высокий score. ML off/нет score → чистый грейд (fail-open).
            # Только тренд (range/scalp — свой сайзинг). Downward-only (≤1.0).
            _grade_mult = 1.0 if str(grade or "").upper() in ("A", "A+") \
                else float(getattr(settings, "DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE", 0.5))
            _ml_mult = 1.0
            _ml_score = ml_eval.get("ml_score")
            if bool(getattr(settings, "ML_SIZE_ALLOC_ENABLED", True)) and _ml_score is not None:
                _ml_mult = 1.0 if float(_ml_score) >= float(getattr(settings, "ML_SIZE_FULL_MIN_SCORE", 0.45)) \
                    else float(getattr(settings, "ML_SIZE_LOW_MULT", 0.5))
            _conv = min(_grade_mult, _ml_mult) if not is_range else 1.0
            # full_auto guardrails могут дополнительно масштабировать (только вниз здесь)
            if ml_eval.get("action") == "size":
                _m = float(ml_eval.get("size_multiplier", 1.0) or 1.0)
                if _m > 0:
                    _conv *= _m
            _conv = max(0.0, min(_conv, 1.0))
            _sizing_debug = {
                "grade": str(grade or ""),
                "grade_mult": _grade_mult,
                "ml_score": _ml_score,
                "ml_mult": _ml_mult,
                "conviction": round(_conv, 4),
                "dyn_budget_usdt": dyn_budget,
            }
            if abs(_conv - 1.0) > 1e-9:
                plan.qty = round(float(plan.qty) * _conv, 6)
                plan.required_margin = round(float(plan.required_margin) * _conv, 6)
                # (#conv-pnl-rescale-2026-07-11) Долларовая экономика ЛИНЕЙНА по qty
                # (gross и комиссии ∝ нотионалу) — масштабируем вместе с размером.
                # Раньше net_pnl_tp1/tp2/stop оставались от ПОЛНОЙ позиции: #230
                # показывал «TP1: 3.07$» при реальной позиции 0.469× (честно 1.44$),
                # а fallback-пути закрытия могли завысить PnL. RR-поля не трогаем —
                # отношения от масштаба не зависят.
                for _pf in ("net_pnl_tp1", "net_pnl_tp2", "net_pnl_stop"):
                    _pv = getattr(plan, _pf, None)
                    if _pv is not None:
                        setattr(plan, _pf, round(float(_pv) * _conv, 6))

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
                    # ML-слой: ml_score и решение контроллера (shadow/advisory/full_auto).
                    "ml": ml_eval,
                    # (#grade-ml-sync-2026-07-09) Прозрачность сайзинга: грейд-ось ×
                    # ML-ось × бюджет цикла → почему у сделки такой размер.
                    "sizing": _sizing_debug,
                    # Режим сделки для exit-политики: trend → ride (едем движение),
                    # scalp → быстрый выход. Range-вход (Phase 2) проставит "scalp".
                    "trade_mode": "scalp" if str(result.regime or "") in ("range", "scalp") else "trend",
                    # Контекст для ML-датасета (фичи на момент входа).
                    "regime": str(result.regime or ""),
                    "radar_state": str(getattr(result, "radar_state", "") or ""),
                    "entry_depth": (
                        OrderBookAnalyzer.analyze(
                            ORDERBOOK_STORE.snapshot(symbol),
                            levels=int(getattr(settings, "OB_DEPTH_LEVELS", 10)),
                        ).as_dict()
                        if bool(getattr(settings, "ENABLE_ORDERBOOK_ENGINE", False))
                        else None
                    ),
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

    # ── Динамическое распределение маржи по кандидатам цикла ──────────────────
    def _ready_candidate_check(self, db, bot, symbol, result, balance_usdt) -> bool:
        """Size-INDEPENDENT предикат: прошёл бы кандидат гейты (без маржевых).
        Без side-effects — только для ПОДСЧЁТА конкурентов за маржу. Маржевые
        гейты (exposure/anti-drain) опускаем: их удовлетворяет сам дележ. Лучше
        чуть пере-считать (консервативно — меньше бюджет на сделку), чем недосчитать.
        Любая ошибка → не считаем (fail-safe)."""
        try:
            if result is None or result.action == "hold" or result.setup_decision != "approve":
                return False
            is_range = str(getattr(result, "regime", "")) in ("range", "crt", "scalp")
            if result.action == "short" and not settings.ALLOW_SHORTS and not is_range:
                return False
            if bool(getattr(settings, "ENABLE_ORDERBOOK_ENGINE", False)) and bool(getattr(settings, "OB_GATE_ENTRIES", True)):
                ob_snap = ORDERBOOK_STORE.snapshot(symbol)
                if ob_snap and ob_snap.get("age_sec", 1e9) > float(getattr(settings, "OB_DATA_MAX_AGE_SEC", 15.0)):
                    ob_snap = None
                ob_sig = OrderBookAnalyzer.analyze(ob_snap, levels=int(getattr(settings, "OB_DEPTH_LEVELS", 10)))
                _is_position = str(result.regime or "").lower() not in ("range", "scalp")
                _max_spread = float(getattr(settings, "OB_POSITION_MAX_SPREAD_PCT" if _is_position else "OB_MAX_SPREAD_PCT", 0.20 if _is_position else 0.08))
                ob_ok, _ = OrderBookAnalyzer.entry_gate(
                    result.action, ob_sig, max_spread_pct=_max_spread,
                    obi_confirm=float(getattr(settings, "OB_OBI_CONFIRM", 0.15)),
                    wall_confirm=float(getattr(settings, "OB_WALL_CONFIRM_SHARE", 0.30)),
                    cvd_block_ratio=float(getattr(settings, "OB_CVD_ENTRY_BLOCK_RATIO", 0.6)),
                    cvd_min_trades=int(getattr(settings, "OB_CVD_MIN_TRADES", 25)),
                    obi_hard_veto=float(getattr(settings, "OB_OBI_HARD_VETO", 0.75)),
                    wall_rescue_max_adverse_obi=float(getattr(settings, "OB_WALL_RESCUE_MAX_ADVERSE_OBI", 0.35)),
                    cvd_thin_ratio=float(getattr(settings, "OB_CVD_THIN_RATIO", 0.9)),
                    cvd_thin_min_trades=int(getattr(settings, "OB_CVD_THIN_MIN_TRADES", 1)),
                )
                if not ob_ok:
                    return False
                # LiquidityGuard: адаптивный спред-блок (свип-риск тонкой ликвидности).
                try:
                    from services.liquidity_guard import LIQUIDITY_GUARD
                    _sp_bps = (ob_sig.spread_pct * 100.0) if ob_sig and ob_sig.spread_pct is not None else None
                    if LIQUIDITY_GUARD.entry_blocked(symbol, sp_bps=_sp_bps)[0]:
                        return False
                except Exception:
                    pass
            entry_from = float(result.entry_zone[0]); entry_to = float(result.entry_zone[1])
            entry_mid = (entry_from + entry_to) / 2.0
            try:
                entry_price = float(self.trade_plan_builder.htx.price_to_precision(symbol, entry_mid))
            except Exception:
                entry_price = entry_mid
            stop = float(result.stop_price); tp1 = float(result.tp["tp1"]); tp2 = float(result.tp["tp2"])
            plan = self.trade_plan_builder.build_plan(
                symbol=symbol, side=result.action, entry_price=entry_price,
                stop_price=stop, tp1=tp1, tp2=tp2, balance_usdt=balance_usdt, scalp=is_range,
            )
            if not plan.is_valid:
                return False
            if not is_range:
                sq = result.setup_quality if isinstance(result.setup_quality, dict) else {}
                eff = self._intelligence_effective_confidence(result)
                grade = self.quality.grade(
                    confidence=result.confidence_hint, rationale=f"intelligence_{result.reason}",
                    regime=result.regime, setup_score=sq.get("final_score"), effective_confidence=eff,
                )
                if not self.quality.should_publish_to_clients(
                    grade=grade, setup_score=sq.get("final_score"), effective_confidence=eff,
                    setup_decision=result.setup_decision, setup_quality=sq,
                ):
                    return False
                pd = self.production_gate.check(
                    grade=grade, setup_score=sq.get("final_score"), effective_confidence=eff,
                    net_rr_tp1=plan.net_rr_tp1, net_rr_tp2=plan.net_rr_tp2, priority_score=100.0,
                )
                if not pd.allowed:
                    return False
            return True
        except Exception:
            return False

    def _compute_dynamic_budget(self, db, bot, balance_usdt):
        """Пред-проход: кэшируем анализ символов, считаем готовых кандидатов и
        делим свободную маржу. Возвращает (budget_usdt|None, analyses_cache, ready, free)."""
        analyses: dict = {}
        ready = 0
        for symbol in bot.config_json.get("symbols", []):
            try:
                result = self.intelligence.analyze_symbol(symbol)
            except Exception:
                result = None
            analyses[symbol] = result
            if result is not None and self._ready_candidate_check(db, bot, symbol, result, balance_usdt):
                ready += 1
        try:
            from services.live_executor import LIVE_EXECUTOR
            equity = LIVE_EXECUTOR.effective_equity_usdt(settings.execution_market_type)
        except Exception:
            equity = float(getattr(settings, "RISK_EQUITY_USDT", balance_usdt))
        used_pct = float(getattr(settings, "ANTI_DRAIN_POSITION_MAX_USED_MARGIN_PCT", 70.0))
        ceiling = equity * used_pct / 100.0
        try:
            used = float(self.exposure_guard.used_margin(db, bot.id) or 0.0)
        except Exception:
            used = 0.0
        free = max(0.0, ceiling - used)
        from services.margin_allocator import per_trade_margin
        cap = float(getattr(settings, "DYNAMIC_MARGIN_CAP_PCT_OF_FREE", 1.0))
        budget = per_trade_margin(free, ready, cap_pct_of_free=cap) if ready > 0 else None
        return budget, analyses, ready, free

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

    def _should_record_block_event(self, db: Session, symbol: str, decision: str) -> bool:
        """(#audit-event-spam) Дедуп повторяющихся blocked-событий: одно и то же
        (symbol, decision) не пишем чаще INTEL_EVENT_DEDUP_MINUTES. До фикса
        blocked_post_loss_cooldown/blocked_depth_gate писались каждый тик (~70с)
        весь период блокировки — 34.7k строк в intelligence_events."""
        minutes = float(getattr(settings, "INTEL_EVENT_DEDUP_MINUTES", 10.0))
        if minutes <= 0:
            return True
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        recent = (
            db.query(IntelligenceEvent.id)
            .filter(
                IntelligenceEvent.symbol == symbol,
                IntelligenceEvent.decision == decision,
                IntelligenceEvent.created_at >= since,
            )
            .first()
        )
        return recent is None

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