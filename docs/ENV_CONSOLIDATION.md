# Консолидация конфига: вывод каши из environment

**Статус:** план на утверждение
**Дата:** 2026-06-15
**Принцип:** код — единственный источник правды для тюнинга; env — только секреты,
инфра, флаги ON/OFF и деплой-уровень (equity/mode/symbols). Override через env
остаётся возможным (strategy_profiles/config читают `getattr(settings, NAME, default)`),
но НЕ обязателен: дефолт в коде = намеренное значение.

---

## 0. Корень проблемы

В проде (`robot-api.env`) ~73 переменные. Из них ~11 **молча противоречат**
дефолтам в `config.py`. Поэтому ни код, ни env по отдельности не описывают
поведение бота. Это не дублирование — это рассинхрон.

### Противоречия env ↔ код (ОПАСНО — чинить первым)

| переменная | env (прод) | config.py | решение |
|---|---|---|---|
| `ANTI_DRAIN_MAX_OPEN_POSITIONS` | 5 | 2 | бакнуть 5 в код |
| `ANTI_DRAIN_MAX_USED_MARGIN_PCT` | 50 | 30 | бакнуть 50 |
| `SYMBOL_PERF_LOOKBACK` | 10 | 12 | (заменяется окном по времени — см. ниже) |
| `SYMBOL_PERF_BLOCK_MAX_WINRATE` | 48 | 42 | бакнуть 48 |
| `SYMBOL_PERF_REDUCE_MAX_WINRATE` | 55 | 50 | бакнуть 55 |
| `SYMBOL_PERF_COOLDOWN_FAILED_SETUPS` | 2 | 3 | бакнуть 2 |
| `SYMBOL_PERF_COOLDOWN_STREAK` | 3 | 4 | бакнуть 3 |
| `OB_CVD_EXIT_RATIO` | 0.8 | 0.6 | бакнуть 0.8 |
| `OB_CVD_MIN_TRADES` | 25 | 15 | бакнуть 25 |
| `SCALP_BREAKEVEN_GIVEBACK_SHARE` | 0.6 | 0.5 | бакнуть 0.6 |
| `CRT_LTF_TF` | 5m | 15m | бакнуть 5m |

Правило миграции: **сначала текущее прод-значение становится дефолтом в коде,
ПОТОМ переменная удаляется из env.** Тогда удаление ничего не меняет в поведении —
чистый рефактор без рисков.

---

## 1. Категории (что куда)

### A. ОСТАЁТСЯ в env — секреты (никогда не в код, не в git)
`JWT_SECRET`, `HTX_API_KEY`, `HTX_API_SECRET`, `OWNER_API_TOKEN`,
`OWNER_PASSWORD`, `TELEGRAM_BOT_TOKEN`.

### B. ОСТАЁТСЯ в env — инфра/деплой (зависит от окружения)
`APP_ENV`, `DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS`, `HTX_API_HOSTNAME`,
`DB_AUTO_CREATE_SCHEMA`, `TRADE_OUTCOMES_PATH`.

### C. ОСТАЁТСЯ в env — бизнес/каналы
`OWNER_EMAIL`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_*_CHAT_ID`,
`TELEGRAM_*_TIMEOUT`, `VIP_*`, `AFFILIATE_FREE_VIP_DAYS`.

### D. ОСТАЁТСЯ в env — деплой-уровень торговли (рубильники режима)
`ROBOT_MODE`, `TRADING_MODE`, `ENABLE_LIVE_ORDERS`, `HTX_MARKET_TYPE`,
`HTX_SYMBOLS`, `RISK_EQUITY_USDT`, и все `ENABLE_*` стратегий/движков
(`ENABLE_CRT_STRATEGY`, `ENABLE_RANGE_STRATEGY`, `ENABLE_ORDERBOOK_ENGINE`,
`ENABLE_SCALP_STRATEGY`, `ENABLE_FUTURES*`, `ENABLE_FUNDING_ARB`, `NEWS_ENABLED`,
`EXCHANGE_RECONCILIATION_ENABLED`, `FUNDING_ARB_*`).

### E. УХОДИТ в код (дефолт в config.py/strategy_profiles), удаляется из env — ТЮНИНГ
Все пороги движков и риск-слоёв, у которых уже есть дефолт в коде:
- `PROD_GATE_*` (6 шт) → профиль качества
- `SYMBOL_PERF_*` (тюнинг guard; lookback заменён `SYMBOL_PERF_WINDOW_HOURS`)
- `OB_*` (CVD/спред/стены — depth-движок)
- `SCALP_*`, `POST_LOSS_*`, `ANTI_DRAIN_*` (кроме оставленных в D)
- `LIVE_SHADOW_*`, `MARKET_CONNECTIVITY_*`, `VALIDATION_MIN_CLOSED_SIGNALS`,
  `RISK_PER_TRADE_PCT`, `RANGE_ALLOW_SHORT`, `CRT_LTF_TF`, `FUTURES_LEVERAGE`.

Итог: env ~73 → **~35** (только A–D). Тюнинг живёт в коде, override через env
остаётся как «аварийный рычаг», но дефолт = намеренное значение.

---

## 2. Фазы (каждая отдельно, безопасно)

1. **Reconcile** — привести дефолты `config.py` к текущим прод-значениям (таблица §0).
   Поведение НЕ меняется. Деплой, проверка, что метрики идентичны.
2. **Strip env** — удалить из Render все переменные категории E (они теперь = коду).
   Деплой, проверка идентичности. env усыхает до ~35.
3. **(Опционально) Profiles** — сгруппировать тюнинг в `strategy_profiles.py`
   по движкам (range/trend/crt/scalp/depth) — уже начато; доедаем потребителей.
4. **`.env.example`** — обновить: только A–D + комментарий «тюнинг в config.py».

---

## 3. Принцип на будущее (чтобы каша не вернулась)

- Новый тюнинг-параметр → **сначала дефолт в config.py/профиле**, env только если
  реально зависит от деплоя.
- Менять тюнинг для теста → правим **дефолт в коде** и коммитим (видно в git),
  а не плодим env-override. Render-override — только для горячего хотфикса,
  с обязательным последующим переносом в код.
- Любое значение в env, дублирующее дефолт кода, — кандидат на удаление.
