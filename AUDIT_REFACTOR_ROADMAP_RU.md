# Новый аудит и ROADMAP Robot V1

Дата актуализации: **4 июня 2026 UTC**.
Основание: полный статический проход по репозиторию, существующий `AUDIT_REFACTOR_ROADMAP_RU.md`, `RELEASE_ROADMAP_RU.md`, latest analytics snapshot `analytics_24h/run_20260528T124709Z`, структура API/Web/Telegram/ML/ops и проверка компиляции backend entrypoint.

> Важно: документ не является финансовой рекомендацией и не обещает прибыль. Его цель — синхронизировать код, продуктовую цель и порядок работ так, чтобы робот можно было безопасно довести от paper/live-shadow к ограниченному production.

---

## 1. Цель проекта в текущем виде

Robot V1 — это не просто генератор сигналов. Целевой продукт состоит из пяти связанных контуров:

1. **Trading/intelligence контур** — поиск кандидатов, scoring, quality gates, trade plan, risk/exposure проверки, lifecycle и exit policy.
2. **Safety/readiness контур** — production blockers, live safety, kill-switch, validation gates, exchange reconciliation, live-shadow drift.
3. **Telegram контур** — VIP/FREE доставка сигналов, меню бота, delivery log, retry worker и customer notifications.
4. **Monetization контур** — billing plans, платежи, события, reconciliation, revenue metrics, выдача/продление доступа.
5. **Owner operations контур** — web dashboard, health/readiness pages, analytics, reports, clients/payments/funding panels и runbook.

Главная продуктовая цель на ближайший этап: **сначала сделать надежный управляемый owner/VIP-сервис с прозрачными рисками и safety gates, затем выходить в live только малым лимитом после доказанного edge**.

---

## 2. Что было найдено при текущем аудите

### 2.1 Критичный рассинхрон в `apps/api/main.py` — исправлено

В `main.py` был большой merge/generation drift: одни и те же FastAPI routes были объявлены по 2–4 раза, а часть тел из одного домена была вставлена под decorators другого домена. Примеры рассинхрона до исправления:

- `GET /reports/summary` был продублирован и в одном из дублей возвращал `LiveSafetyService().snapshot(...)`, что относилось к `/system/live-safety`.
- `POST /reports/send-owner`, `/reports/send-free`, `/reports/send-vip` содержали фрагменты kill-switch/readiness логики.
- `POST /subscribers` в одном из дублей содержал readiness response вместо создания подписчика.
- `/telegram/webhook` был объявлен несколько раз.
- Блоки payments/funding/subscribers/reports повторялись несколько раз и делали часть handlers недостижимыми.
- `python -m py_compile apps/api/main.py` падал с `SyntaxError`.
- `.env` расходился с v4 exit-policy/test contract: `MIN_PROTECTIVE_EXIT_PCT=0.60` и `MFE_CAPTURE_PROTECT_SHARE=0.35` ослабляли защиту прибыли относительно кодового default/ожидаемого контракта.

Текущее исправление:

- удален загрязненный повторный slab routes;
- оставлены единичные канонические handlers для reports, subscribers, payments, funding, system readiness, Telegram webhook и delivery summary;
- добавлен regression-test на уникальность `@app.<method>("path")` decorators;
- `main.py` снова компилируется;
- tracked `.env` синхронизирован с v4 protective floor: `MIN_PROTECTIVE_EXIT_PCT=1.20`, `MFE_CAPTURE_PROTECT_SHARE=0.40`.

### 2.2 Документация и входная точка проекта — исправлено

- `README.md` был пустым, из-за чего цель проекта и entrypoints были неочевидны.
- `.gitignore` содержал повторяющиеся `.env` строки.

Текущее исправление:

- добавлен краткий README с назначением проекта, основными entrypoints и предупреждением по release safety;
- `.gitignore` очищен от повторяющейся `.env` строки.

### 2.3 Статус торговой модели по последнему локальному snapshot

Последний доступный snapshot: `analytics_24h/run_20260528T124709Z`.

Ключевые факты из `ml_outcomes_summary.json`:

- `total_rows=90`, `closed_rows=90`;
- `net_pnl_sum=-96.208143`;
- `winrate_pct=45.56`;
- `wins=41`, `losses=49`;
- топ причина закрытия: `failed_setup_exit=47`;
- `protective_breakeven_profit_guard=37`;
- худшие по net PnL символы в этом snapshot: `SOL/USDT`, `AVAX/USDT`, `ETH/USDT`, `XRP/USDT`, `TON/USDT`, `LINK/USDT`, `DOT/USDT`, `BTC/USDT`.

Вывод: readiness/product контуры стали шире, но **торговый edge еще не доказан**. Масштабировать платный трафик с обещанием результата нельзя; допустим только ограниченный soft launch с честным дисклеймером, paper/live-shadow evidence и малыми лимитами.

---

## 3. Где мы находимся относительно старого roadmap

### Уже сделано / частично закрыто

- Есть FastAPI + Next.js + Postgres/Redis инфраструктура.
- Есть Telegram routing, menu service, delivery log/worker и owner test endpoints.
- Есть billing plans, payments, payment events, reconciliation и revenue metrics.
- Есть subscriptions/watchdog, customer notifications и affiliate/trial контуры.
- Есть production/readiness gates: validation gates, live safety, kill-switch, live-shadow drift, exchange reconciliation, market connectivity.
- Есть ML outcome summary и symbol performance tooling.
- Есть funding arbitrage домен с opportunity/position endpoints и tests.
- Есть production runbook и большой набор backend tests.

### Главный текущий шаг

Мы находимся **между старой Фазой 0/1 и стабилизационной Фазой 3**:

- продуктовый и payment контур уже появился;
- Telegram UX уже не нулевой;
- safety/readiness контур уже появился;
- но кодовый drift в `main.py` показывал, что перед любыми новыми фичами нужен **refactor freeze + route/domain synchronization**;
- торговая модель все еще требует улучшения `failed_setup_exit`, MFE capture и symbol policy.

### Что предстоит

1. Закрепить отсутствие рассинхрона tests/CI.
2. Разнести `main.py` на routers/domains без изменения контрактов.
3. Доказать устойчивость Telegram/payment/subscription E2E.
4. Уменьшить торговые причины убытка и зафиксировать live-shadow evidence.
5. Только после этого проводить soft launch.

---

## 4. Новый ROADMAP

## Фаза A — 24–48 часов: Code sync freeze и защита от повторного drift

**Цель:** остановить рассинхрон между доменами и сделать API entrypoint предсказуемым.

**Задачи:**

1. Держать `main.py` компилируемым в каждом PR.
2. Проверять уникальность FastAPI routes тестом.
3. Запретить ручное копирование больших route blocks без теста контрактов.
4. Составить route inventory: `bot`, `signals`, `analytics`, `reports`, `subscribers`, `payments`, `funding-arb`, `system`, `telegram`, `trade`, `intelligence`.
5. Для каждого route указать владельца домена и целевой router module.

**Критерий выхода:** `py_compile`, route uniqueness test и owner endpoint contract проходят; в `main.py` нет повторных decorators одного метода/пути.

---

## Фаза B — 2–4 дня: Разнос `main.py` по routers без изменения API контрактов

**Цель:** убрать главный источник будущего рассинхрона — монолитный `main.py`.

**Порядок выноса:**

1. `reports` → `apps/api/routers/reports.py`.
2. `subscribers` + Telegram delivery summary → `apps/api/routers/subscribers.py` / `telegram.py`.
3. `payments` → `apps/api/routers/payments.py`.
4. `funding-arb` → `apps/api/routers/funding.py`.
5. `system` readiness/live-safety/kill-switch → `apps/api/routers/system.py`.
6. `trade` debug/build-plan/cost-preview → `apps/api/routers/trade.py`.
7. `intelligence` scan/analyze/events/funnel → `apps/api/routers/intelligence.py`.

**Правила:**

- переносить по одному домену за PR;
- сохранять paths, methods, dependencies и response shape;
- после каждого переноса запускать contract tests;
- `main.py` должен остаться bootstrap/lifespan/middleware/include_router точкой.

**Критерий выхода:** `main.py` < 900 строк, все route handlers живут в routers/services, контракт owner endpoints не изменен.

---

## Фаза C — 3–5 дней: Product E2E hardening Telegram + payments + subscriptions

**Цель:** пользователь без ручного вмешательства проходит путь Telegram → тариф → pending payment → подтверждение/сверка → VIP access → reminder/renewal.

**Задачи:**

1. Проверить `/telegram/webhook` на команды `/start`, `/menu`, `/plans`, `/pay`, `/status`, `/help`, `/support`.
2. Зафиксировать сценарии callback keyboard в tests.
3. Добавить E2E-smoke для создания payment event и выдачи/продления subscription.
4. Проверить idempotency ключи платежных событий.
5. Проверить payment reconciliation на stale/pending cases.
6. Проверить customer notifications: success/fail/renewal/reminder/expiration.
7. В UI явно показать payment health и Telegram delivery SLA.

**Критерий выхода:** один automated smoke покрывает путь от Telegram user до активной VIP подписки.

---

## Фаза D — 5–10 дней: Trading quality stabilization

**Цель:** уменьшить системный убыток и доказать, что робот умеет не только находить движение, но и удерживать/фиксировать прибыль.

**Задачи:**

1. Разобрать `failed_setup_exit` по символам, таймингам, spread/latency и режимам рынка.
2. Ужесточить invalidate для setups, которые не подтверждаются быстро.
3. Усилить MFE capture: partial profit, adaptive trailing, breakeven shift, max giveback.
4. Включить symbol policy guard по net PnL/failed setup share, а не только по общему count.
5. Добавить replay comparison старых и новых правил через `symbol_policy_replay`.
6. Ввести daily owner report: `net_pnl`, `failed_setup_exit_share`, `positive_then_negative_share`, `mfe_capture`, `telegram_failed`, `readiness_status`.

**Критерий выхода:** на paper/live-shadow окне минимум 7 дней видно снижение `failed_setup_exit` share и улучшение net PnL trend; readiness gates не блокируют soft launch по качественным причинам.

---

## Фаза E — 3–5 дней: Ops/CI/release discipline

**Цель:** сделать так, чтобы проект можно было сопровождать без ручного угадывания состояния.

**Задачи:**

1. Добавить минимальный CI набор: backend compile/tests, frontend build/typecheck, duplicate route guard.
2. Сделать health/readiness release checklist обязательным перед deploy.
3. Документировать rollback по API/Web/DB migration.
4. Добавить backup/restore smoke в release checklist.
5. Зафиксировать env policy: какие переменные mandatory для paper, live-shadow и live.
6. Разделить debug endpoints и production blockers.

**Критерий выхода:** release можно повторить по runbook; debug endpoints не доступны в production; go/no-go решение основано на `/system/readiness`.

---

## Фаза F — 1–2 недели: Controlled soft launch

**Цель:** проверить продуктовую и операционную нагрузку без агрессивных обещаний прибыли.

**Ограничения:**

- небольшая аудитория;
- честный risk disclaimer;
- paper/live-shadow evidence вместо обещаний;
- дневной лимит VIP сигналов;
- kill-switch и manual override доступны owner.

**KPI:**

- Telegram delivery success ≥ 99%;
- payment event processing/reconciliation success ≥ 99.5%;
- D1/D7 retention по Telegram users;
- refund/support load под контролем;
- торговые quality metrics не деградируют относительно baseline.

**Критерий выхода:** можно расширять аудиторию только если одновременно выполняются product, ops и trading gates.

---

## 5. Backlog MoSCoW

### Must

- Закрепить compile + duplicate route tests.
- Разнести `main.py` по routers.
- E2E smoke Telegram/payment/subscription.
- Daily trading quality report.
- `failed_setup_exit` diagnostics + first policy fix.
- Readiness gate как обязательный release blocker.

### Should

- UI блоки для route/domain health и payment/Telegram SLA.
- Replay-based policy comparison перед изменением live rules.
- Более строгие symbol deny/weight rules.
- Alerting по деградации ML outcomes и stale logs.

### Could

- CRM-lite для support/refund/manual extension.
- Referral/affiliate UX после стабилизации retention.
- AI-summary сделок для клиентов.
- Публичная landing page с прозрачными risk disclosures.

---

## 6. Go/No-Go критерии для live/marketing

**No-Go, если:**

- есть duplicate route decorators или `main.py` не компилируется;
- `/system/readiness` возвращает blockers;
- Telegram delivery failures не объяснены и не восстановлены;
- платежи требуют ручного вмешательства без reconciliation;
- `failed_setup_exit` остается доминирующей причиной убытка без mitigation;
- нет свежего ML outcomes summary или он stale/degraded.

**Go только для soft launch, если:**

- API/Web/Telegram/payment контуры проходят smoke;
- owner может остановить live через kill-switch;
- есть 7-дневный paper/live-shadow отчет;
- risk disclosure явно показан пользователю;
- лимиты и symbol policy включены.

---

## 7. Ближайшие 10 задач

1. Прогнать полный backend test suite и исправить failures после очистки `main.py`.
2. Добавить route inventory doc или auto-report.
3. Начать вынос `reports` routes из `main.py`.
4. Затем вынести `payments` routes и покрыть contract tests.
5. Затем вынести `subscribers` + Telegram delivery summary.
6. Сделать Telegram/payment/subscription E2E smoke.
7. Сформировать daily trading quality report по latest outcomes.
8. Реализовать первую policy-правку против `failed_setup_exit`.
9. Прогнать replay на старом outcomes snapshot.
10. Обновить runbook go/no-go с новым readiness checklist.
