# Матрица аудита системы Robot V1: что уже есть, что не дублировать, что делать дальше

Дата: **4 июня 2026 UTC**.
Цель документа: зафиксировать результат повторного прохода по репозиторию после замечания «не дублируй то, что уже сделано», разделить уже реализованные контуры и реальные gaps из старого списка «первые 10 задач».

---

## 1. Покрытие текущего прохода

Проверены tracked-файлы репозитория по доменам:

| Домен | Что проверено | Вывод |
|---|---|---|
| `apps/api` | models, services, workers, routers, migrations, tests, `main.py` | Основной функционал payments/Telegram/subscribers/readiness уже реализован; найден и удален остаточный dead-code drift в `main.py` и `intelligence_memory.py`. |
| `apps/web` | owner pages, components, API client | Payments/health/readiness UI уже есть; новый блок Monetization не нужно создавать заново, нужно усиливать существующую страницу payments и health. |
| `telegram` | отдельный Docker/bot entrypoint | Не основной продуктовый webhook; основной customer menu сейчас обслуживает API `/telegram/webhook`. |
| `scripts` | run reports, ML summary, replay, backup smoke | Есть dry-run backup/restore и outcome/replay tooling; E2E product smoke еще нужен отдельно. |
| `infra` | nginx/start/init DB | Базовая инфраструктура есть; release-hardening остается задачей runbook/ops. |
| `docs` | production runbook | Runbook есть, но go/no-go checklist нужно расширять фактами из `/system/readiness`. |
| `analytics_24h` / `storage/ml` | исторические snapshots/outcomes | Есть данные для baseline и replay; текущий торговый edge по последнему snapshot не доказан. |

---

## 2. Удаленный рассинхрон без дублирования функционала

### 2.1 `apps/api/main.py`

Удален legacy helper `_telegram_menu_text(...)`, потому что реальный Telegram UX уже централизован в `TelegramBotMenuService`. Держать второй набор текстов/команд в `main.py` опасно: он устаревает отдельно и снова создает рассинхрон между webhook и меню.

Канонический путь теперь один:

`/telegram/webhook` → `TelegramBotMenuService().handle(...)` → `SignalBroadcaster().send_message(...)`.

### 2.2 `apps/api/services/intelligence_memory.py`

Удалены:

- недостижимый повтор блока создания `IntelligenceEvent` после `return event`;
- продублированные module-level функции `_is_noisy_decision` и `_has_recent_same_noisy_event`, которые уже существуют как методы `IntelligenceMemory`.

Это не меняет бизнес-логику, а убирает генерационный мусор и снижает риск неправильного импорта/копирования.

### 2.3 Telegram command gap

Команда `/subscription_status` добавлена как alias к существующему `/status`, чтобы закрыть пункт старого roadmap без создания второго обработчика статуса подписки.

---

## 3. Старые «первые 10 задач»: текущий статус без дублирования

| № | Задача из старого roadmap | Статус сейчас | Что делать без дублирования |
|---:|---|---|---|
| 1 | Зафиксировать релизный baseline и ветку | Частично есть: git history, analytics snapshots, run reports | Добавить release tag/branch policy и baseline artifact в runbook. |
| 2 | Спроектировать payment state-machine + БД миграцию | В основном есть: `BillingPlan`, `Payment`, `PaymentEvent`, `Subscriber`, Alembic operational migration | Не создавать новые таблицы без причины; формализовать status enum/transition doc и добавить transition tests. |
| 3 | Реализовать webhook endpoint + подпись провайдера | Частично есть: owner endpoint `/payments/events`; публичного provider webhook/signature adapter нет | Добавлять только после выбора провайдера; вынести в `PaymentProviderAdapter`, не ломая текущий manual flow. |
| 4 | Добавить idempotency таблицу/ключи | Есть через unique `(provider, provider_event_id)` в `payment_events` и tests | Не дублировать таблицу; усилить error handling и provider payload signature verification. |
| 5 | Сделать `grant_vip_access()` после оплаты | Логика есть в `BillingService.confirm_payment()` | Не дублировать сервис; можно позже переименовать/выделить thin alias `grant_vip_access` для читаемости API. |
| 6 | Добавить команды Telegram меню и callback маршруты | Есть в `TelegramBotMenuService`: menu/plans/pay/status/help/support/HTX/callbacks | Расширять этот сервис, не добавлять тексты в `main.py`. |
| 7 | Ввести `/subscription_status` | Сделано как alias к `/status` | Поддерживать один текст статуса и один test contract. |
| 8 | Owner dashboard “Monetization health” | Частично есть: `apps/web/app/payments/page.tsx`, payments summary/revenue; health/readiness отдельно | Не создавать новую страницу; добавить компактный блок SLA/reconciliation на существующую Payments/Health страницу. |
| 9 | 1–2 правки в выход из failed setup | Частично есть: exit policy v4, MFE capture, validation gates, symbol policy tooling | Следующий кодовый шаг: diagnostics/replay по `failed_setup_exit`, затем точечный invalidate/capture change. |
| 10 | Dry-run E2E и go/no-go checklist | Частично есть: backup dry-run, kill-switch smoke, `/system/readiness` | Нужен новый product E2E smoke Telegram→payment→subscriber и checklist в runbook. |

---

## 4. Следующие безопасные задачи

1. Добавить product E2E smoke test: Telegram `/start` → `pay:vip_30` → payment event → active subscriber → `/subscription_status`.
2. Формализовать payment transitions без новой таблицы: `pending -> paid|failed|expired|refunded`.
3. Добавить provider webhook только после выбора провайдера и секрета подписи.
4. Доработать существующую payments dashboard как Monetization health, а не создавать новую сущность.
5. Запустить replay/diagnostics по `failed_setup_exit` и менять только один exit-policy параметр за раз.
6. Расширить `docs/PRODUCTION_RUNBOOK_RU.md` go/no-go checklist на основе `/system/readiness`.
