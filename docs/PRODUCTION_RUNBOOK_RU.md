# Production runbook: schema, migrations and readiness

Этот runbook фиксирует production-профиль запуска после ввода Alembic baseline migration.

## Обязательные переменные

Production должен запускаться с явным запретом runtime schema auto-create:

```env
APP_ENV=production
DB_AUTO_CREATE_SCHEMA=false
POSTGRES_DB=robot
POSTGRES_USER=robot
POSTGRES_PASSWORD=<strong-password>
OWNER_API_TOKEN=<long-random-token>
JWT_SECRET=<long-random-secret>
TELEGRAM_BOT_TOKEN=<telegram-token>
TELEGRAM_OWNER_CHAT_ID=<owner-chat-id>
HTX_API_KEY=<key>
HTX_API_SECRET=<secret>
```

Если `APP_ENV=production` и `DB_AUTO_CREATE_SCHEMA=true`, API добавит production blocker и readiness не должен считаться готовым.

## Первый запуск / обновление схемы

Production compose override добавляет одноразовый сервис `api-migrate`, который выполняет:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm api-migrate
```

Для обычного запуска после миграций:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d db redis api web
```

Проверка применяемой конфигурации:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
```

## Readiness gates

Перед включением live-режима проверить:

```bash
curl -s http://localhost:8000/system/readiness
curl -s http://localhost:8000/system/health
```

Ожидаемо:

- `production_readiness.blockers` пустой;
- `live_safety.kill_switch_enabled=false`;
- Telegram delivery SLA соответствует gate;
- market connectivity не блокирует запуск;
- rolling PnL и closed validation sample проходят требования roadmap.


## Backup / restore smoke

Перед Alembic downgrade, крупным deploy или включением live-режима выполнить backup и проверку восстановления в временную БД:

```bash
./scripts/db_backup_restore_smoke.sh
```

Для CI/runbook-проверки без доступа к Docker/Postgres доступен dry-run, который печатает те же `pg_dump`, `createdb`, `pg_restore`, `psql` и `dropdb` команды без выполнения:

```bash
./scripts/db_backup_restore_smoke.sh --dry-run
```

Ожидаемый финальный маркер: `backup_restore_smoke_status=ok`.

## Rollback

1. Включить kill switch через owner endpoint.
2. Остановить `api`/`web`, оставить `db`.
3. Откатить образ/commit.
4. Если требуется откат схемы, выполнить Alembic downgrade только после backup БД.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop api web
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm api-migrate python -m alembic -c alembic.ini downgrade -1
```

## Важно

`Base.metadata.create_all` остаётся только dev/local convenience path. Production startup должен использовать Alembic migrations и `DB_AUTO_CREATE_SCHEMA=false`.
