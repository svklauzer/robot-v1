# Telegram-контур: подписки, оплаты, VIP, affiliate — конспект настройки

> Что покрывает: FREE-тизер → бот → тарифы → оплата Telegram Stars →
> авто-выдача VIP (одноразовый invite) → истечение → возврат к оплате;
> плюс бесплатный 30-дневный VIP за регистрацию по HTX affiliate с авто-проверкой.
>
> API: `https://robot-api-fx9h.onrender.com` · Web: `https://robot-web-0xvm.onrender.com`

---

## 1. Что уже в коде (трогать не нужно)

- **FREE-тизер** ведёт на бота: `https://t.me/<bot>?start=vip` → открывает тарифы.
- **/pay vip_30|vip_90** выставляет счёт в **Telegram Stars**; `pre_checkout_query`
  подтверждается автоматически; на `successful_payment` — активация VIP.
- **Одноразовый invite** в приватный VIP-канал генерится после оплаты и после
  affiliate-триала (`createChatInviteLink`, member_limit=1, TTL).
- **Истечение** подписки: `SubscriptionWatchdog` шлёт клиенту сообщение с кнопками
  «Оформить VIP» / «Продлить» (а не только алерт владельцу).
- **HTX affiliate**: при включённом флаге бот спрашивает HTX UID и проверяет его
  через affiliate-API перед выдачей бесплатных 30 дней.

---

## 2. ENV-переменные (robot-api → Environment)

### Обязательные для Stars + VIP

| Переменная | Значение / откуда взять | sync |
|---|---|---|
| `TELEGRAM_BOT_USERNAME` | username бота без `@` (напр. `finmt_bot`) — для тизера | value |
| `VIP_STARS_PRICE_30` | цена VIP-30 в звёздах (целое XTR) | value |
| `VIP_STARS_PRICE_90` | цена VIP-90 в звёздах (целое XTR) | value |
| `VIP_INVITE_LINK` | статичная invite-ссылка канала (фоллбэк, если бот не админ) | secret |
| `VIP_INVITE_EXPIRE_HOURS` | TTL одноразовой ссылки, по умолчанию `24` | value |
| `TELEGRAM_VIP_SIGNALS_CHAT_ID` | id приватного VIP-канала | secret |
| `TELEGRAM_FREE_SIGNALS_CHAT_ID` | id FREE-канала | secret |
| `TELEGRAM_OWNER_CHAT_ID` | твой Telegram user id (алерты) | secret |
| `TELEGRAM_BOT_TOKEN` | @BotFather | secret |

### Для HTX affiliate авто-триала (опционально)

| Переменная | Значение / откуда взять | sync |
|---|---|---|
| `HTX_AFFILIATE_VERIFY_ENABLED` | `true` чтобы включить проверку UID; `false` = self-claim | value |
| `HTX_AFFILIATE_API_KEY` | API key **affiliate-аккаунта** HTX | secret |
| `HTX_AFFILIATE_API_SECRET` | API secret affiliate-аккаунта | secret |
| `HTX_AFFILIATE_API_HOST` | по умолчанию `api.huobi.pro` | value |
| `HTX_AFFILIATE_INVITEES_PATH` | точный путь эндпоинта со списком рефералов (плейсхолдер!) | value |
| `HTX_AFFILIATE_LINK` | твоя партнёрская ссылка HTX (показывается в `/htx`) | value |
| `AFFILIATE_FREE_VIP_DAYS` | длительность триала, по умолчанию `30` | value |

> Про цену в Stars: курс звезды плавающий. Подбери `VIP_STARS_PRICE_*` под свои
> $49 / $129 по актуальной цене Star на момент запуска.

---

## 3. Настройка в Telegram (без этого invite не выдастся)

1. **Бот — админ приватного VIP-канала** с правом **«Invite Users via Link»**.
   Без этого `createChatInviteLink` падает → используется фоллбэк `VIP_INVITE_LINK`.
2. **Бот — админ FREE-канала** (чтобы постить тизеры).
3. `TELEGRAM_VIP_SIGNALS_CHAT_ID` / `TELEGRAM_FREE_SIGNALS_CHAT_ID` — id именно этих
   каналов (для каналов id вида `-100...`).
4. **Telegram Stars** для цифровых товаров работают без provider-token — отдельная
   платёжная интеграция не нужна.
5. Webhook уже зарегистрирован на `…/telegram/webhook` (при смене токена — заново).

---

## 4. Код, который нужно дополнить (только для HTX-проверки)

Файл `apps/api/services/htx_affiliate.py`:

- **`HTX_AFFILIATE_INVITEES_PATH`** (через env) — реальный путь эндпоинта списка
  приглашённых из affiliate-доков HTX (сейчас плейсхолдер `/v2/affiliate/invitees`).
- **`_extract_uids()`** — разбор ответа под реальные имена полей (заложен
  best-effort поиск `uid` / `userId` / `invitedUid`).
- Подпись запроса (HmacSHA256, SignatureVersion 2) реализована — менять не нужно.
  Если HTX вернёт ошибку подписи — сверь канонизацию (host/path/params) с доками.

Остальное (Stars, invite, expiry) — готово, дополнять не требуется.

---

## 5. Деплой

```bash
git add apps/api render.yaml
git commit -m "feat(telegram): Stars payments, single-use VIP invites, expiry->pay, HTX affiliate verify"
git push origin main
```

Перед пушем (локально, т.к. в этой сессии sandbox-mount подвисал):
```bash
cd apps/api && python -m py_compile core/config.py routers/telegram.py services/htx_affiliate.py services/telegram_bot_menu.py
```

После пуша: autoDeploy пересоберёт robot-api → впиши env-переменные (раздел 2) →
сервис передеплоится. `sync:false` переменные при Blueprint Sync не подставляются —
вводить вручную.

---

## 6. Тест-чеклист после деплоя

**FREE-тизер → бот**
- [ ] В FREE-канале у нового сигнала ссылка `t.me/<bot>?start=vip` кликается и
      открывает бота с тарифами.

**Оплата Stars**
- [ ] `/plans` → кнопка тарифа → приходит счёт в звёздах.
- [ ] Оплата тестовая проходит; в логах robot-api — `stars_payment_confirmed`.
- [ ] Пользователю приходит сообщение с **персональной** invite-ссылкой в VIP.
- [ ] Повторное открытие той же ссылки не пускает второго (member_limit=1).

**Истечение → оплата**
- [ ] У subscriber с истёкшим `expires_at` watchdog ставит `expired` и шлёт
      клиенту сообщение с кнопками «Оформить VIP» / «Продлить».

**HTX affiliate (если включён)**
- [ ] `/htx` → «Я зарегистрировался» → бот просит HTX UID.
- [ ] Верный UID (есть в рефералах) → триал на 30 дней + invite.
- [ ] Чужой UID → отказ `uid_not_in_referrals` с кнопкой «Попробовать снова».
- [ ] Флаг `false` → старое поведение (self-claim), ничего не сломано.

**Регресс**
- [ ] `/system/product-e2e-smoke` → все checks `true`.
- [ ] `/system/readiness` → `ready: true`.

---

## 7. Тарифы (текущие)

| Код | Длительность | Цена USDT | Цена Stars |
|---|---|---|---|
| `vip_30` | 30 дней | $49 | `VIP_STARS_PRICE_30` |
| `vip_90` | 90 дней | $129 | `VIP_STARS_PRICE_90` |
| `affiliate_htx_vip` | 30 дней (триал) | бесплатно | — |
