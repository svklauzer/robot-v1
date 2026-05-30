# Полный аудит Robot V1 и дорожная карта рефакторинга к прибыли, live market и Telegram-подпискам

Дата аудита: 28 мая 2026 UTC.
Основание: статический аудит репозитория, текущая архитектура `api + web + db + redis`, существующая roadmap-документация и последний предоставленный тестовый срез `run_20260528T133826Z`.

> Важно: документ не является финансовой рекомендацией и не гарантирует прибыль. Его цель — превратить текущую систему из исследовательского/paper-прототипа в управляемый продукт с измеримым edge, платным доступом и безопасным go-live процессом.

## 1. Executive summary

Система уже имеет рабочий каркас торгового продукта: FastAPI backend, Next.js owner UI, Postgres/Redis, торговый цикл, signal lifecycle, analytics endpoints, Telegram routing, список подписчиков и watchdog истечений. Но по состоянию на текущий срез ее нельзя выпускать на живой рынок с обещанием прибыли и нельзя масштабировать продажи без завершения платежного и Telegram UX контура.

Главный вывод: **сначала нужно стабилизировать экономику сигнала и замкнуть продуктовую воронку, затем ограниченно выходить в live с малыми лимитами, и только после доказанного edge включать агрессивную монетизацию**.

### Критические факты из последнего среза

- `total_signals=39`, `closed_signals=36`, `expired_signals=3`, `active_signals=0`.
- `wins=11`, `losses=25`, `winrate=30.56%`.
- `total_net_pnl_usdt=-24.159891`, `avg_net_pnl_usdt=-0.671108`, `total_costs_usdt=22.053502`.
- Главный источник убытка: `failed_setup_exit` — 25 из 36 закрытых сделок, `69.44%`, суммарно `-25.05804 USDT`.
- Позитивное движение было у 28 из 36 сделок, но `positive_then_negative=17`, то есть `47.22%` закрытых сигналов уходили из плюса в минус.
- `avg_missed_profit_pct=0.793`, `tp2_rate=0.0%`, `trailing_rate=2.78%` — система часто видит движение, но не монетизирует его.
- `ml_outcomes_summary` в свежем API-срезе деградировал с причиной `ml_summary_python_failed`; значит ML-аналитика не может считаться надежным production-контролем.
- В API логах есть `TELEGRAM SEND ERROR ... ConnectTimeout`; отправка Telegram сейчас не имеет полноценной очереди, retry/backoff и доставочного SLA.

## 2. Текущее состояние системы as-is

### 2.1 Инфраструктура

- Docker Compose поднимает `db`, `redis`, `api`, `web`.
- Postgres и Redis проброшены наружу, что удобно для разработки, но требует hardening перед production.
- API использует `.env`; web получает `NEXT_PUBLIC_API_URL`.
- В репозитории нет полноценного `.gitignore` для `node_modules`, `.next`, кешей и локальных артефактов; в рабочем дереве уже появился `apps/web/node_modules/` как untracked мусор.

### 2.2 Backend/API

- В `apps/api/main.py` сосредоточено слишком много ответственности: bootstrap, lifecycle app, фоновые циклы, bot endpoints, signals, analytics, subscribers, health, debug/force endpoints и экспериментальные маршруты.
- Фоновые циклы запускаются в lifespan API-процесса:
  - `background_robot_loop` каждые 60 секунд;
  - `background_subscription_loop` раз в 6 часов.
- Есть отдельные сервисы для trade plan, cost, lifecycle, exit policy, quality, market intelligence, production gate, exposure guard, symbol performance guard.
- Часть роутеров в `apps/api/routers/*` существует, но фактические endpoints в основном живут в `main.py`; это признак незавершенного разделения слоев.

### 2.3 Trading/intelligence контур

Положительные стороны:

- Есть `TradePlanBuilder` с расчетом риска, qty, биржевых precision/limits, net PnL и RR после costs.
- Есть `CostEngine`, `ExposureGuard`, `ProductionEntryGate`, `AntiDrainGuard`, `SymbolPerformanceGuard`.
- Есть lifecycle сопровождение и exit policy с защитными выходами.
- Есть тесты для trade plan, exit policy, signal quality и symbol performance guard.

Проблемы:

- Текущий PnL отрицательный, а корневая причина повторяется: `failed_setup_exit` доминирует в убытках.
- Много сигналов имеют MFE, но не фиксируют прибыль: проблема не только входов, но и exit/partial/trailing логики.
- `tp2_rate=0.0%` означает, что текущие цели либо недостижимы для выбранного таймфрейма/волатильности, либо exit policy забирает сделки раньше, либо план уровней не соответствует фактическому движению.
- Риски live execution пока не закрыты: нет полноценного order reconciliation, idempotency, circuit breaker, exchange outage handling, dry-run/live parity report.
- Настройки в `core/config.py` частично дублируются (`MIN_NET_PNL_TP1_USDT`, `MIN_NET_PNL_TP2_USDT` объявлены дважды), что повышает риск неверной конфигурации.

### 2.4 Telegram и подписки

Что уже есть:

- `TelegramRouter` разделяет VIP full signal и FREE teaser/update.
- `SignalBroadcaster` умеет отправлять сообщения через Telegram Bot API.
- Есть модель `Subscriber` со статусом, планом, trial-флагом и датой окончания.
- Есть CRUD-подобные endpoints `/subscribers`, `/subscribers/{id}/extend`, `/subscribers/{id}/status`, `/subscribers/check-expirations`.
- В web есть страница клиентов с добавлением, продлением, блокировкой и фильтрами.

Критические gap'ы:

- Нет Telegram webhook/polling обработчика входящих сообщений пользователя.
- Нет команд `/start`, `/menu`, `/status`, `/plans`, `/pay`, `/help`, `/support`.
- Нет inline keyboard/callback меню.
- Нет payment provider, invoices, payment webhooks, статуса оплаты, idempotency payment events.
- Нет автоматической выдачи/отзыва доступа в VIP-канал после оплаты/истечения.
- Watchdog уведомляет owner, но не управляет клиентским жизненным циклом полностью.
- Telegram send failure сейчас логируется print'ом; нет очереди доставок, retry, dead-letter, метрик доставляемости.

### 2.5 Web/owner UI

Положительные стороны:

- Есть операционные страницы: dashboard, analytics, reports, signals, positions, health, clients.
- Страница клиентов уже закрывает ручной MVP управления подписками.

Проблемы:

- UI — owner-first, не customer-facing.
- Нет платежной панели, invoices, revenue metrics, MRR, churn, cohort, conversion funnel.
- Нет строгого auth guard на уровне API/UI для всех owner endpoints.
- Нет production build validation в CI.
- Next dev лог из среза показал автоправку TypeScript include; нужно зафиксировать стабильную конфигурацию и запретить runtime-mutating surprises.

### 2.6 ML/outcomes/analytics

- Есть JSONL outcomes storage и `ml_outcome_stats`.
- Последний локальный `latest_report_for_chat.md` показывает 90 closed rows, winrate `45.56%`, net PnL `-96.208143`, top reason `failed_setup_exit=47`.
- Свежий API-срез показал degraded summary из-за `ml_summary_python_failed`.
- Нужна единая truth-source витрина: paper/live, gross/net, costs, slippage, fees, funding, latency, symbol, setup type, entry/exit reason, MFE/MAE, missed profit.

## 3. Диагноз прибыльности

Текущая проблема не выглядит как «бот вообще не видит рынок». Напротив, `went_positive=28/36` показывает, что рынок часто дает движение в сторону сигнала. Проблема в трех местах:

1. **Качество входа и режим рынка** — слишком много сетапов быстро признаются failed setup.
2. **Экономика сделки после costs** — комиссии/издержки съедают маленькие защитные плюсы; `protective_breakeven_profit_guard` дает win count, но почти не покрывает минусы.
3. **Exit capture** — система упускает MFE и почти не доводит до TP2/trailing; текущий protective guard часто превращает потенциально хорошие сделки в микроплюс.

### Profit-first метрики, которые должны стать release gates

Перед live и продажами фиксируем минимальные критерии:

- Paper rolling window: не меньше 200 закрытых сигналов или 30 календарных дней.
- Net PnL after costs: положительный минимум 14 из последних 21 дней или positive expectancy на выборке.
- Max daily loss: не выше заранее заданного лимита, hard stop в коде.
- `failed_setup_exit` share: ниже 35% закрытых сигналов.
- `positive_then_negative_rate`: ниже 25%.
- `tp2_rate + meaningful trailing_rate`: не ниже 15%.
- Средний net win должен быть минимум в 1.3 раза выше среднего net loss или winrate должен компенсировать payout ratio.
- Все Telegram VIP deliveries: SLA не ниже 99% с retry; недоставленный сигнал не считается активным клиентским сигналом.

## 4. Целевая архитектура refactor-to-profit

### 4.1 Разделение доменов backend

Разнести `apps/api/main.py` на модули:

```text
apps/api/
  app.py / main.py                 # только создание FastAPI, middleware, lifespan
  routers/
    bot.py
    signals.py
    positions.py
    analytics.py
    subscribers.py
    payments.py
    telegram_webhook.py
    system.py
  services/
    trading/
    telegram/
    billing/
    analytics/
    risk/
  workers/
    robot_loop.py
    subscription_loop.py
    telegram_delivery_worker.py
    payment_reconciliation_worker.py
  models/
    subscriber.py
    payment.py
    telegram_delivery.py
    signal.py
```

Цель: убрать god-file, сделать тестируемые сервисы, отделить trading от billing и Telegram UX.

### 4.2 Событийная модель

Ввести таблицы/очереди:

- `telegram_deliveries`: сообщение, chat_id, type, status, attempts, last_error, next_retry_at.
- `payment_events`: provider, provider_event_id, user_id, amount, currency, status, raw_payload, processed_at.
- `subscriptions`: можно оставить `subscribers`, но добавить payment linkage, plan_id, source, auto_renew, cancel_reason.
- `audit_events`: admin actions, bot start/stop, config changes, manual subscriber changes.
- `trade_runs` / `signal_decisions`: все rejected/approved с причинами и score snapshot.

### 4.3 Live market safety envelope

Для выхода на live нужен отдельный режим `live_shadow` и затем `live_limited`:

- `paper_signal` — текущая публикация/наблюдение.
- `live_shadow` — отправляем ордера в симулятор с реальными bid/ask/slippage snapshots.
- `live_limited` — реальные ордера на малый капитал, hard caps.
- `live_scaled` — масштабирование только после подтвержденных метрик.

Hard controls:

- One-click kill switch и API endpoint stop с audit log.
- Daily loss circuit breaker.
- Exchange connectivity breaker.
- Telegram delivery breaker: если VIP publish не доставлен, сигнал не активируется.
- Position/order reconciliation каждые N секунд.
- Idempotent client order id.
- Запрет live, если config не прошел validation.

## 5. Roadmap по фазам

## Фаза 0 — 48 часов: Freeze, hygiene, контрольная база

Цель: остановить расползание хаоса и получить воспроизводимую baseline-картину.

**Работы:**

1. Зафиксировать `.gitignore`: `node_modules`, `.next`, кеши, локальные analytics run folders при необходимости.
2. Описать env profiles: `dev`, `paper`, `live_shadow`, `live_limited`, `production`.
3. Ввести release checklist: tests, docker compose health, API smoke, Telegram test, analytics summary.
4. Разделить «экспериментальные force/debug endpoints» и production endpoints; закрыть их owner-auth или feature flag.
5. Зафиксировать текущие метрики из `run_20260528T133826Z` как baseline.

**Exit criteria:**

- `pytest` проходит.
- `next build` проходит.
- Owner health показывает API/db/market/telegram/subscribers.
- Новый analytics report генерируется без `ml_summary_python_failed`.

## Фаза 1 — Неделя 1: Profit instrumentation и analytics truth-source

Цель: понять, где именно теряются деньги, до изменения стратегии.

**Работы:**

1. Единая таблица/витрина outcomes:
   - symbol, side, timeframe, setup reason, grade, confidence;
   - gross PnL, fees, slippage, funding, net PnL;
   - MFE/MAE, missed profit, time in trade;
   - entry reason, exit reason, lifecycle states.
2. Добавить `decision_events` для всех rejected/approved кандидатов.
3. Сделать report `failed_setup_exit root cause`:
   - по символам;
   - по side;
   - по grade;
   - по volatility regime;
   - по времени суток;
   - по RR bucket.
4. Добавить dashboard widgets:
   - expectancy;
   - payout ratio;
   - costs share;
   - missed profit;
   - failed setup share;
   - delivery SLA.
5. Исправить `ml_summary_python_failed`: тестируемый CLI/endpoint с fallback и ошибкой в health.

**Exit criteria:**

- Любая закрытая сделка объяснима через `entry_decision -> lifecycle -> exit_decision -> net outcome`.
- Owner видит top-5 источников убытка за сутки/неделю.
- Нет degraded ML summary в штатном отчете.

## Фаза 2 — Неделя 1-2: Trading edge stabilization

Цель: перестать терять на повторяемых failed setup и начать забирать MFE.

**Работы:**

1. Ужесточить entry gates для символов с отрицательной статистикой:
   - временно заблокировать худшие symbols из report: SOL, AVAX, ETH, XRP, TON, LINK, DOT при продолжении отрицательного expectancy;
   - включить cooldown по `failed_setup_exit` не только streak, но и rolling net PnL.
2. Пересобрать exit policy:
   - partial take profit при MFE `0.30-0.50%`, если net after costs положительный;
   - adaptive trailing не позже после MFE `0.60-0.80%`;
   - запрет микроплюса, если он статистически не покрывает средний failed setup loss;
   - отдельная логика для scalp/trend regimes.
3. Пересчитать TP уровни под фактическую волатильность:
   - TP1 должен быть достижимым с учетом ATR и fees;
   - TP2 не должен быть фиктивной целью, если `tp2_rate=0%`.
4. Добавить per-symbol policy profiles:
   - `tradeable`, `watch_only`, `blocked`;
   - min confidence/RR per symbol;
   - side restrictions.
5. Backtest/replay на JSONL outcomes и новых правилах.

**Exit criteria:**

- `failed_setup_exit` ниже 35% на rolling sample.
- `positive_then_negative_rate` ниже 25%.
- Net PnL rolling window положительный после costs.
- Документирован go/no-go report по каждому символу.

## Фаза 3 — Неделя 2: Telegram delivery reliability

Цель: VIP-сигнал должен доставляться надежно; иначе продукт нельзя продавать.

**Работы:**

1. Вынести отправку из прямого `httpx.post` в очередь `telegram_deliveries`.
2. Добавить retry/backoff, dead-letter, last_error, attempts.
3. Ввести статусы:
   - `queued`, `sent`, `failed_retryable`, `failed_final`.
4. Для VIP full signal сделать transactional flow:
   - signal created as `queued`;
   - delivery success -> `published`;
   - delivery final fail -> `telegram_failed` и owner alert.
5. Добавить health metric `telegram_delivery_sla_24h`.
6. Добавить тесты на required/optional delivery behavior.

**Exit criteria:**

- VIP delivery failure не теряется print'ом.
- Любой Telegram timeout виден в owner UI.
- Повторная отправка идемпотентна.

## Фаза 4 — Неделя 2-3: Telegram bot menu и customer UX

Цель: пользователь сам проходит путь от `/start` до оплаты/статуса/поддержки.

**Работы:**

1. Добавить Telegram webhook endpoint `/telegram/webhook`.
2. Поддержать commands:
   - `/start` — welcome + value proposition + кнопки;
   - `/menu` — главное меню;
   - `/plans` — тарифы;
   - `/pay` — создание invoice/payment link;
   - `/status` — статус подписки и дата окончания;
   - `/help` — FAQ и disclaimer;
   - `/support` — контакт поддержки.
3. Inline keyboard сценарии:
   - `trial_start`;
   - `buy_vip_30d`;
   - `buy_vip_90d`;
   - `renew`;
   - `faq_risks`;
   - `contact_support`.
4. Хранить Telegram user profile при первом контакте.
5. Добавить customer messages templates:
   - trial activated;
   - payment pending;
   - payment success;
   - expires in 3 days/1 day;
   - expired;
   - access revoked.

**Exit criteria:**

- Новый пользователь может без owner-ручного действия узнать тариф, получить ссылку на оплату и проверить статус.
- Owner UI показывает источник пользователя и funnel stage.

## Фаза 5 — Неделя 3: Payments и подписки end-to-end

Цель: подписка активируется оплатой, а не ручным добавлением.

**Работы:**

1. Выбрать payment provider под юрисдикцию и аудит:
   - для MVP можно начать с crypto/manual confirmed, но production лучше делать через provider с webhook;
   - любые manual payments должны иметь audit trail.
2. Добавить модели:
   - `Plan`: code, price, duration_days, is_active;
   - `Payment`: user/subscriber, provider, amount, currency, status;
   - `PaymentEvent`: idempotent webhook events.
3. Endpoints:
   - `POST /payments/checkout`;
   - `POST /payments/webhook/{provider}`;
   - `GET /payments` owner;
   - `POST /payments/{id}/manual-confirm` owner-only.
4. Subscription activation flow:
   - payment success -> subscriber active/extended;
   - failed/canceled -> no access;
   - refund/chargeback -> suspend/revoke.
5. VIP access automation:
   - generate invite link or approve join request;
   - revoke/ban on expiry if policy allows.
6. Revenue dashboard:
   - MRR;
   - cash collected;
   - active paid subscribers;
   - trials;
   - conversion trial-to-paid;
   - churn.

**Exit criteria:**

- Оплата автоматически продлевает подписку.
- Повтор webhook не продлевает дважды.
- Owner видит revenue и ошибки платежей.

## Фаза 6 — Неделя 3-4: Refactor API structure и security hardening

Цель: подготовить код к production maintenance.

**Работы:**

1. Разбить `main.py` на routers.
2. Включить auth dependencies для owner endpoints.
3. Убрать debug/force endpoints из production или закрыть `APP_ENV != production`.
4. Добавить Alembic migrations вместо `Base.metadata.create_all` как production path.
5. Привести config к single source of truth:
   - удалить дубли;
   - добавить typed profiles;
   - добавить config validation at startup.
6. Перевести `print`-логи на structured logging.
7. Добавить request ids и audit logs.

**Exit criteria:**

- Production API не содержит открытых force/debug операций.
- Все owner actions авторизованы и аудируются.
- Миграции воспроизводимо поднимают схему.

## Фаза 7 — Неделя 4-5: Live shadow и limited live

Цель: проверить соответствие paper/live без риска большого капитала.

**Live shadow:**

- Реальные bid/ask snapshots.
- Симуляция исполнения по worst-case slippage.
- Сравнение planned vs executable levels.
- Отчет drift между paper и live-shadow.

**Limited live:**

- Только top symbols с положительным expectancy.
- Риск на сделку: минимальный, например 0.05-0.10% equity.
- Max open positions: 1.
- Max daily loss: жестко 0.5-1.0%.
- Trading hours/regime filters.
- Owner manual approval на первые N сделок.

**Exit criteria:**

- 50-100 live_shadow сделок без критических расхождений.
- 20-30 limited_live сделок без execution incidents.
- Live net after costs не хуже paper на заранее допустимый drift.

## Фаза 8 — Неделя 5-6: Go-to-market и монетизация

Цель: продавать не «гарантированную прибыль», а прозрачный сервис сигналов с контролируемой статистикой и рисками.

**Работы:**

1. Product packaging:
   - FREE: teaser + delayed updates + education;
   - VIP: full levels + lifecycle updates + daily report;
   - PRO/Owner later: dashboard/advanced analytics.
2. Legal/marketing:
   - risk disclaimer;
   - no guaranteed returns;
   - terms of service;
   - refund policy;
   - privacy policy.
3. Funnel:
   - Telegram free channel -> bot `/start` -> trial -> payment -> VIP invite.
   - HTX affiliate hook: пользователь переходит по `HTX_AFFILIATE_LINK`, регистрируется в HTX и получает бесплатный VIP на `AFFILIATE_FREE_VIP_DAYS` дней с invite из `VIP_INVITE_LINK` (MVP: self-claim в боте, later: verification webhook/provider report).
4. Retention:
   - daily/weekly transparent reports;
   - explain closed trades;
   - show risk management;
   - renewal reminders.
5. Pricing tests:
   - monthly VIP;
   - quarterly discount;
   - limited founder price.

**Exit criteria:**

- Клиентский путь полностью автоматизирован.
- Owner видит funnel/revenue metrics.
- Торговая статистика не противоречит публичному positioning.

## 6. Приоритетный backlog

### P0 — нельзя идти в live/продажи без этого

1. Исправить analytics/ML degraded report.
2. Добавить Telegram delivery queue + retry + status.
3. Ввести payment/subscription domain models.
4. Добавить Telegram webhook/menu.
5. Закрыть force/debug endpoints и owner operations auth.
6. Снизить `failed_setup_exit` и `positive_then_negative` через gates/exit tuning.
7. Ввести live kill switch и daily loss circuit breaker.

### P1 — нужно для устойчивой монетизации

1. Revenue dashboard.
2. VIP access automation.
3. HTX affiliate free-VIP funnel: `/htx` в Telegram, tracking stage, trial activation на 30 дней, анти-дублирование и последующая верификация регистрации.
3. Payment webhook idempotency.
4. Payment reconciliation worker: auto-expire stale pending checkouts, audit event, `/payments/reconcile`.
5. Per-symbol profitability guard + owner report (`/analytics/symbol-performance`) для block/reduce/ok решений по каждому символу.
5. MFE capture analytics and adaptive exit experiments.
6. Structured logs + health checks.
7. Alembic migrations for new billing/telegram tables.

### P2 — масштабирование

1. A/B тарифы, trial duration и affiliate free-VIP офферы.
2. Referral codes.
3. Cohort analytics.
4. Multi-provider payments.
5. Multi-exchange architecture.
6. Backtest/replay framework.
7. Customer web portal.

## 7. Рекомендуемый порядок рефакторинга файлов

1. `apps/api/main.py` -> routers:
   - `routers/subscribers.py`, `routers/system.py`, `routers/bot.py`, `routers/signals.py`, `routers/analytics.py`, `routers/debug.py`.
2. Новый billing domain:
   - `models/payment.py`, `models/plan.py`, `services/billing/*`, `routers/payments.py`.
3. Новый Telegram UX domain:
   - `models/telegram_delivery.py`, `services/telegram/*`, `routers/telegram_webhook.py`, `workers/telegram_delivery_worker.py`.
4. Trading safety:
   - `services/execution_engine.py`, `services/recovery_engine.py`, `services/exposure_guard.py`, `services/signal_lifecycle.py`, `services/exit_policy.py`.
5. Analytics truth-source:
   - `services/analytics_service.py`, `services/ml_outcome_stats.py`, `models/analytics.py`.
6. Web UI:
   - `apps/web/app/clients/page.tsx` -> add payments/revenue/funnel;
   - `apps/web/app/health/page.tsx` -> delivery/payment/live safety metrics;
   - new `apps/web/app/payments/page.tsx`.

## 8. Acceptance checklist перед публичным запуском

### Trading

- [ ] Rolling net PnL positive after all costs.
- [ ] `failed_setup_exit < 35%`.
- [ ] `positive_then_negative_rate < 25%`.
- [ ] Per-symbol profitability guard виден owner-у и блокирует/снижает риск по убыточным символам.
- [ ] At least 200 closed paper/live_shadow outcomes.
- [ ] Live kill switch tested.
- [ ] Exchange reconnect/reconciliation tested.

### Telegram

- [ ] `/start`, `/menu`, `/plans`, `/pay`, `/status`, `/help`, `/support` implemented.
- [ ] VIP signal delivery queue with retries.
- [ ] Delivery SLA dashboard.
- [ ] Undelivered VIP full signal does not become active.

### Payments/subscriptions

- [ ] Checkout creates payment.
- [ ] Stale pending checkout expires automatically and is audit-visible.
- [ ] Webhook is idempotent.
- [ ] Success activates/extends subscription.
- [ ] Expiry revokes or flags VIP access.
- [ ] Revenue dashboard exists.
- [ ] HTX affiliate funnel tracked: link click -> registration claim/verification -> 30d VIP -> paid conversion.

### Security/ops

- [ ] Owner endpoints authenticated.
- [ ] Debug endpoints disabled in production.
- [ ] Secrets not logged.
- [ ] Alembic migrations in place.
- [ ] Docker production profile documented.
- [ ] Backup/restore tested.

## 9. Финальная рекомендация

Не запускать живой рынок и платный VIP как «прибыльный продукт» прямо сейчас. Запустить можно только контролируемую beta: FREE/closed VIP с честным статусом paper/live-shadow, сбором статистики и ручной модерацией. Основной фокус ближайших 2-3 недель — **снизить системный drain, автоматизировать Telegram/payment funnel и обеспечить доставку сигналов**. После прохождения profit gates можно переходить к limited live и платному масштабированию.
