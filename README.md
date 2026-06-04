# Robot V1

Robot V1 — это экспериментально-продуктовый контур для крипто-сигналов и owner-операций вокруг них:

- **API/backend:** FastAPI, SQLAlchemy, Postgres, Redis, фоновые циклы робота, подписок, доставок Telegram, платежной сверки и funding arbitrage.
- **Trading/intelligence:** генерация кандидатов, quality gates, risk/trade plan, lifecycle сигналов, exit policy, ML outcome statistics и production readiness gates.
- **Telegram/product:** VIP/FREE маршрутизация сигналов, меню бота, подписчики, платежи, уведомления и delivery log.
- **Owner UI:** Next.js dashboard для сигналов, позиций, аналитики, клиентов, платежей, health/readiness и funding arbitrage.
- **Ops:** Docker Compose, миграции Alembic, runbook и скрипты сбора отчетов.

## Главные entrypoints

- `apps/api/main.py` — FastAPI приложение, bootstrap и HTTP endpoints.
- `apps/api/workers/robot_loop.py` — основной торговый цикл.
- `apps/api/services/` — бизнес-сервисы робота, Telegram, платежей, analytics и safety gates.
- `apps/web/app/` — страницы owner dashboard.
- `AUDIT_REFACTOR_ROADMAP_RU.md` — актуальный roadmap аудита/рефакторинга.
- `docs/PRODUCTION_RUNBOOK_RU.md` — эксплуатационный runbook.

## Безопасность релиза

Проект нельзя выпускать в агрессивный live/marketing без прохождения readiness gates: стабильный paper/live-shadow baseline, контролируемый `failed_setup_exit`, надежная Telegram delivery SLA, платежная idempotency/reconciliation и kill-switch.
