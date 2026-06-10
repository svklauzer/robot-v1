# Phase 5 — Деплой на Render.com

> Цель: запустить robot-v1 в облаке с paper-trading режимом, подготовить к live.
> Репозиторий: https://github.com/svklauzer/robot-v1.git

---

## Архитектура на Render

```
GitHub repo
    │
    ├─ apps/api/     → Render Web Service "robot-api"   (Docker, порт $PORT)
    │                   + Render Disk /app/storage/ml    (1 GB, persistent)
    │                   + Pre-deploy: alembic upgrade head
    │                   + healthCheckPath: /health
    │
    ├─ apps/web/     → Render Web Service "robot-web"   (Docker, порт 3000)
    │
    ├─ Render Postgres   "robot-db"      ← авто-привязка DATABASE_URL
    └─ Render Key Value  "robot-redis"   ← авто-привязка REDIS_URL
```

Всё это описано в `render.yaml` и создаётся одним Blueprint'ом.

**Важно:** все background workers (robot_loop, subscription, telegram delivery,
payment reconciliation, funding arb) запущены в одном uvicorn процессе через
asyncio — отдельного Celery/worker сервиса не нужно.

---

## Предварительные требования

- [ ] Аккаунт на render.com (нужен платный план от **$7/мес** — free tier засыпает
  через 15 минут простоя, недопустимо для торгового бота)
- [ ] GitHub repo подключён к Render: Settings → Connected Accounts → GitHub
- [ ] Telegram бот создан через @BotFather, токен готов
- [ ] HTX API ключи (read-only для shadow, read+write для live)

---

## Шаг 1. Dockerfile'ы (уже готовы)

Оба Dockerfile приведены к production-виду — менять ничего не нужно:

- `apps/web/Dockerfile` — `npm run build` + `npm run start` (production build).
- `apps/api/Dockerfile` — `EXPOSE 8000` + `CMD uvicorn ... --port ${PORT:-8000}`
  (shell-форма, чтобы Render мог подставить свой `PORT`).

---

## Шаг 2. render.yaml (уже в корне репозитория)

`render.yaml` описывает **все** сервисы, включая Postgres и Redis. Ключевое:

- `robot-api` (web, docker, plan `starter`, region `frankfurt`),
  `healthCheckPath: /health`, persistent disk на `/app/storage/ml`,
  `preDeployCommand: sh migrate.sh` (запускает `alembic upgrade head`).
- `robot-web` (web, docker, plan `starter`).
- `robot-redis` (`type: keyvalue`, plan `free`) — создаётся blueprint'ом.
- `robot-db` (Render Postgres, plan `basic-256mb`) — создаётся blueprint'ом.

**Авто-привязка (вручную URL'ы вводить не нужно):**

```yaml
- key: DATABASE_URL
  fromDatabase: { name: robot-db, property: connectionString }
- key: REDIS_URL
  fromService: { name: robot-redis, type: keyvalue, property: connectionString }
```

`core/config.py` принимает `DATABASE_URL` напрямую (с нормализацией
`postgres://` → `postgresql://`); если он пуст — собирает URL из `POSTGRES_*`.

> **Почему `sh migrate.sh`, а не `alembic ... upgrade head` напрямую:** Render
> для Docker-сервисов разбивает `preDeployCommand` по пробелам без обработки
> кавычек, из-за чего alembic терял сабкоманду и падал с `too few arguments`.
> Скрипт `apps/api/migrate.sh` выполняет команду внутри shell — это надёжно.
> `.gitattributes` форсит LF для `*.sh`, иначе sh ломается на CRLF.

> Если меняешь модели — создавай миграцию (см. раздел в конце), иначе
> `preDeployCommand` не подхватит новые таблицы.

---

## Шаг 3. Закоммитить изменения и создать инфраструктуру

### 3.1 Commit + push

```bash
git add render.yaml apps/api/Dockerfile apps/web/Dockerfile \
        apps/api/core/config.py apps/api/migrate.sh .gitattributes
git commit -m "infra: render blueprint fixes (auto-wire db+redis, plans, healthcheck, \$PORT, migrate.sh)"
git push origin main
```

> Уже задеплоился со старым `preDeployCommand`? Просто запушь этот фикс —
> `autoDeploy: true` пересоберёт `robot-api`, и pre-deploy пройдёт.

### 3.2 Blueprint

1. Render Dashboard → **New** → **Blueprint**
2. Выбери репозиторий `svklauzer/robot-v1`
3. Render найдёт `render.yaml` и предложит создать **все** сервисы
   (robot-api, robot-web, robot-redis, robot-db)
4. Нажми **Apply**

Postgres и Redis больше **не** нужно создавать вручную — blueprint их поднимет
и сам пропишет `DATABASE_URL` / `REDIS_URL` в окружение API.

---

## Шаг 4. Настроить секреты в Render Dashboard

`DATABASE_URL` и `REDIS_URL` уже привязаны автоматически. Вручную заполни
только переменные с `sync: false`:

**robot-api → Environment:**

| Переменная | Как получить |
|---|---|
| `JWT_SECRET` | `openssl rand -hex 32` |
| `OWNER_API_TOKEN` | `openssl rand -hex 32` |
| `OWNER_PASSWORD` | сильный пароль |
| `OWNER_EMAIL` | твой email |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_OWNER_CHAT_ID` | твой Telegram user ID |
| `TELEGRAM_FREE_SIGNALS_CHAT_ID` | ID канала |
| `TELEGRAM_VIP_SIGNALS_CHAT_ID` | ID VIP канала |
| `HTX_API_KEY` | HTX → API Management |
| `HTX_API_SECRET` | HTX → API Management |

> Если предпочитаешь раздельные `POSTGRES_*` вместо `DATABASE_URL` — можно, но
> Render не отдаёт host/port Postgres как отдельные свойства, поэтому
> `connectionString` (через `DATABASE_URL`) — самый надёжный путь.

---

## Шаг 5. Первый деплой и проверка миграций

После настройки переменных → **Manual Deploy** → **Deploy latest commit**

В логах деплоя ты увидишь:
```
==> Running preDeployCommand...
INFO  [alembic.runtime.migration] Running upgrade -> 20260530_0001, ...
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, ...
Done. Predeployment succeeded.
==> Starting service...
INFO:     Started server process
INFO:     Application startup complete.
```

Если миграции упали — проверь, что `DATABASE_URL` привязан (robot-api →
Environment → должна быть строка вида `postgresql://...frankfurt-postgres...`).

---

## Шаг 6. Проверить `/system/readiness`

```bash
curl -H "X-Owner-Token: <OWNER_API_TOKEN>" \
  https://robot-api.onrender.com/system/readiness
```

Ожидаемый ответ:
```json
{ "ready": true, "blockers": [], "app_env": "production" }
```

Типичные blockers и решения:

| Blocker | Решение |
|---|---|
| `OWNER_API_TOKEN is not configured` | добавь в Render env |
| `DB_AUTO_CREATE_SCHEMA must be disabled` | убедись `DB_AUTO_CREATE_SCHEMA=false` |
| `JWT_SECRET uses development default` | замени на `openssl rand -hex 32` |

---

## Шаг 7. Зарегистрировать Telegram webhook

```bash
# Заменить <BOT_TOKEN>
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://robot-api.onrender.com/telegram/webhook"}'

curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

Ожидаемо: `"url": "...onrender.com/telegram/webhook"`, `pending_update_count: 0`,
`last_error_date: 0`.

---

## Шаг 8. Kill-switch smoke test

```bash
curl -X POST https://robot-api.onrender.com/system/kill-switch-smoke \
  -H "X-Owner-Token: <OWNER_API_TOKEN>" -H "Content-Type: application/json"
```

Ожидаемо: owner получает Telegram сообщение. Если нет — проверь
`TELEGRAM_OWNER_CHAT_ID` и логи сервиса.

---

## Шаг 9. Проверить работу бота

```bash
curl -H "X-Owner-Token: <OWNER_API_TOKEN>" \
  https://robot-api.onrender.com/bot/state

curl -X POST https://robot-api.onrender.com/system/product-e2e-smoke \
  -H "X-Owner-Token: <OWNER_API_TOKEN>" -H "Content-Type: application/json"
```

E2E smoke должен вернуть все checks = true.

---

## Шаг 10. Настроить мониторинг

### 10.1 Uptime monitor (UptimeRobot / BetterUptime, бесплатно)
- URL: `GET https://robot-api.onrender.com/health`
- Интервал: 5 минут, алерт: email + Telegram при даунтайме

### 10.2 Render алерты
robot-api → **Notifications**: Deploy failed, Service unavailable.

### 10.3 Daily отчёт (опционально)
Render → New → **Cron Job**, schedule `0 8 * * *`:
```bash
curl -X POST https://robot-api.onrender.com/reports/send-owner \
  -H "X-Owner-Token: <token>"
```
Или встроенный scheduled task через Cowork.

---

## Известные особенности и решения

### trade_outcomes.jsonl — persistent disk
Render Disk монтируется в `/app/storage/ml`, env:
```
TRADE_OUTCOMES_PATH=/app/storage/ml/trade_outcomes.jsonl
```
Накопленные локальные данные залей при первом деплое через robot-api → **Shell**.

### IPv6 / Happy Eyeballs
На Render проблема с IPv6 не воспроизводится; `extra_hosts`/`sysctls` из
docker-compose к Render-контейнерам не применяются. Код уже содержит
`httpx.AsyncHTTPTransport(local_address="0.0.0.0")` — этого достаточно.

### DB_AUTO_CREATE_SCHEMA
В production всегда `false`. Схему создают Alembic миграции через
`preDeployCommand`. Новая модель → новая миграция:
```bash
cd apps/api
python -m alembic revision --autogenerate -m "add_new_table"
git add migrations/versions/ && git commit -m "migration: add_new_table"
```

### Бэкап Postgres / Disk
Render Disk не бэкапится автоматически. Render Postgres имеет встроенные
backups на платных планах; дополнительно настрой Cron Job с `pg_dump`.

### Redis (Key Value) на free-плане
`robot-redis` стартует на `free` (25 MB, без персистентности) — этого хватает
для кэша и pub/sub. При росте подними `plan: starter` ($10) для персистентности.

### Обновление NEXT_PUBLIC_API_URL
После создания API Render присвоит URL вида `https://robot-api.onrender.com` —
зафиксируй его в env веб-сервиса.

---

## Чеклист перед выходом в live (после Phase 5)

- [ ] `/system/readiness` → `ready: true`, `blockers: []`
- [ ] Telegram webhook зарегистрирован и работает (`/start` в боте)
- [ ] Kill-switch smoke → owner получил алерт
- [ ] Product E2E smoke → все checks ok
- [ ] Uptime monitor настроен
- [ ] trade_outcomes.jsonl перенесён на disk
- [ ] `OWNER_API_TOKEN`, `JWT_SECRET` — случайные, не дефолтные
- [ ] Validation gates: 200+ closed trades, net PnL > 0 (Phase 2 exit criteria)
- [ ] 7 дней live-shadow без аномалий (Phase 3 exit criteria)

Только после всех галочек → `ROBOT_MODE=live`, `ENABLE_LIVE_ORDERS=true`.

---

## Стоимость инфраструктуры (минимальная конфигурация)

| Сервис | Plan | Цена/мес |
|---|---|---|
| robot-api (Web Service) | Starter | $7 |
| robot-web (Web Service) | Starter | $7 |
| robot-db (Render Postgres) | basic-256mb | $6 |
| robot-redis (Key Value) | Free | $0 |
| ml-storage (Disk) | 1 GB | $0.25 |
| **Итого** | | **~$20/мес** |

> Redis Free (25 MB) подходит для старта. При росте: Postgres → `basic-1gb`,
> Redis → `starter` ($10), API → `standard` (авто-scaling).
