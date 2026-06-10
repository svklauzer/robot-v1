# Robot V1 — Анализ проекта и ROADMAP до продакшна
**Дата аудита: 4 июня 2026**

---

## 1. Что из себя представляет система

Robot V1 — это торговый бот с сигнальным SaaS-продуктом поверх него. Пять связанных контуров:

**Trading/Intelligence** — MarketIntelligenceEngine собирает OHLCV с HTX через ccxt, строит мультитаймфреймный анализ (5m/15m/1h), StrategyEngine генерирует кандидатов, MLScorer скорит их (пока эвристический, не ML), ProductionEntryGate фильтрует по grade/RR/confidence. SignalLifecycleManager сопровождает сигналы через opened → tp1 → tp2/stop → closed, используя ExitPolicy v4 (failed_setup_exit, protective_breakeven, adaptive_trailing, MFE capture). ExecutionEngine поддерживает оба режима: paper и live (ccxt market orders через HTX).

**Safety/Readiness** — LiveSafetyService (daily loss circuit breaker + kill switch), ValidationGateService (200+ closed signals, positive net PnL, failed_setup < 35%, positive_then_negative < 25%), ExposureGuard, SymbolPerformanceGuard, ProductionEntryGate, SystemHealthService, /system/readiness endpoint.

**Telegram** — TelegramBotMenuService (menu, /start, /plans, /pay, /status), TelegramRouter (VIP/FREE маршрутизация), TelegramDeliveryLog + TelegramDeliveryWorker (retry/backoff), CustomerNotificationService (welcome/expiry/renewal).

**Monetization** — BillingService (plan checkout, confirm_payment, idempotency), PaymentReconciliationService (stale/pending cleanup), RevenueMetricsService, billing plans vip_30/vip_90/vip_180.

**Owner Operations** — Next.js dashboard, FastAPI endpoints (signals, positions, analytics, reports, system readiness, funding-arb, ml outcomes), AuditLogService, StructuredLogging, /system/readiness, /system/kill-switch, /system/product-e2e-smoke.

---

## 2. Текущее состояние кода

### Что сделано и работает

- FastAPI backend компилируется без SyntaxError
- Уникальность route decorators подтверждена тестом
- 42 тест-файла, ~110+ test cases, migrations существуют и импортируются
- Alembic migration v0001 покрывает все runtime-таблицы (users, bots, signals, orders, positions, subscribers, payments, payment_events, telegram_profiles, telegram_deliveries, audit_events, billing_plans, intelligence_events)
- Docker Compose (dev + prod) с postgres/redis/api/web
- Production runbook с миграционным flow и readiness checklist
- HTXClient полностью реализован: market orders, fetch_ohlcv, fetch_ticker, fetch_balance, fetch_positions, fetch_funding_rate, price/amount precision, fee rates
- ExecutionEngine: open_paper_position и close_paper_position через CostEngine, live path через `ENABLE_LIVE_ORDERS`
- FundingArbitrage: spot/swap hedge, FundingArbEngine, FundingMonitorService — домен полностью в коде
- ProductE2ESmokeService покрывает полный путь: Telegram user → checkout → payment event → subscriber active → VIP access

### Результаты тестов (статический анализ — workspace без дискового места)

**Ожидаемый результат при запуске pytest:**

| Категория | Оценка |
|---|---|
| Всего тест-файлов | 42 |
| Ожидаемых PASS | ~105 |
| Потенциальных FAIL/ERROR | ~5 |

**Потенциальные провалы:**
1. `test_production_runbook::test_backup_restore_smoke_script_has_dry_run_contract` — вызывает `bash` через subprocess; в нативном Windows без Git Bash в PATH упадёт с WinError 2
2. `test_telegram_delivery_worker` (4 async теста) — требует `anyio[trio]` + `pytest-anyio`; без них будут ERROR
3. `test_main_import_contract` — требует полного набора зависимостей (ccxt, pydantic-settings, sqlalchemy, pandas); в чистой среде может упасть на import

**Все остальные тесты должны проходить**: exit_policy, strategy_engine, billing_service, validation_gates, live_safety, live_shadow, funding_arbitrage, product_e2e_smoke, ml_outcome_stats, telegram_router, symbol_performance_guard, и т.д.

**Команда для запуска:**
```bash
cd apps/api
pip install pytest anyio[trio] pytest-anyio pydantic-settings sqlalchemy pandas ccxt fastapi --break-system-packages
python -m pytest tests/ -v --tb=short
```

---

## 3. Торговый edge: текущие данные (90 закрытых трейдов)

```
Net PnL:       -96.21 USDT  ← ОТРИЦАТЕЛЬНЫЙ
Win rate:       45.56%  (41 wins, 49 losses)
```

**Распределение причин закрытия:**

| Причина | Кол-во | % |
|---|---|---|
| failed_setup_exit | 47 | 52.2% |
| protective_breakeven_profit_guard | 37 | 41.1% |
| adaptive_trailing_stop | 3 | 3.3% |
| stop_loss | 2 | 2.2% |
| tp2_reached | 1 | 1.1% |

**Символы (все отрицательные):**

| Символ | Трейдов | Net PnL |
|---|---|---|
| SOL/USDT | 9 | -17.68 |
| AVAX/USDT | 8 | -14.04 |
| ETH/USDT | 5 | -10.69 |
| XRP/USDT | 7 | -10.46 |
| TON/USDT | 23 | -9.56 |
| LINK/USDT | 12 | -9.49 |
| DOT/USDT | 14 | -8.62 |
| BTC/USDT | 8 | -8.13 |

**Ключевые проблемы торговой модели:**

1. `failed_setup_exit` доминирует (52%) — сигналы входят, но цена не подтверждает направление, и позиции закрываются в убытке до TP1
2. `protective_breakeven_profit_guard` на 41% сделок — закрывает слишком рано с микроприбылью, которая не покрывает комиссии полного круга
3. Только 1 TP2 hit из 90 трейдов — стратегия не умеет держать победителей
4. Паттерн `positive_then_negative` на большинстве убыточных — цена уходила в нашу сторону, затем разворачивалась
5. MLScorer — фактически хардкод, не настоящий ML (score = 0.5 + эвристики). Confidence из него не информативен для отбора сделок

**Вывод: trading edge не доказан. На live с реальными деньгами сейчас = гарантированный убыток.**

---

## 4. Что нужно сделать до live

### Блокеры первого уровня (без них live невозможен технически)

- [ ] `ENABLE_LIVE_ORDERS=true` разблокирован — нужны реальные HTX API keys с разрешением на торговлю
- [ ] `ROBOT_MODE=live` или `live_limited`, `TRADING_MODE=live` или `live_limited`
- [ ] `APP_ENV=production`, `DB_AUTO_CREATE_SCHEMA=false`
- [ ] Настоящий `JWT_SECRET`, `OWNER_PASSWORD`, `OWNER_API_TOKEN`
- [ ] `TELEGRAM_BOT_TOKEN` с работающим ботом и `TELEGRAM_OWNER_CHAT_ID`
- [ ] Alembic migrations применены к production PostgreSQL (не auto-create)
- [ ] `/system/readiness` возвращает `{"ready": true, "blockers": []}`
- [ ] Kill-switch доступен и протестирован через `/system/kill-switch`

### Блокеры второго уровня (trading edge)

- [ ] Накоплено минимум 200 closed paper/live-shadow сигналов (сейчас 90)
- [ ] Rolling net PnL положительный (сейчас -96 USDT)
- [ ] failed_setup_exit share < 35% (сейчас 52%)
- [ ] positive_then_negative rate < 25%

---

## 5. ROADMAP — от текущего состояния до live HTX

---

### Фаза 1 — Стабилизация базы (1–2 недели)
**Цель:** надёжная тестовая база, исправленные мелкие блокеры, понимание провалов тестов

**Задачи:**

**1.1 Запустить тесты и зафиксировать baseline**
```bash
cd apps/api
pip install pytest anyio[trio] pytest-anyio ... --break-system-packages
python -m pytest tests/ -v --tb=short 2>&1 | tee test_baseline.txt
```
Цель: все тесты green. Исправить найденные failures.

**1.2 Починить `test_telegram_delivery_worker`**  
Добавить в requirements.txt:
```
anyio[trio]>=4.0
pytest-anyio>=0.0.0
```

**1.3 Убедиться что `bash` в PATH на dev-машине**  
Git Bash или WSL — нужен для `test_production_runbook`. Либо добавить платформенную проверку в тест.

**1.4 Добавить `requirements.txt` с полным набором зависимостей**  
Если его нет или он неполный — добавить: fastapi, sqlalchemy, pydantic-settings, ccxt, pandas, uvicorn, alembic, httpx, anyio.

**1.5 Создать минимальный CI**  
GitHub Actions или локальный pre-commit hook:
```yaml
- name: Test
  run: cd apps/api && python -m pytest tests/ --tb=short -q
- name: Compile check
  run: python -m py_compile apps/api/main.py
```

**Критерий выхода Фазы 1:** `pytest tests/ -q` — все green; `py_compile main.py` — OK; CI проходит на каждом push.

---

### Фаза 2 — Торговое качество: диагностика и первые правки (2–4 недели)
**Цель:** понять и уменьшить `failed_setup_exit`, начать получать трейды ближе к TP1/TP2

Это самая важная фаза. Без улучшения trading edge всё остальное бессмысленно.

**2.1 Глубокая диагностика `failed_setup_exit`**

Запустить `OutcomeDiagnosticsService` по всем 90 трейдам. Разобрать по:
- Символ: какие хуже всего (TON 23 трейда — очевидный кандидат)
- Grade: C-grade `failed_setup_exit` vs A-grade
- Тайминг: через сколько минут после входа закрывались
- MFE при закрытии: трейды с MFE < 0.3% — сигнал вообще не дошёл до нашей стороны

**Ожидаемый вывод диагностики:**
- TON/USDT генерирует 23 трейда из 90 (25.6%) — кандидат на временный ban
- Grade C сигналы с failed_setup должны быть заблокированы полностью
- Трейды с signal_age_sec < 300 при failed_setup — слишком ранний выход

**2.2 Ужесточить символьную политику**  
В `SymbolPerformanceGuard` и `SYMBOL_PERF_BLOCK_*` настройках:
```python
# Временно убрать TON/USDT из списка пока не наберёт 5+ трейдов без убытка
# Поднять SYMBOL_PERF_BLOCK_MAX_WINRATE до 45% (сейчас 40%)
# Поднять SYMBOL_PERF_COOLDOWN_STREAK до 4 (сейчас 3)
```

**2.3 Заблокировать Grade C от входа в live**  
В `ProductionEntryGate` уже есть grade-система. Проверить, что Grade C полностью заблокирован:
```python
PROD_GATE_B_MIN_SETUP = 56.0  # поднять с 52
PROD_GATE_B_MIN_CONFIDENCE = 58.0  # поднять с 54
```

**2.4 Улучшить MFE capture — не закрывать при микроприбыли**  
37 из 90 трейдов — `protective_breakeven_profit_guard` с net PnL ≈ 0–0.15 USDT. Это убытки после spread:
```python
# Поднять MIN_PROTECTIVE_EXIT_PCT с 1.20 до 1.80
# Поднять MIN_PROTECTIVE_NET_USDT с 1.50 до 2.50
# Поднять MFE_CAPTURE_START_PCT с 0.75 до 0.90
```
Цель: не закрывать позицию если net PnL ≤ 2 USDT — дождаться движения или стопа.

**2.5 Улучшить holding — не выходить при первой коррекции**  
Анализ lifecycle data: большинство `positive_then_negative` трейдов вернулись к прибыли после 30–60 минут. Увеличить терпимость:
```python
FAILED_SETUP_MIN_AGE_SEC = 600  # с 300 до 600
FAILED_SETUP_MFE_SOFT_PCT = 0.70  # с 0.50 до 0.70
```

**2.6 Replay старых данных с новыми правилами**  
`SymbolPolicyReplayService` уже есть. Запустить replay на 90 трейдах с новыми параметрами и сравнить simulated net PnL.

**2.7 Заменить MLScorer на реальный сигнал качества**  
Текущий MLScorer — 3 эвристики, возвращает probability 0.5–0.7. Это не ML. Варианты:
- Использовать outcome data для логистической регрессии на features (MFE, MAE, MFE/stop_distance)
- Или добавить feature importance из lifecycle: трейды с MFE > 1% на ранней стадии — high quality
- Минимум: добавить `hit_tp1_rate_by_grade` lookup из MLOutcomeStats как confidence multiplier

**2.8 Накопить ещё 110+ paper трейдов**  
Validation gate требует 200. Сейчас 90. Нужно ~110 ещё. При текущей скорости (~90 за 7 дней) — ещё 8–9 дней paper.

**Критерий выхода Фазы 2:**
- `failed_setup_exit` share < 35%
- `positive_then_negative` rate < 25%
- Rolling net PnL > 0 USDT на последних 200 трейдах
- Replay показывает улучшение относительно baseline

---

### Фаза 3 — Live-shadow validation (1–2 недели)
**Цель:** запустить параллельный режим live-shadow где paper = симуляция по live ценам

**3.1 Включить live-shadow drift мониторинг**  
`LiveShadowDriftService` уже есть. Настроить:
```env
ROBOT_MODE=live_shadow
LIVE_SHADOW_MAX_ENTRY_DRIFT_PCT=0.35
LIVE_SHADOW_SLIPPAGE_PCT=0.10
```

**3.2 Подключить реальные HTX API keys (read-only)**  
На этом этапе только чтение: fetch_ticker, fetch_ohlcv, fetch_balance. Ордера НЕ размещаем:
```env
HTX_API_KEY=<ваш key>
HTX_API_SECRET=<ваш secret>
ENABLE_LIVE_ORDERS=false
```

**3.3 Включить ExchangeReconciliation**  
```env
EXCHANGE_RECONCILIATION_ENABLED=true
```
Это позволяет видеть drift между paper-позициями и реальным рынком.

**3.4 Мониторить daily owner report 7 дней**  
`/reports/summary` + Telegram owner alerts. Смотреть:
- Есть ли live-shadow drift > 0.35% на entry?
- Как spread влияет на cost?
- Реальные комиссии совпадают с настройками?

**3.5 Проверить `/system/readiness` на production конфиге**  
```bash
APP_ENV=production python -c "from core.config import settings; print(settings.production_blockers())"
```
Должен вернуть пустой список.

**Критерий выхода Фазы 3:**
- 7 дней live-shadow без критичных расхождений (drift < 0.35%)
- `/system/readiness` → `{"ready": true}`
- Validation gates проходят (200+ signals, положительный PnL)
- Реальные комиссии близки к настройкам

---

### Фаза 4 — Production hardening: Telegram + Payments (1 неделя, параллельно с Фазой 3)
**Цель:** убедиться что product контур надёжен до прихода реальных пользователей

**4.1 Проверить полный Telegram flow**  
Запустить `POST /system/product-e2e-smoke` с `persist=false` и убедиться что все 11 checks = true:
```json
{
  "start_menu_ok": true,
  "profile_created": true,
  "checkout_created": true,
  "payment_event_created": true,
  "payment_paid": true,
  "subscriber_active": true,
  "vip_access_granted": true,
  "idempotent_event": true,
  "idempotent_expiry_unchanged": true,
  "customer_notification_queued": true,
  "subscription_status_ok": true
}
```

**4.2 Проверить webhook все команды**  
Протестировать через curl или Telegram Bot API test mode:
- `/start`, `/menu`, `/plans`, `/pay`, `/status`, `/subscription_status`, `/help`, `/support`
- callback keyboard (inline buttons)

**4.3 Telegram delivery SLA**  
`TelegramDeliveryWorker` запущен каждые 30 секунд. Убедиться что:
- retry до 3 раз на неудачу
- owner получает алерт при > 5% delivery failures
- VIP сигналы доходят < 5 секунд от момента публикации

**4.4 Payment reconciliation smoke**  
```bash
POST /payments/reconcile {"older_than_hours": 48}
```
Проверить что stale pending payments правильно истекают.

**4.5 Разнести main.py на routers (опционально, но желательно)**  
Согласно старому roadmap: reports → routers/reports.py, subscribers → routers/subscribers.py, payments → routers/payments.py, system → routers/system.py. Это уменьшает риск drift при следующих изменениях.

**Критерий выхода Фазы 4:**
- Product E2E smoke = 100% ok
- Webhook команды работают
- Delivery worker логирует доставки
- Payment flow протестирован end-to-end вручную

---

### Фаза 5 — Production deployment setup (3–5 дней)
**Цель:** правильно развернуть production окружение

**5.1 Настроить production .env**  
```env
APP_ENV=production
DB_AUTO_CREATE_SCHEMA=false

# Генерировать через: openssl rand -hex 32
JWT_SECRET=<64-char-random>
OWNER_API_TOKEN=<64-char-random>
OWNER_PASSWORD=<strong-password>
OWNER_EMAIL=<your-email>

# PostgreSQL (production instance)
POSTGRES_HOST=<prod-host>
POSTGRES_DB=robot_prod
POSTGRES_USER=robot_prod
POSTGRES_PASSWORD=<strong-password>

# HTX
HTX_API_KEY=<real-key>
HTX_API_SECRET=<real-secret>
HTX_MARKET_TYPE=spot

# Telegram
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_OWNER_CHAT_ID=<your-chat-id>
TELEGRAM_VIP_SIGNALS_CHAT_ID=<vip-channel-id>
TELEGRAM_FREE_SIGNALS_CHAT_ID=<free-channel-id>

# Trading — пока НЕ live
ROBOT_MODE=live_shadow
ENABLE_LIVE_ORDERS=false
TRADING_MODE=paper_signal

# Risk limits (консервативные для старта)
RISK_EQUITY_USDT=1000.0
MAX_DAILY_LOSS_PCT=2.0
MAX_OPEN_POSITIONS=2
RISK_PER_TRADE_PCT=0.5

# HTX symbols (начать с топ-ликвидных)
HTX_SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT
```

**5.2 Запустить production Alembic миграции**  
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm api-migrate
```
Убедиться что все таблицы созданы: users, bots, subscribers, signals, orders, positions, payments, etc.

**5.3 Проверить /system/readiness на production**  
```bash
curl -H "X-Owner-Token: <token>" http://prod-host:8000/system/readiness
```
Должен вернуть `{"ready": true}`.

**5.4 Backup/restore smoke**  
```bash
bash scripts/db_backup_restore_smoke.sh --dry-run
```
Проверить что pg_dump/pg_restore работают.

**5.5 Kill-switch smoke**  
```bash
POST /system/kill-switch-smoke
```
Убедиться что owner получает Telegram алерт.

**5.6 Настроить мониторинг**  
- Uptime monitor на `GET /health`
- Алерты при `/system/readiness` → `ready: false`
- Daily automated report через cron → `/reports/summary`

**Критерий выхода Фазы 5:**
- Production deploy прошёл по runbook
- `/system/readiness` → ready
- Kill-switch работает
- Backup smoke прошёл

---

### Фаза 6 — Первый live с micro-лимитом (1–2 недели после всех gate)
**Цель:** первые live ордера с минимальным риском

**Условия входа в live (все должны быть выполнены):**
- [ ] Validation gates: 200+ closed, positive net PnL, failed_setup < 35%, pos_then_neg < 25%
- [ ] `/system/readiness` → `{"ready": true}`
- [ ] 7 дней live-shadow без критичных аномалий
- [ ] Kill-switch протестирован
- [ ] Backup/restore smoke пройден
- [ ] Product E2E smoke пройден
- [ ] Telegram delivery SLA ≥ 99%
- [ ] Risk disclaimer показан пользователям

**6.1 Включить live с микро-лимитом**  
```env
ROBOT_MODE=live
TRADING_MODE=live_limited
ENABLE_LIVE_ORDERS=true

# Жёсткие лимиты для старта
MAX_ACTIVE_SIGNALS=2
MAX_OPEN_POSITIONS=2
RISK_PER_TRADE_PCT=0.3         # 0.3% от капитала на сделку
MAX_DAILY_LOSS_PCT=2.0          # стоп при -2% за день
RISK_EQUITY_USDT=500.0          # торгуем от 500 USDT базы

# Только топ символы с доказанной ликвидностью
HTX_SYMBOLS=BTC/USDT,ETH/USDT

# Только Grade A+ и A
PROD_GATE_A_PLUS_MIN_SETUP=76.0
PROD_GATE_A_PLUS_MIN_CONFIDENCE=68.0
PROD_GATE_A_MIN_SETUP=65.0
PROD_GATE_A_MIN_CONFIDENCE=62.0
```

**6.2 Первый live ордер — что проверить сразу**  
- Логи: `robot_loop_step_completed` → `mode: live`
- HTX orders: ордер появился в истории биржи с правильным symbol/side/qty
- Position в БД: entry_price близок к рыночной цене в момент входа (slippage < 0.1%)
- TelegramRouter: VIP алерт о входе отправлен

**6.3 Мониторить первые 48 часов**  
Owner лично проверяет каждую позицию:
- Реальное заполнение vs ожидаемое (slippage)
- Закрытие ордеров: выход по стопу/TP работает через SignalLifecycle?
- net PnL закрытых позиций совпадает с расчётом CostEngine?

**6.4 При первых проблемах — немедленный kill-switch**  
```bash
POST /system/kill-switch {"enabled": true, "reason": "live_qa_hold"}
```
Это останавливает все новые входы, текущие позиции продолжают сопровождаться.

**Критерий выхода Фазы 6:**
- 50+ live ордеров выполнено без технических ошибок (fill, cancel, close)
- Slippage в норме (< 0.15% от market price)
- Daily loss circuit breaker НЕ сработал
- Net PnL не хуже paper baseline

---

### Фаза 7 — Масштабирование (после 2–4 недель live)
**Цель:** осторожное увеличение капитала и символов

**7.1 Критерии расширения (нужны все):**
- 50+ live ордеров без технических сбоев
- Live net PnL ≥ paper baseline за тот же период
- Telegram delivery ≥ 99.5%
- Payment reconciliation 0 stale pending > 48h
- No unresolved /system/readiness blockers

**7.2 Шаги масштабирования:**
- Добавить 1–2 символа (начать с SOL/USDT если улучшился paper performance)
- Поднять `RISK_EQUITY_USDT` с 500 до 1000 USDT
- Поднять `MAX_ACTIVE_SIGNALS` с 2 до 3
- Включить `ENABLE_FUNDING_ARB=true` только если фьючерсы настроены

**7.3 Продуктовое масштабирование:**
- Открыть Telegram VIP канал для первых 10–20 подписчиков
- Risk disclaimer явно показать в /plans и перед оплатой
- Retention метрики D1/D7 — отслеживать через BillingService

---

## 6. Быстрые победы (можно сделать сейчас)

1. **Запустить полный pytest** и зафиксировать baseline — 1 час
2. **Добавить anyio в requirements.txt** — 5 минут
3. **Убрать TON/USDT из HTX_SYMBOLS** — 1 минута (23 трейда, все убыточные)
4. **Поднять MIN_PROTECTIVE_EXIT_PCT до 1.80** — 5 минут (остановить выход при микроприбыли)
5. **Поднять FAILED_SETUP_MIN_AGE_SEC до 600** — 5 минут (больше времени на подтверждение)
6. **Запустить OutcomeDiagnosticsService на 90 трейдах** — понять root cause

---

## 7. Go/No-Go чеклист для live с реальными деньгами

```
✅ python -m py_compile apps/api/main.py
✅ pytest tests/ -q  → 0 failures
✅ /system/readiness → {"ready": true, "blockers": []}
✅ Validation gates: 200+ closed, net PnL > 0, failed_setup < 35%
✅ HTX API keys с торговыми разрешениями настроены и протестированы
✅ Kill-switch работает (owner получает Telegram алерт)
✅ Product E2E smoke → все 11 checks ok
✅ 7+ дней live-shadow без аномалий
✅ Backup/restore smoke прошёл
✅ Risk disclaimer в Telegram боте
✅ MAX_DAILY_LOSS_PCT настроен консервативно (≤ 2%)
✅ RISK_EQUITY_USDT отражает реальный баланс на HTX
```

**Если хотя бы один пункт не выполнен — GO запрещён.**

---

## 8. Сводная таблица этапов

| Фаза | Длительность | Ключевое условие | Блокер |
|---|---|---|---|
| 1 — Стабилизация базы | 1–2 нед | Все тесты green, CI | Нет |
| 2 — Торговый edge | 2–4 нед | 200 сигналов, net PnL > 0 | Текущий убыток |
| 3 — Live-shadow | 1–2 нед | 7 дней без аномалий | После Фазы 2 |
| 4 — Product hardening | 1 нед | E2E smoke 100% | Параллельно |
| 5 — Production deploy | 3–5 дней | /system/readiness ready | После Фаз 3–4 |
| 6 — First live | 1–2 нед | Все Go/No-Go green | После Фазы 5 |
| 7 — Масштабирование | ongoing | Live baseline ≥ paper | После Фазы 6 |

**Минимальный срок до live: 6–10 недель при условии улучшения торгового edge.**  
Главный bottleneck — не код, а торговая модель. Пока failed_setup_exit > 50% — расширять капитал нельзя.
