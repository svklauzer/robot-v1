# Execution Plan V1 (Profitability + Telegram/VIP)

## Цель
Стабилизировать качество сигналов и сопровождения сделок перед live-торговлей, а также подготовить Telegram/VIP контур к монетизации.

## Что внедрено в этом шаге

### 1) Конфигурируемые пороги exit-policy
Добавлены параметры в `apps/api/core/config.py`, чтобы управлять ранним выходом из слабых сетапов и защитой прибыли без хардкода.

**Зачем:**
- быстрее тюнинговать стратегию по фактической статистике,
- уменьшать долю `failed_setup_exit` и сценариев “был плюс → ушёл в минус”,
- управлять агрессивностью сопровождения без переписывания логики.

### 2) ExitPolicy переведена на параметры Settings
В `apps/api/services/exit_policy.py` логика `before_tp1_decision` теперь использует пороги из `.env` (через Settings), включая:
- early failed setup exits,
- MFE-based profit protection,
- adaptive trailing блок.

**Зачем:**
- получить управляемую “ручку” риска и сопровождения,
- ускорить цикл гипотеза → прогон → корректировка.

### 3) Ужесточение публикации в paper-режиме
В `apps/api/services/signal_quality.py`:
- класс `C` отключён для публикации даже в paper,
- порог для `B` повышен.

**Зачем:**
- не засорять выборку слабым шумом,
- приблизить paper-поток к боевому quality-bar,
- улучшить качество статистики для будущего ML.

---

## Следующий обязательный блок (следующий коммит)
1. `analytics_24h` reason-breakdown в API: endpoint с вкладом причин закрытия в net PnL.
2. Trade journaling v2: запись snapshot-фичей на входе и на выходе.
3. Telegram payments blueprint: states + webhook contracts + idempotency.
4. Bot menu UX: «Мой статус», «Купить VIP», «Продлить», «История оплат».

## Рекомендуемые стартовые значения .env
- `FAILED_SETUP_MFE_SOFT_PCT=0.20`
- `FAILED_SETUP_LOSS_SOFT_PCT=-0.25`
- `FAILED_SETUP_MFE_MID_PCT=0.45`
- `FAILED_SETUP_LOSS_MID_PCT=-0.45`
- `FAILED_SETUP_MFE_DEEP_PCT=0.70`
- `FAILED_SETUP_LOSS_DEEP_PCT=-0.70`
- `PROTECTIVE_MFE_START_PCT=0.45`
- `PROTECTIVE_DRAWDOWN_SHARE=0.60`
- `ADAPTIVE_TRAIL_MFE_START_PCT=1.20`
- `ADAPTIVE_TRAIL_DRAWDOWN_PCT=0.55`

