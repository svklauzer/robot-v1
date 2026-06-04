# Матрица аудита системы Robot V1: что уже есть, что не дублировать, что делать дальше

Дата: **4 июня 2026 UTC**.
Цель документа: зафиксировать результат повторного прохода по репозиторию после замечания «не дублируй то, что уже сделано», разделить уже реализованные контуры и реальные gaps из старого списка «первые 10 задач».

---

## 1. Покрытие текущего прохода

Проверены tracked-файлы репозитория по доменам:

| Домен | Что проверено | Вывод |
|---|---|---|
| `apps/api` | models, services, workers, routers, migrations, tests, `main.py` | Основной функционал payments/Telegram/subscribers/readiness уже реализован; найден и удален остаточный dead-code drift в `main.py` и `intelligence_memory.py`; paper-сигналы больше не блокируются Telegram timeout, closed outcomes backfill пишется в JSONL, добавлен product E2E smoke Telegram→payment→subscription. |
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
| 10 | Dry-run E2E и go/no-go checklist | Product E2E smoke добавлен: `POST /system/product-e2e-smoke`; также есть backup dry-run, kill-switch smoke, `/system/readiness` | Осталось расширить runbook go/no-go checklist и запускать smoke перед релизом. |

---

## 4. Следующие безопасные задачи

1. Запускать product E2E smoke перед релизом: `POST /system/product-e2e-smoke` проверяет Telegram `/start` → `pay:vip_30` → payment event → active subscriber → `/subscription_status` и по умолчанию откатывает изменения.
2. Формализовать payment transitions без новой таблицы: `pending -> paid|failed|expired|refunded`.
3. Добавить provider webhook только после выбора провайдера и секрета подписи.
4. Доработать существующую payments dashboard как Monetization health, а не создавать новую сущность.
5. Запустить replay/diagnostics по `failed_setup_exit` и менять только один exit-policy параметр за раз.
6. Расширить `docs/PRODUCTION_RUNBOOK_RU.md` go/no-go checklist на основе `/system/readiness`.


---

## 5. Hotfix 4 июня 2026: publication → paper open → ML outcomes

Проблема по текущим логам: `telegram_send_error ConnectTimeout` мог переводить публичный paper-сигнал в `telegram_failed`, из-за чего lifecycle не видел его как `published` и не открывал paper-position. Для paper режима Telegram должен быть SLA/доставка с retry, но не gate торгового состояния.

Исправлено:

1. В paper modes Telegram full-signal failure больше не переводит сигнал в `telegram_failed`; сигнал остается `published`, а ошибка сохраняется в `plan_json.telegram_delivery` как `non_blocking_paper`.
2. В live modes Telegram остается обязательным gate: при failure сигнал помечается `telegram_failed`.
3. Добавлен backfill закрытых сигналов в `storage/ml/trade_outcomes.jsonl`: background robot loop после lifecycle пишет все еще не залогированные `closed` signals.
4. Добавлен ручной owner endpoint `POST /ml/outcomes/backfill?limit=500`, чтобы дозаписать уже закрытые сделки из БД в JSONL без ожидания нового цикла.
5. Путь JSONL вынесен в `TRADE_OUTCOMES_PATH` с default `storage/ml/trade_outcomes.jsonl`, совпадающим с Docker bind mount `./storage/ml:/app/storage/ml`.

Операционное действие после деплоя: выполнить `POST /ml/outcomes/backfill?limit=500`, затем проверить `GET /ml/outcomes/summary` и файл `storage/ml/trade_outcomes.jsonl` на host.


---

## 6. Roadmap step: Product E2E smoke для Telegram/payment/subscription

Добавлен dry-run owner endpoint `POST /system/product-e2e-smoke`, который переиспользует реальные production services, а не дублирует логику:

1. `TelegramBotMenuService` обрабатывает `/start`;
2. callback `pay:vip_30` создает pending checkout;
3. `BillingService.process_payment_event()` переводит payment в `paid`;
4. `BillingService.confirm_payment()` выдает/продлевает VIP доступ;
5. `CustomerNotificationService` ставит customer notification в Telegram delivery queue;
6. повтор того же provider event проверяет idempotency;
7. `/subscription_status` проверяет, что пользователь видит активную подписку.

Endpoint по умолчанию делает `rollback` (`persist=false`), поэтому подходит для pre-release smoke без создания реального клиента. Если нужен отладочный persist в dev, передать `{"persist": true}`.
