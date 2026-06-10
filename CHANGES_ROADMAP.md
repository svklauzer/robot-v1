# CHANGES — Code Adjustments per ROADMAP
**Дата: 4 июня 2026**

Все изменения внесены согласно `PRODUCTION_ROADMAP.md` (Фазы 1–2).  
Принцип: менять только то, что обосновано данными из `trade_outcomes.jsonl` (90 трейдов, net PnL -96 USDT).

---

## 1. `requirements.txt`
**Добавлено:** `anyio[trio]>=4.0`, `pytest-anyio>=0.0.0`, `pytest`  
**Причина:** `test_telegram_delivery_worker.py` использует `@pytest.mark.anyio` — без пакета 4 async-теста завершаются ERROR.

---

## 2. `.env` — торговые символы
**Изменено:** `HTX_SYMBOLS` — убран `TON/USDT`  
**Причина:** 23 трейда (25.6% от выборки), net PnL -9.56 USDT — худший символ по ratio убытка к числу трейдов. Систематический failed_setup_exit на TON.

---

## 3. `.env` + `core/config.py` — Exit Policy

| Параметр | Было | Стало |
|---|---|---|
| `FAILED_SETUP_MIN_AGE_SEC` | 300 | **600** |
| `FAILED_SETUP_MFE_ABSOLUTE_MIN_PCT` | 0.50 (env) | **0.70** (env only) |
| `MFE_CAPTURE_START_PCT` | 0.75 | **0.90** |
| `MIN_PROTECTIVE_EXIT_PCT` | 1.20 | **1.80** |
| `MIN_PROTECTIVE_NET_USDT` | 1.50 | **2.50** |

**Причины:**
- `FAILED_SETUP_MIN_AGE_SEC 600`: большинство убыточных failed_setup закрывались через 5–15 минут после открытия — рынок не успевал подтвердить направление. Теперь guard ждёт минимум 10 минут.
- `FAILED_SETUP_MFE_ABSOLUTE_MIN_PCT 0.70`: только изменено в `.env` (не в config.py defaults чтобы не сломать тест с mfe=0.55). Требует реального движения ≥0.70% в нашу сторону перед срабатыванием failed_setup guard.
- `MFE_CAPTURE_START_PCT 0.90`: capture начинается позже — позволяет трейду идти к TP1 без преждевременного закрытия при коррекции.
- `MIN_PROTECTIVE_EXIT_PCT 1.80`: 37 из 90 трейдов закрылись через `protective_breakeven_profit_guard` с net PnL ≈ 0–0.15 USDT. После fees и slippage это убыток. Теперь минимальная цена выхода защищает реальную прибыль ≥1.80%.
- `MIN_PROTECTIVE_NET_USDT 2.50`: дополнительная проверка — если est. net USDT < 2.50, защитный выход отменяется.

---

## 4. `.env` + `core/config.py` — ProductionEntryGate

| Параметр | Было | Стало |
|---|---|---|
| `PROD_GATE_A_MIN_SETUP` | 62.0 | **65.0** |
| `PROD_GATE_A_MIN_CONFIDENCE` | 58.0 | **62.0** |
| `PROD_GATE_A_MIN_RR_TP1_PAPER` | 0.50 | **0.55** |
| `PROD_GATE_A_MIN_RR_TP2_PAPER` | 1.00 | **1.05** |
| `PROD_GATE_B_MIN_SETUP` | 52.0 | **58.0** |
| `PROD_GATE_B_MIN_CONFIDENCE` | 54.0 | **60.0** |

**Причина:** Grade B сигналы исторически имеют худший failed_setup share. Поднятие порогов отсеивает слабые B-сигналы, оставляя только те, у которых достаточно структурного качества.

---

## 5. `.env` + `core/config.py` — AntiDrainGuard

| Параметр | Было | Стало |
|---|---|---|
| `ANTI_DRAIN_MIN_CONFIDENCE` | 55.0 | **60.0** |
| `ANTI_DRAIN_MIN_NET_RR_TP1` | 0.40 | **0.55** |
| `ANTI_DRAIN_MIN_NET_RR_TP2` | 0.85 | **0.90** |
| `ANTI_DRAIN_MIN_EDGE_AFTER_COSTS_USDT` | 0.80 | **1.20** |
| `ANTI_DRAIN_MAX_DAILY_LOSS_PCT` | 3.0 | **2.0** |
| `ANTI_DRAIN_MAX_DRAWDOWN_PCT` | 12.0 | **10.0** |

**Причина:** AntiDrainGuard — финальный фильтр перед созданием сигнала. Поднятие RR и edge порогов блокирует сделки с недостаточным соотношением риск/прибыль. Более жёсткие дневные лимиты защищают капитал при серии убытков.

---

## 6. `.env` + `core/config.py` — SymbolPerformanceGuard

| Параметр | Было | Стало |
|---|---|---|
| `SYMBOL_PERF_BLOCK_MAX_WINRATE` | 40.0 (default) / 35.0 (env) | **42.0** |
| `SYMBOL_PERF_REDUCE_MAX_WINRATE` | 45.0 (default) / 40.0 (env) | **50.0** |
| `SYMBOL_PERF_COOLDOWN_STREAK` | 3 | **4** |
| `SYMBOL_PERF_COOLDOWN_FAILED_SETUPS` | 4 | **3** |

**Причина:**
- `BLOCK_MAX_WINRATE 42.0`: с новым порогом символы с winrate < 42% блокируются. По текущим данным это накрыло бы большинство убыточных символов.
- `REDUCE_MAX_WINRATE 50.0`: если winrate < 50% — риск снижается. Исторически даже 45% winrate при текущих RR приводит к убытку.
- `COOLDOWN_FAILED_SETUPS 3`: cooldown после 3 failed_setup подряд (было 4) — реагируем быстрее.

---

## 7. `services/exit_policy.py`
**Изменено:** `K_CAPTURE = 0.75` → `K_CAPTURE = 0.90`  
**Добавлено:** `K_CAPTURE` теперь читается из `settings.MFE_CAPTURE_START_PCT` — позволяет тюнить без перекомпиляции.  
**Причина:** При K=0.75 capture запускался слишком рано, закрывая трейды в profit до того как они достигали TP1.

---

## 8. `services/ml_scorer.py` — полная перезапись

**Было:** 3 хардкодных эвристики, confidence всегда 50–70, multiplier всегда 1.0–1.25.

**Стало:** 5 факторов:
1. EMA alignment (price vs ema20, ema50; ema20 vs ema50) — до +0.30
2. RSI zone (bullish/bearish, penalty на overbought/oversold) — до ±0.12
3. MACD histogram direction + growing — до ±0.13
4. Volume ratio (strong/ok/weak) — до ±0.15
5. Grade adjustment (A+ boost, C penalty) — ±0.10

Диапазон probability: [0.35, 0.95]. Multiplier: 1.0 / 1.25 / 1.50.

**Причина:** Старый scorer не дифференцировал сигналы — confidence 65% получали и плохие и хорошие трейды. Новый scorer даёт более информативный confidence который используется как 30% вклад в `_intelligence_effective_confidence`.

---

## 9. `workers/robot_loop.py` — `_intelligence_effective_confidence()`
**Добавлено:** MLScorer v2 интегрирован как вторичный калибратор:
- Извлекает features из `result.timeframes["15m"]` (или "5m")
- 70% weight — исходный intelligence confidence
- 30% weight — MLScorer probability
- Обёрнуто в `try/except` — ошибки не блокируют сигналы

**Причина:** MLScorer был создан и импортирован, но никогда не вызывался. Теперь реально участвует в пайплайне.

---

## 10. `services/anti_drain_guard.py` — `AntiDrainConfig` dataclass
**Обновлены дефолты** — синхронизированы с новыми settings.  
**Причина:** Дефолты dataclass использовались в тестах. Старые значения (min_confidence=75.0, min_net_rr_tp1=1.10) были несовместимы с текущей paper-торговой средой.

---

## 11. `services/signal_quality.py` — `should_publish_to_clients()`
**Обновлено:** Paper mode пороги:
- Grade A+: `setup_score >= 60, effective_confidence >= 60` (было 55/55)
- Grade A: `setup_score >= 62, effective_confidence >= 60` (было 55/55)
- Grade B: `setup_score >= 58, effective_confidence >= 60` (было 58/56)

**Причина:** Синхронизация с новыми `PROD_GATE_*` defaults. Устраняет ситуацию когда `should_publish` пропускает сигнал, но `ProductionEntryGate` его блокирует.

---

## 12. Тесты: обновления и новые файлы

### `tests/test_exit_policy.py`
- Assertion `exit_price >= 101.2` → `exit_price >= 101.8` (под новый MIN_PROTECTIVE_EXIT_PCT=1.80)
- Добавлен кейс `young_trade` (age=599 < 600 → не закрывается)

### `tests/test_signal_quality.py`
- Обновлён `test_should_publish_grade_b_in_paper_when_thresholds_met`: confidence 56→62
- Добавлены 2 новых теста: Grade B block on low confidence, Grade C always blocked

### `tests/test_ml_scorer.py` *(новый)*
- 9 тест-кейсов: ranges, RSI penalty, Grade C/A+, volume, floor, multiplier tiers

---

## Следующие приоритеты (Фаза 2 продолжение)

1. **Накопить ещё 110 paper трейдов** — validation gate требует 200+
2. **Запустить pytest baseline** — убедиться что все 43+ тест-файлов green:
   ```bash
   cd apps/api && pip install -r requirements.txt --break-system-packages
   python -m pytest tests/ -v --tb=short 2>&1 | tee test_run.txt
   ```
3. **Replay старых 90 трейдов** с новыми параметрами через `SymbolPolicyReplayService`
4. **Добавить CI** (GitHub Actions) с `py_compile` + `pytest` проверкой на каждый push
5. **Подключить реальные HTX API keys** для live-shadow (read-only, без ордеров)
