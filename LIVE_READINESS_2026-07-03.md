# FINMT — рефакторинг и подготовка к live (2026-07-03)

## 1. Что изменено в коде

### P0 — экономика (главный источник убытка)

**`services/htx_client.py` — `trading_fee_rates()`**
Для swap/futures/perp комиссии больше НЕ берутся у спот-инстанса ccxt (HTX_MARKET_TYPE=spot). Раньше spot-ставка 0.2% подставлялась в swap-расчёты → издержки завышались в ~4 раза (факт: total_cost 0.45% round-trip). Теперь: metadata контрактного рынка (`BTC/USDT:USDT`) → иначе `FUTURES_*` из settings (0.05%/0.02%).

**`services/exit_policy.py`**
- `_fee_rate()` по умолчанию использует **рынок исполнения** (`execution_market_type`), а не MARKET_TYPE данных.
- `_estimated_net_usdt()` принимает рыночную ставку вместо хардкода `SPOT_TAKER_FEE` — защитные выходы больше не глушатся завышенной оценкой издержек.
- Пол net-safe стал рыночным: спот 0.60%, деривативы 0.30% (`NET_SAFE_FLOOR_SPOT_PCT` / `NET_SAFE_FLOOR_SWAP_PCT`). Пороги protect/trail/capture пересчитываются от него.
- **`scalp_time_stop` стал cost-aware**: если по истечении таймера сделка не в значимом минусе (|cur| < net_safe) или показала жизнь (MFE ≥ 0.5×arm) и поток не против — держим до жёсткого стопа (`SCALP_TIME_STOP_HARD_MULT`, деф. 2×). Реальный минус или CVD-разворот закрывают сразу. Кейс AAVE #181 (−0.58% на ровном месте) при новых правилах — hold.

**`services/signal_lifecycle.py`**
- 4 вызова exit-политики переведены с `MARKET_TYPE` на `execution_market_type`.
- Breakeven-буфер после TP1 — по рыночной ставке, а не `SPOT_TAKER_FEE`.

### P0 — ML

**`services/ml_features.py`**: CVD-фичи зануляются при `cvd_trades < 10` (константа `CVD_MIN_TRADES`) — одинаково в train и serve. Раньше `cvd_ratio=±1.0` от 1–2 сделок в окне шёл в модель как «сигнал» (в live так почти всегда).
После деплоя **переобучить модель** («Обучить сейчас» на ML-странице) — фичи изменились.

### P1 — grid (`services/grid_engine.py`)

- **Анти-пила флипа**: flip_streak растёт только когда `|price − EMA| ≥ 0.5×ATR` (`GRID_FLIP_MIN_ATR_DIST`). Лечит серию мелких flip-минусов (12 из 20 последних закрытий, −1.73 USDT; BTC — 4 флипа подряд).
- **Анти-мартингейл**: доборы заблокированы при `regime_now` противоположном корзине или RSI против стороны (шорт при RSI≥65, лонг при RSI≤35). Кейс TRX (5 доливок в шорт на импульсе) больше невозможен. Статус виден в поле `fills_paused` цикла.
- **Экономический фильтр открытия**: цикл не открывается, если шаг 1-го уровня < спред×1.0 + fee_round (кейс AAVE neutral-грид на спреде 0.1–0.5%).
- **Комиссии в realized**: при любом закрытии корзины из результата вычитается round-trip fee по исполненному нотионалу (раньше realized был gross).

### P3 — надёжность

- **`services/signal_lifecycle.py`**: `process_signal` и `process_signal_with_price` слиты в единый `_process_signal_core` (−370 строк дубля). Устранена боевая дивергенция: guard `low_grade_capital_release` существовал только в тестовом пути — теперь работает и в боевом.
- **`workers/robot_loop.py`**: дедуп blocked-событий — `blocked_post_loss_cooldown`, `blocked_depth_gate`, anti-drain-блоки и `reentry_cooldown_active` пишутся не чаще раза в `INTEL_EVENT_DEDUP_MINUTES` (10 мин) на (symbol, decision). Останавливает рост intelligence_events (34.7k строк).
- **`services/execution_engine.py`**: закрытая позиция получает `unrealized_pnl = 0` (реализованный результат — в `Signal.closed_net_pnl`); `/positions` больше не показывает «нереализованный» PnL у закрытых.

### Новые настройки (все с безопасными дефолтами, env не обязателен)

```
SCALP_TIME_STOP_HARD_MULT=2.0
NET_SAFE_FLOOR_SPOT_PCT=0.60
NET_SAFE_FLOOR_SWAP_PCT=0.30
INTEL_EVENT_DEDUP_MINUTES=10
GRID_FLIP_MIN_ATR_DIST=0.5
GRID_ANTI_MARTINGALE_ENABLED=true
GRID_SHORT_FILL_RSI_MAX=65
GRID_LONG_FILL_RSI_MIN=35
GRID_OPEN_MIN_EDGE_SPREAD_MULT=1.0
GRID_FEES_IN_REALIZED=true
```

Миграций БД нет — деплой на render обычным пушем.

---

## 2. Правки env на render (robot-api)

| Переменная | Сейчас | Рекомендация | Зачем |
|---|---|---|---|
| `ML_MODE` | shadow | **advisory** (сразу после переобучения) | Live shadow AUC 0.7424; гейт 0.45 отрезал бы WR-11% хвост (+10.39 USDT/17 сделок). Advisory не влияет на сделки, но пишет рекомендацию — копим доказательства для full_auto |
| `HTX_MARKET_TYPE` | spot | оставить spot | Это тип инстанса ДАННЫХ; ставки swap теперь резолвятся корректно фиксом fee |
| `ENABLE_LIVE_ORDERS` | false | оставить false до прохождения чек-листа §4 | |
| `EXCHANGE_RECONCILIATION_ENABLED` | false | **true перед live** | Сверка позиций с биржей обязательна при живых ордерах |
| `RISK_PER_TRADE_PCT` | 0.4 | ok | |
| `MAX_DAILY_LOSS_PCT` | 3 | ok (circuit breaker работает, `LiveSafetyService`) | |

Опционально после недели наблюдений: `ML_MODE=full_auto` (критерии в §4).

---

## 3. Ожидаемые сдвиги после деплоя (что проверять в телеметрии)

1. `closed_total_cost` сделок падает с ~0.45% до ~0.12–0.14% нотионала (swap taker 0.05%×2 + слиппедж). Если не упал — фикс fee не подхватился, смотреть `fee_source` в /costs (должен быть `contract_market_metadata` или `fallback_futures_settings`, НЕ `exchange_api`).
2. Доля `scalp_time_stop`-закрытий в минус падает; появляются закрытия с note `hard=...` только у реально мёртвых сделок.
3. `grid_regime_flip`-закрытий становится в разы меньше; у циклов появляется `fills_paused` при импульсе против корзины.
4. Рост `intelligence_events` резко замедляется (~в 20 раз по blocked-типам).
5. Так как гейты (`net_rr`, `min_net_pnl`) считаются от честных swap-издержек — **кандидатов станет проходить больше**. Первые дни следить за качеством, а не только количеством.

---

## 4. Чек-лист выхода в live (по порядку)

**Этап A — сразу после деплоя (paper, 3–7 дней):**
- [ ] Переобучить мета-лейблер (фичи изменились), убедиться `val_auc` в meta.
- [ ] `ML_MODE=advisory`.
- [ ] Проверить п.1–5 из §3 на живой телеметрии.
- [ ] Прогнать тесты CI (`apps/api/tests`) — обязательно до пуша в прод.

**Этап B — критерии готовности (paper-выборка ПОСЛЕ фикса издержек):**
- [ ] ≥50 закрытых сделок с новой экономикой.
- [ ] Expectancy > 0 (сумма closed_net_pnl / число сделок).
- [ ] Live-AUC shadow/advisory ≥ 0.60 на ≥50 сделках со score.
- [ ] Grid: realized (уже net of fees) ≥ 0 за период.
- [ ] Старая статистика (WR 29.31%) — до фикса издержек; baseline считать заново, старые строки датасета не удалять (модель переобучится, метки честные).

**Этап C — включение live (малый капитал):**
- [ ] `EXCHANGE_RECONCILIATION_ENABLED=true`.
- [ ] На бирже: API-ключ с правами trade (без withdraw), IP-whitelist render.
- [ ] `ENABLE_LIVE_ORDERS=true`, стартовый капитал ≤ 10–15% от целевого; `FUTURES_LEVERAGE=1`.
- [ ] Проверить kill switch с фронта и circuit breaker (`MAX_DAILY_LOSS_PCT`) на первом же дне.
- [ ] `ML_MODE=full_auto` — только после этапа B и первых стабильных live-дней; гейт 0.45, размер 0.7–1.25× уже в guardrails.

**Известные ограничения (следующая итерация):**
- Нет кросс-движкового учёта экспозиции (интеллект + grid могут держать один символ одновременно) — до его появления не расширять GRID_SYMBOLS на символы тренд-движка с плечом >1.
- `main.py` (3687 строк) и `_ready_candidate_check` (дублирование цепочки гейтов) — рефакторинг без изменения поведения, делать отдельным PR.
- Вход скальпа — taker; maker-first (post-only в зоне) даст ещё −0.03% на сторону на swap.
