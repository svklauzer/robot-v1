# Phase 5 — Деплой на Render.com

> Цель: запустить robot-v1 в облаке с paper-trading режимом, подготовить к live.  
> Репозиторий: https://github.com/svklauzer/robot-v1.git

---

## Архитектура на Render

```
GitHub repo
    │
    ├─ apps/api/     → Render Web Service "robot-api"   (Docker, порт 8000)
    │                   + Render Disk /app/storage/ml    (1 GB, persistent)
    │                   + Pre-deploy: alembic upgrade head
    │
    ├─ apps/web/     → Render Web Service "robot-web"   (Docker, порт 3000)
    │
    ├─ Render Managed PostgreSQL "robot-db"
    └─ Render Managed Redis      "robot-redis"
```

**Важно:** все background workers (robot_loop, subscription, telegram delivery,
payment reconciliation, funding arb) запущены в одном uvicorn процессе через
asyncio — никакого отдельного Celery/worker сервиса не нужно.

---

## Предварительные требования

- [ ] Аккаунт на render.com (нужен платный план от **$7/мес** — free tier засыпает
  через 15 минут простоя, недопустимо для торгового бота)
- [ ] GitHub repo подключён к Render: Settings → Connected Accounts → GitHub
- [ ] Telegram бот создан через @BotFather, токен готов
- [ ] HTX API ключи (read-only для shadow, read+write для live)

---

## Шаг 1. Исправить Dockerfile для web (production build)

Текущий `apps/web/Dockerfile` использует `npm run dev` — не подходит для
production. Нужно исправить:

```dockerfile
# apps/web/Dockerfile — замени последние строки:

RUN npm run build

EXPOSE 3000

CMD ["npm", "run", "start"]
```

```bash
git add apps/web/Dockerfile
git commit -m "fix: web Dockerfile uses production build+start for Render"
git push origin main
```

---

## Шаг 2. Добавить render.yaml в корень репозитория

Создай файл `render.yaml` в корне проекта:

```yaml
# render.yaml
# Infrastructure as Code для Render.com
# Docs: https://render.com/docs/infrastructure-as-code

services:

  # ── API (FastAPI + uvicorn) ────────────────────────────────────────────────
  - type: web
    name: robot-api
    runtime: docker
    dockerfilePath: apps/api/Dockerfile
    dockerContext: apps/api
    plan: starter          # $7/мес — меняй на standard при росте нагрузки
    region: frankfurt      # ближе к HTX EU серверам
    branch: main
    autoDeploy: true

    # Миграции запускаются ПЕРЕД каждым деплоем
    preDeployCommand: python -m alembic -c alembic.ini upgrade head

    # Persistent disk для trade_outcomes.jsonl и ML данных
    disk:
      name: ml-storage
      mountPath: /app/storage/ml
      sizeGB: 1             # $0.25/GB/мес

    envVars:
      - key: APP_ENV
        value: production
      - key: DB_AUTO_CREATE_SCHEMA
        value: "false"

      # Secrets — заполняются в Render Dashboard вручную (sync: false)
      - key: JWT_SECRET
        sync: false
      - key: OWNER_API_TOKEN
        sync: false
      - key: OWNER_PASSWORD
        sync: false
      - key: OWNER_EMAIL
        sync: false

      # Database — Render подставит Internal URL автоматически если привязать DB
      - key: POSTGRES_HOST
        sync: false
      - key: POSTGRES_DB
        sync: false
      - key: POSTGRES_USER
        sync: false
      - key: POSTGRES_PASSWORD
        sync: false
      - key: POSTGRES_PORT
        value: "5432"

      # Redis
      - key: REDIS_URL
        sync: false

      # Telegram
      - key: TELEGRAM_BOT_TOKEN
        sync: false
      - key: TELEGRAM_OWNER_CHAT_ID
        sync: false
      - key: TELEGRAM_FREE_SIGNALS_CHAT_ID
        sync: false
      - key: TELEGRAM_VIP_SIGNALS_CHAT_ID
        sync: false

      # HTX
      - key: HTX_API_KEY
        sync: false
      - key: HTX_API_SECRET
        sync: false
      - key: HTX_MARKET_TYPE
        value: spot
      - key: HTX_SYMBOLS
        value: "BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,ADA/USDT,LINK/USDT,AVAX/USDT,DOT/USDT"

      # Trading — PAPER режим для старта
      - key: ROBOT_MODE
        value: paper
      - key: TRADING_MODE
        value: paper_trade
      - key: ENABLE_LIVE_ORDERS
        value: "false"

      # Trade outcomes path — должен совпадать с mountPath диска
      - key: TRADE_OUTCOMES_PATH
        value: /app/storage/ml/trade_outcomes.jsonl

      # Risk (консервативные для старта)
      - key: RISK_EQUITY_USDT
        value: "950"
      - key: RISK_PER_TRADE_PCT
        value: "0.4"
      - key: MAX_OPEN_POSITIONS
        value: "5"
      - key: MAX_DAILY_LOSS_PCT
        value: "3"

  # ── Web (Next.js) ──────────────────────────────────────────────────────────
  - type: web
    name: robot-web
    runtime: docker
    dockerfilePath: apps/web/Dockerfile
    dockerContext: apps/web
    plan: starter
    region: frankfurt
    branch: main
    autoDeploy: true

    envVars:
      - key: NEXT_PUBLIC_API_URL
        value: https://robot-api.onrender.com   # заменить на реальный URL после деплоя

databases:

  # ── PostgreSQL ─────────────────────────────────────────────────────────────
  - name: robot-db
    plan: starter           # $7/мес — 1 GB storage, 97 connection limit
    databaseName: robot
    user: robot
    region: frankfurt
```

```bash
git add render.yaml
git commit -m "infra: add render.yaml blueprint"
git push origin main
```

---

## Шаг 3. Создать инфраструктуру на Render

### 3.1 Через Blueprint (рекомендуется)

1. Render Dashboard → **New** → **Blueprint**
2. Выбери репозиторий `svklauzer/robot-v1`
3. Render найдёт `render.yaml` и предложит создать все сервисы
4. Нажми **Apply**

### 3.2 Redis (вручную — render.yaml не создаёт Redis автоматически)

Render Dashboard → **New** → **Redis**  
- Name: `robot-redis`  
- Plan: Starter ($10/мес) или используй бесплатный Upstash Redis  
- Region: Frankfurt  
- После создания скопируй **Internal Redis URL** → вставь в env `REDIS_URL`

**Upstash альтернатива (бесплатно до 10k req/day):**
```
https://upstash.com → Create Database → Region: EU-West → скопируй redis://... URL
```

---

## Шаг 4. Настроить секреты в Render Dashboard

Перейди: **robot-api** → **Environment** → заполни все переменные с `sync: false`:

| Переменная | Как получить |
|---|---|
| `JWT_SECRET` | `openssl rand -hex 32` |
| `OWNER_API_TOKEN` | `openssl rand -hex 32` |
| `OWNER_PASSWORD` | придумай сильный пароль |
| `OWNER_EMAIL` | твой email |
| `POSTGRES_HOST` | robot-db → Info → **Internal Database URL** (берём только hostname) |
| `POSTGRES_DB` | `robot` |
| `POSTGRES_USER` | `robot` |
| `POSTGRES_PASSWORD` | robot-db → Info → Password |
| `REDIS_URL` | robot-redis → Info → **Internal Redis URL** |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_OWNER_CHAT_ID` | твой Telegram user ID |
| `TELEGRAM_FREE_SIGNALS_CHAT_ID` | ID канала |
| `TELEGRAM_VIP_SIGNALS_CHAT_ID` | ID VIP канала |
| `HTX_API_KEY` | HTX → API Management |
| `HTX_API_SECRET` | HTX → API Management |

> **Совет:** Render показывает Internal URL в формате  
> `postgres://robot:PASSWORD@dpg-xxxxx-a.frankfurt-postgres.render.com/robot`  
> Разбей его на части: host, user, password, dbname.

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

Если миграции упали — проверь `POSTGRES_HOST`, `POSTGRES_PASSWORD`.

---

## Шаг 6. Проверить `/system/readiness`

```bash
curl -H "X-Owner-Token: <OWNER_API_TOKEN>" \
  https://robot-api.onrender.com/system/readiness
```

Ожидаемый ответ при корректной настройке:
```json
{
  "ready": true,
  "blockers": [],
  "app_env": "production"
}
```

Типичные blockers и решения:

| Blocker | Решение |
|---|---|
| `OWNER_API_TOKEN is not configured` | добавь в Render env |
| `DB_AUTO_CREATE_SCHEMA must be disabled` | убедись `DB_AUTO_CREATE_SCHEMA=false` |
| `JWT_SECRET uses development default` | замени на `openssl rand -hex 32` |

---

## Шаг 7. Зарегистрировать Telegram webhook

После того как сервис поднялся и URL известен:

```bash
# Заменить <BOT_TOKEN> и <RENDER_URL>
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://robot-api.onrender.com/telegram/webhook"}'

# Проверить регистрацию:
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

Ожидаемый ответ:
```json
{
  "ok": true,
  "result": {
    "url": "https://robot-api.onrender.com/telegram/webhook",
    "has_custom_certificate": false,
    "pending_update_count": 0,
    "last_error_date": 0
  }
}
```

---

## Шаг 8. Kill-switch smoke test

```bash
# Активировать kill-switch
curl -X POST https://robot-api.onrender.com/system/kill-switch-smoke \
  -H "X-Owner-Token: <OWNER_API_TOKEN>" \
  -H "Content-Type: application/json"
```

Ожидаемо: owner получает Telegram сообщение.  
Если нет — проверь `TELEGRAM_OWNER_CHAT_ID` и логи сервиса.

---

## Шаг 9. Проверить работу бота

```bash
# Статус бота
curl -H "X-Owner-Token: <OWNER_API_TOKEN>" \
  https://robot-api.onrender.com/bot/state

# Product E2E smoke
curl -X POST https://robot-api.onrender.com/system/product-e2e-smoke \
  -H "X-Owner-Token: <OWNER_API_TOKEN>" \
  -H "Content-Type: application/json"
```

E2E smoke должен вернуть все checks = true.

---

## Шаг 10. Настроить мониторинг

### 10.1 Uptime monitor (бесплатно через UptimeRobot или BetterUptime)
- URL: `GET https://robot-api.onrender.com/health`
- Интервал: 5 минут
- Алерт: email + Telegram при даунтайме

### 10.2 Render алерты
Render Dashboard → robot-api → **Notifications**:
- Deploy failed → уведомление
- Service unavailable → уведомление

### 10.3 Автоматический daily отчёт (опционально)
```bash
# Cron через Render: New → Cron Job
# Schedule: 0 8 * * *  (каждый день в 8:00 UTC)
# Command: curl -X POST https://robot-api.onrender.com/reports/send-owner \
#   -H "X-Owner-Token: <token>"
```
Или использовать встроенный scheduled task через Cowork.

---

## Известные особенности и решения

### trade_outcomes.jsonl — persistent disk
Render диск монтируется в `/app/storage/ml`.  
В `.env` (и в Render env) должно быть:
```
TRADE_OUTCOMES_PATH=/app/storage/ml/trade_outcomes.jsonl
```
Текущие данные с локальной машины нужно залить при первом деплое:
```bash
# Через Render Shell (robot-api → Shell):
# Загрузить текущий файл через curl или scp
```

### IPv6 / Happy Eyeballs
На Render (Linux) проблема с IPv6 не воспроизводится — `extra_hosts` и
`sysctls` из docker-compose не применяются к Render-контейнерам.  
Код уже содержит `httpx.AsyncHTTPTransport(local_address="0.0.0.0")` — этого
достаточно.

### DB_AUTO_CREATE_SCHEMA
В production всегда `false`. Схему создают Alembic миграции через
`preDeployCommand`. Если добавляешь новую модель — создавай миграцию:
```bash
cd apps/api
python -m alembic revision --autogenerate -m "add_new_table"
# проверить сгенерированный файл в migrations/versions/
git add migrations/versions/
git commit -m "migration: add_new_table"
```

### Render Disk — backup
Render Disk не делает автоматических бэкапов.  
Настрой cron для pg_dump PostgreSQL через preDeployCommand или отдельный Cron Job.

### Обновление NEXT_PUBLIC_API_URL
После создания API сервиса Render присвоит URL вида  
`https://robot-api.onrender.com` — зафиксируй его в env веб-сервиса.

---

## Чеклист перед выходом в live (после Phase 5)

- [ ] `/system/readiness` → `ready: true`, `blockers: []`
- [ ] Telegram webhook зарегистрирован и работает (`/start` в боте)
- [ ] Kill-switch smoke → owner получил алерт
- [ ] Product E2E smoke → все checks ok
- [ ] Uptime monitor настроен
- [ ] trade_outcomes.jsonl перенесён на disk (сохранены накопленные данные)
- [ ] `OWNER_API_TOKEN`, `JWT_SECRET` — случайные, не дефолтные
- [ ] Validation gates: 200+ closed trades, net PnL > 0 (Phase 2 exit criteria)
- [ ] 7 дней live-shadow без аномалий (Phase 3 exit criteria)

Только после всех галочек → переключить `ROBOT_MODE=live`, `ENABLE_LIVE_ORDERS=true`.

---

## Стоимость инфраструктуры (минимальная конфигурация)

| Сервис | Plan | Цена/мес |
|---|---|---|
| robot-api (Web Service) | Starter | $7 |
| robot-web (Web Service) | Starter | $7 |
| robot-db (PostgreSQL) | Starter | $7 |
| robot-redis (Redis) | Starter | $10 |
| ml-storage (Disk) | 1 GB | $0.25 |
| **Итого** | | **~$31/мес** |

> Upstash Redis (бесплатно до 10k req/day) снизит до ~$21/мес.  
> При росте нагрузки API можно апгрейднуть до Standard ($25/мес) с авто-scaling.
