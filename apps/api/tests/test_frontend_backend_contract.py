"""Контракт фронт↔бэк: коды, которые отдаёт API, должны иметь ярлык в UI.

Мотивация (#fe-sync-2026-07-25): каждый раунд правок добавлял новые
`close_reason` / `decision`, а фронт узнавал о них вручную — и показывал сырой
код (`trend_capture_band`, `reentry_adverse_price`). Ошибка тихая: страница не
падает, просто владелец видит машинный идентификатор вместо смысла.

Тест дешёвый и статический — читает исходники, ничего не запускает.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[1]   # apps/api
WEB = API.parent / "web"                    # apps/web

# Причины, которые НЕ попадают в Signal.closed_reason и ярлыка не требуют.
NOT_A_CLOSE_REASON = {
    "tp1_partial",   # причина частичного закрытия позиции, а не закрытия сделки
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _backend_close_reasons() -> set[str]:
    """Все строковые литералы reason=... из exit-контура."""
    reasons: set[str] = set()
    for name in ("services/exit_policy.py", "services/signal_lifecycle.py"):
        src = _read(API / name)
        reasons |= set(re.findall(r'reason="([a-z0-9_]+)"', src))
        # динамические: reason = "a" if cond else "b"
        for line in re.findall(r'reason = "([a-z0-9_]+)" if .* else "([a-z0-9_]+)"', src):
            reasons |= set(line)
    return reasons - NOT_A_CLOSE_REASON


def _frontend_labels(page: str, dict_name: str) -> set[str]:
    path = WEB / page
    if not path.exists():
        pytest.skip(f"нет фронтенда по пути {path}")
    src = _read(path)
    start = src.index(dict_name)
    body = src[start : src.index("};", start)]
    return set(re.findall(r"^\s{2,}([a-z0-9_]+):", body, re.M))


def _rendered(page: str) -> str:
    """Текст страницы без комментариев (#analytics-audit-2026-09-05).

    Проверки вида «страница больше не обещает X» трижды подряд падали на моих же
    комментариях, объясняющих, почему X убрали. Тест обязан смотреть на то, что
    видит пользователь: комментарий — объяснение для читателя кода, а не
    содержимое экрана. Иначе выбор такой: либо не писать причину рядом с
    правкой, либо ослабить проверку. Оба варианта хуже, чем убрать комментарии
    из сравнения.
    """
    import re as _re

    without_jsx = _re.sub(r"\{/\*.*?\*/\}", "", page, flags=_re.S)
    without_block = _re.sub(r"/\*.*?\*/", "", without_jsx, flags=_re.S)
    return _re.sub(r"^\s*//.*$", "", without_block, flags=_re.M)


def test_every_close_reason_has_a_ui_label():
    backend = _backend_close_reasons()
    frontend = _frontend_labels("lib/closeReasons.ts", "CLOSE_REASON_LABELS")

    missing = sorted(backend - frontend)
    assert not missing, (
        "бэкенд отдаёт close_reason без ярлыка в UI — владелец увидит сырой код: "
        f"{missing}"
    )


def test_new_exit_and_guard_codes_are_labelled_in_intelligence_feed():
    """Лента решений показывает decisionLabel(); новые коды раунда 25.07
    должны быть в карте, иначе в ленте будет машинный идентификатор."""
    src = _read(WEB / "app/intelligence/page.tsx") if (WEB / "app/intelligence/page.tsx").exists() else None
    if src is None:
        pytest.skip("нет фронтенда")

    for code in ("trend_capture_band", "reentry_adverse_price", "reentry_cooldown_active"):
        assert f"{code}:" in src, f"decisionLabel не знает код {code}"


def _intelligence_labels() -> set[str]:
    return _frontend_labels("app/intelligence/page.tsx", "const map: Record<string, string>")


def test_every_setup_comment_code_has_a_ui_label():
    """(#ui-audit-2026-09-03) Реальное сравнение множеств вместо трёх кодов.

    Прежний тест проверял три захардкоженных имени и пропустил 22 кода, каждый
    из которых встречается в боевых выгрузках. Ошибка тихая: страница не падает,
    владелец видит `range_pos_long_too_high(0.86>0.60)` вместо смысла.

    Источник истины — `comment = "..."` из market_intelligence: именно эти
    строки уезжают в `decision` события и рисуются в ленте.
    """
    src = _read(API / "services/market_intelligence.py")
    codes = set(re.findall(r'comment = f?"([a-z0-9_]+)', src))
    assert codes, "не нашёл ни одного comment-кода — тест устарел вместе с кодом"

    missing = sorted(codes - _intelligence_labels())
    assert not missing, (
        "решение сетапа без ярлыка в ленте — владелец увидит машинный код: "
        f"{missing}"
    )


def test_decision_label_strips_the_inline_detail():
    """Коды несут деталь прямо в строке: `anti_chop_no_trend(fan_atr=-0.61<0.80)`,
    `range_pos_long_too_high(0.86>0.60)`, `depth_spread_too_wide:0.122>0.12`.

    Поиск точным совпадением такие коды не находил НИКОГДА — включая depth_*,
    у которых ярлыки заведены с комментарием «могут иметь :значение». Ярлык
    существовал, совпадение не наступало. Без нормализации любой новый ярлык
    для такого кода снова окажется мёртвым.
    """
    src = _read(WEB / "app/intelligence/page.tsx")

    assert "function normalizeDecision" in src, "нормализация кода пропала"
    assert "map[base]" in src, "decisionLabel снова ищет только точное совпадение"
    # Цвет бейджа тоже обязан считаться по базовому коду, иначе блокирующее
    # решение красится нейтральным по умолчанию.
    assert "normalizeDecision(rawDecision).base" in src, (
        "DecisionBadge снова сравнивает сырую строку — цвет будет неверным"
    )


def test_backend_exposes_honest_pnl_fields_the_ui_reads():
    """UI показывает честный PnL и счётчик фантомов — поля обязаны существовать.

    Раньше дашборд читал только `total_net_pnl_usdt`, завышенный фантомными
    филлами: главная карточка показывала прибыль, которой не было.
    """
    analytics = _read(API / "routers/analytics.py")
    gates = _read(API / "services/validation_gates.py")

    assert "total_net_pnl_honest_usdt" in analytics
    assert "phantom_fill_count" in analytics or "summarize_phantom" in analytics
    assert "net_pnl_honest_usdt" in gates
    assert "no_phantom_fills_in_sample" in gates

    web_analytics = WEB / "app/analytics/page.tsx"
    if web_analytics.exists():
        ui = _read(web_analytics)
        assert "total_net_pnl_honest_usdt" in ui, "дашборд всё ещё показывает сырой PnL"
        assert "net_pnl_honest_usdt" in ui, "Profit gates показывают не ту цифру, по которой судит гейт"
        assert "phantom_fill_count" in ui


def test_every_egress_verdict_has_a_ui_label():
    """(#egress-monitor-2026-07-26) Вердикты монитора решают, КУДА идти.

    `exchanges_unreachable` → менять хост/прокси; `egress_down` → проблема на
    стороне платформы, и трогать HTX_API_HOSTNAME бесполезно. Разница между
    ними стоила нам дня разбирательств — сырой код на экране её стирает.
    """
    src = _read(API / "services/egress_monitor.py")
    verdicts = set(re.findall(r'verdict = "([a-z_]+)"', src))
    assert verdicts, "не нашёл вердиктов в мониторе — тест устарел вместе с кодом"

    health = WEB / "app/health/page.tsx"
    if not health.exists():
        pytest.skip("нет фронтенда")
    labels = _frontend_labels("app/health/page.tsx", "EGRESS_VERDICTS")

    missing = sorted(verdicts - labels)
    assert not missing, f"вердикт монитора без ярлыка в UI: {missing}"


def test_network_diagnostics_are_surfaced_on_health():
    """Обе витрины инцидента 26.07 должны быть на экране, а не только в выгрузке."""
    health = WEB / "app/health/page.tsx"
    if not health.exists():
        pytest.skip("нет фронтенда")
    ui = _read(health)

    assert "/system/egress-history" in ui, "история доступности egress не выведена"
    assert "/system/exchange-diagnostics" in ui, "постадийная диагностика не выведена"
    assert "outage_windows" in ui, "окна недоступности — главное для тикета в поддержку"
    assert "control_hosts" in ui, (
        "без контрольной группы витрина не отличает проблему биржи от проблемы egress"
    )
    # Диагностика блокирующая (DNS+TCP+TLS+HTTP, до 8 с на хост) — она не должна
    # попадать в 5-секундный автополлинг страницы.
    assert "setInterval(runDiagnostics" not in ui and "runDiagnostics, 5000" not in ui, (
        "тяжёлый зонд не должен висеть на автообновлении"
    )


def test_live_safety_trade_counter_is_surfaced():
    """Новый предохранитель MAX_TRADES_PER_DAY должен быть виден на Health."""
    safety = _read(API / "services/live_safety.py")
    assert "trade_count_blocked" in safety and "trades_today" in safety

    health = WEB / "app/health/page.tsx"
    if health.exists():
        ui = _read(health)
        assert "trades_today" in ui, "счётчик сделок за сутки не выведен на Health"
        assert "trade_count_blocked" in ui


def test_grade_badge_is_not_reimplemented_per_page():
    """(#grade-axis-2026-09-04) GradeBadge жил в трёх копиях — signals,
    intelligence, reports, — и все три красили A зелёным, B жёлтым, C красным.
    Замер по 97 сделкам говорит обратное: A значимо убыточен (−0.4257R), B у
    нуля. Правка палитры в одной копии оставила бы две страницы, продолжающие
    врать, — ровно так же, как формула уверенности разъехалась между
    market_intelligence и main.
    """
    shared = WEB / "components" / "GradeBadge.tsx"
    assert shared.exists(), "общий компонент грейда исчез"

    offenders = [
        path.name for path in WEB.rglob("app/**/*.tsx")
        if "function GradeBadge" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"страницы объявляют свой GradeBadge: {offenders}"


def test_grade_badge_does_not_paint_a_verdict():
    """Цвет — это утверждение о результате. Пока ось грейда измерена
    антипредсказательной, ни зелёного «хорошо», ни красного «плохо» на ней быть
    не должно; инвертировать палитру тоже нельзя — B не «хорош», он у нуля.
    """
    source = (WEB / "components" / "GradeBadge.tsx").read_text(encoding="utf-8")

    verdict_colours = [c for c in ("emerald-500", "emerald-800", "yellow-600", "red-700")
                       if c in source]
    assert verdict_colours == [], f"грейд снова покрашен как вердикт: {verdict_colours}"
    assert "title=" in source, "измерение обязано быть в подсказке, а не только в коде"


def test_confidence_legs_written_to_the_plan_are_read_by_the_ui():
    """(#confidence-ratchet-2026-09-04) Разложение уверенности на ноги имеет
    смысл только если владелец его видит: «обе ноги высоки» и «ноги спорят,
    взяли большую» давали одно и то же итоговое число, и именно вторая группа
    наполняла ведро A. Поле в plan_json без места на карточке — телеметрия,
    которую никто не откроет.
    """
    from services.confidence_scale import calibrate

    written = set(calibrate(45.0, 76.0, "approve").as_dict())
    page = (WEB / "app" / "signals" / "page.tsx").read_text(encoding="utf-8")

    for field in ("effective", "base", "setup_leg", "leg_gap"):
        assert field in written, f"{field} перестало писаться в план"
        assert f"conf.{field}" in page, f"{field} пишется в план, но не показано"


def test_every_impulse_kind_has_a_ui_label():
    """(#entry-impulse-2026-09-04) Виды импульса пишутся в план и показываются
    на карточке. Новый вид без подписи выведется сырым кодом — а именно этим
    полем объясняется, почему вход прошёл при падающем ADX.
    """
    from services import entry_impulse_latch as latch

    kinds = {latch.IMPULSE_ADX_TURN, latch.IMPULSE_STOCH_CROSS}
    # Строка защёлки общая для журнала и ленты решений — подписи живут в ней.
    page = _read(WEB / "components/ImpulseLatchLine.tsx")

    missing = sorted(k for k in kinds if f"{k}:" not in page)
    assert missing == [], f"виды импульса без подписи в UI: {missing}"


def test_shadow_mode_is_visible_to_the_owner():
    """Пока режим shadow, защёлка ничего не решает. Показывать её без этой
    пометки значило бы дать владельцу думать, что механизм уже работает.
    """
    page = _read(WEB / "components/ImpulseLatchLine.tsx")
    assert 'latch.mode === "shadow"' in page


def test_scan_silence_codes_are_labelled_in_the_feed():
    """(#scan-visibility-2026-09-05) Код без подписи выводится сырым, а эта
    запись существует ровно затем, чтобы отличить «робот встал» от «рынок не
    даёт сетапов». Сырой `scan_no_candidate` в ленте не отвечает ни на один из
    этих вопросов.
    """
    from services.loop_skip_reporter import (
        DECISION_SCAN_NO_CANDIDATE, DECISION_SCAN_RESUMED,
    )

    page = (WEB / "app" / "intelligence" / "page.tsx").read_text(encoding="utf-8")

    missing = sorted(code for code in (DECISION_SCAN_NO_CANDIDATE, DECISION_SCAN_RESUMED)
                     if f"{code}:" not in page)
    assert missing == [], f"коды молчания без подписи в ленте: {missing}"


def test_backtest_page_shows_which_exit_model_it_replayed():
    """(#replay-partials-2026-09-05) Модель описывала лестницу от 27.07, а живой
    выход с тех пор получил частичную фиксацию на TP1 и на TP2. Инструмент
    выглядел одинаково авторитетно в обоих случаях, и его собственная проверка
    точности уже показывала разрыв 3.81 п.п. при выводе в 0.04.

    Поля модели без места на странице — телеметрия, которую никто не откроет.
    """
    page = (WEB / "app" / "backtest" / "page.tsx").read_text(encoding="utf-8")

    for field in ("exit_model", "tp1_partial_share", "tp2_partial_share",
                  "trades_without_targets", "sources"):
        assert field in page, f"страница не показывает {field}"


def test_backtest_page_shows_the_fidelity_check_it_computes():
    """(#replay-ui-parity-2026-09-05) `_fidelity_verdict` считался с 03.08 и не
    показывался НИ РАЗУ. Он отвечает на вопрос, который стоит прежде всех
    остальных: воспроизводит ли модель факт. На боевых данных разрыв был
    3.81 п.п. при выводе в 0.04 — ошибка в 91 раз больше заключения, — и
    страница про это молчала, показывая таблицу вариантов как готовый совет.
    """
    page = (WEB / "app" / "backtest" / "page.tsx").read_text(encoding="utf-8")

    for field in ("fidelity", "trustworthy", "gap_pct", "best_edge_pct"):
        assert field in page, f"проверка точности модели не показана: {field}"


def test_backtest_page_does_not_restate_the_methodology_the_backend_owns():
    """Заголовок держал вторую копию объяснения и обещал сравнение по gross-%,
    тогда как бэкенд на том же экране объяснял, что формулировка неверна и
    сравнение идёт по чистым. Две копии расходятся молча."""
    page = _rendered((WEB / "app" / "backtest" / "page.tsx").read_text(encoding="utf-8"))
    header = page[:page.index("</header>")]

    assert "gross-%" not in header, "заголовок снова пересказывает методику расчёта"


def test_every_swept_axis_has_a_column():
    """Ось перебора без колонки хуже отсутствующей: в таблице появляются строки
    с одинаковыми видимыми параметрами и одинаковым итогом, и она выглядит
    сломанной. ride_arm_pct стал осью с #band-corridor и колонки не имел."""
    page = (WEB / "app" / "backtest" / "page.tsx").read_text(encoding="utf-8")

    for axis in ("be_arm_pct", "be_floor_pct", "band_arm_pct", "band_giveback_share",
                 "ride_trail_share", "ride_arm_pct", "min_protective_pct"):
        assert f"v.{axis}" in page, f"ось перебора без колонки: {axis}"


def test_both_profiles_explain_themselves_the_same_way():
    """Скальп отдавал меньше полей, чем тренд, и страница показывала «—% на
    сделку» и ни слова о модели. Читатель не обязан помнить, какая из двух
    вкладок одного инструмента честнее."""
    import inspect

    from services import exit_replay

    scalp = inspect.getsource(exit_replay.build)
    trend = inspect.getsource(exit_replay.build_trend)

    for field in ('"exit_model"', '"sources"', '"actual_avg_pct"'):
        assert field in scalp, f"скальп-профиль не отдаёт {field}"
        assert field in trend, f"трендовый профиль не отдаёт {field}"


def test_analytics_shows_the_sample_size_before_the_ratios():
    """(#analytics-audit-2026-09-05) Эндпоинт ожидания возвращает `sample`, а
    страница показывала payoff и winrate без него — ровно ту ловушку, о которой
    предупреждает примечание того же ответа: 67% побед при payoff 0.11 это
    убыточная система. Соседняя страница бэктеста ставит размер выборки первым
    намеренно.
    """
    page = (WEB / "app" / "analytics" / "page.tsx").read_text(encoding="utf-8")

    assert "expectancy.sample" in page, "ожидание показано без размера выборки"
    assert page.index("expectancy.sample") < page.index("payoff_ratio"), (
        "выборка идёт после производных величин"
    )


def test_analytics_does_not_retell_the_backend_note():
    """Примечание к ожиданию приходит с бэкенда и обязано жить в одном месте:
    пересказ во фронте — вторая копия, расходящаяся молча. Ровно так разъехалась
    методика на странице бэктеста."""
    page = (WEB / "app" / "analytics" / "page.tsx").read_text(encoding="utf-8")

    assert "expectancy.note" in page, "примечание бэкенда не показано"


def test_readiness_panel_lives_on_one_page_only():
    """Production readiness была на /analytics и на /health одновременно, причём
    на /health — с сетевой диагностикой и состоянием бирж, то есть с контекстом,
    без которого блокер не разобрать. Две поверхности правды расходятся молча;
    тот же довод убрал отсюда Telegram delivery 28.07.
    """
    analytics = _rendered((WEB / "app" / "analytics" / "page.tsx").read_text(encoding="utf-8"))
    health = (WEB / "app" / "health" / "page.tsx").read_text(encoding="utf-8")

    assert "Production blockers" in health, "перечень блокеров исчез со /health"
    assert "Production readiness" not in analytics, "панель готовности вернулась дублем"


def test_analytics_subtitle_matches_what_the_page_holds():
    """Подзаголовок обещал Telegram delivery ещё полтора месяца после того, как
    панель убрали. Описание, живущее отдельно от содержимого, устаревает молча.
    """
    page = _rendered((WEB / "app" / "analytics" / "page.tsx").read_text(encoding="utf-8"))
    header = page[:page.index("</header>")]

    assert "Telegram" not in header, "подзаголовок снова обещает раздел, которого нет"


def test_the_ml_step_in_confidence_is_visible_not_a_contradiction():
    """(#ml-blend-visible-2026-09-06) Карточка сигнала #475 показывала 71.7 в
    диагностике и 60.67 в шапке — два разных числа под одним словом
    «уверенность». Разница в смешивании с MLScorer (71.7×0.7 + 35×0.3), и
    различить их было нечем: шапка выглядела опечаткой.

    Смешивание идёт независимо от ML_MODE, а уверенность гейтит вход и задаёт
    грейд: у #476 оно опустило 75.8 до 63.54 и сменило грейд с A на B в режиме,
    который обещает «на сделки НЕ влияет». Пока шаг не виден, этого не заметить.
    """
    source = (API / "workers" / "robot_loop.py").read_text(encoding="utf-8")
    page = (WEB / "app" / "signals" / "page.tsx").read_text(encoding="utf-8")

    assert '"ml_blend"' in source, "шаг ML не пишется в план"
    for field in ("before_ml", "ml_confidence", "after_ml", "ml_mode"):
        assert field in source, f"в записи шага нет {field}"

    assert "conf.ml_blend" in page, "шаг ML не показан на карточке"
    assert "after_ml" in page, "итог после ML не показан рядом с шапкой"


# ── разбор главной (07.09) ──────────────────────────────────────────────────

def test_no_page_invents_a_telegram_sla_out_of_silence():
    """(#sla-without-facts-2026-09-07) `sla_pct ?? 100` стоял в трёх местах:
    мини-карточка главной, суточный отчёт и панель на /health. Во всех трёх
    отсутствие доставок печаталось как 100%, а в суточном ещё и красилось
    зелёным — при том, что бэкенд там честно отдаёт null.

    Порог SLA объявлен в required_gates (99%), так что молчащий канал выглядел
    не просто исправным, а проходящим гейт. Отсутствие замера не должно
    выглядеть как измеренное значение.
    """
    offenders = []
    for path in sorted((WEB / "app").rglob("page.tsx")):
        page = _rendered(path.read_text(encoding="utf-8"))
        if re.search(r"sla_pct[^\n]{0,40}\?\?\s*100", page):
            offenders.append(path.relative_to(WEB).as_posix())

    assert offenders == [], f"SLA додумывается до 100% без доставок: {offenders}"


def test_readiness_card_does_not_collapse_paper_ready_into_ready():
    """`ready` — это отсутствие ЖЁСТКИХ блокеров. Бэкенд отдаёт рядом `status`
    с третьим значением `paper_ready` и отдельный список `warnings`; карточка
    сводила всё к READY/BLOCKED и считала одни жёсткие.

    В бумажном режиме это давало зелёное «READY · 0 blockers» при непустых
    предупреждениях — а гейт positive_then_negative держится на 55% при пороге
    25%. Экран сообщал готовность там, где её нет.
    """
    system = (API / "routers" / "system.py").read_text(encoding="utf-8")
    page = _rendered((WEB / "app" / "page.tsx").read_text(encoding="utf-8"))

    assert '"paper_ready"' in system, "у readiness больше нет третьего состояния"
    assert '"warnings": soft_warnings' in system, "мягкие предупреждения не отдаются"

    assert "readiness?.status" in page, "главная снова читает только ready"
    assert "readiness?.warnings" in page, "предупреждения не считаются на главной"
    assert "warnings.length" in page, "счётчик предупреждений не выведен"

    # /health — страница, которой это состояние принадлежит; там та же карточка
    # схлопывала статус ещё дольше.
    health = _rendered(_read(WEB / "app/health/page.tsx"))
    assert "readiness?.status" in health, "/health снова читает только ready"
    assert "warnings.length" in health, "/health не считает предупреждения"


def test_soft_warnings_are_rendered_somewhere():
    """Списка `warnings` не было ни на одном экране: при пустых блокерах
    /health говорил «блокеров нет», хотя предупреждения были непусты. В live
    бэкенд переливает мягкие в жёсткие — то есть это ровно тот список, который
    придётся закрыть перед боем, и не видеть его нельзя.
    """
    health = _rendered((WEB / "app" / "health" / "page.tsx").read_text(encoding="utf-8"))

    assert "warnings.map(" in health, "предупреждения не выводятся на /health"


def test_the_blocker_list_is_enumerated_on_one_page_only():
    """(#ui-audit-2026-09-07) Один и тот же readiness.blockers перечислялся и на
    главной («Go-live blockers»), и на /health («Production blockers»). По
    правилу от 28.07 техническое состояние живёт на /health, деньги в
    /analytics; на главной остаётся счётчик со ссылкой.
    """
    overview = _rendered((WEB / "app" / "page.tsx").read_text(encoding="utf-8"))
    health = _rendered((WEB / "app" / "health" / "page.tsx").read_text(encoding="utf-8"))

    assert "blockers.map(" in health, "список блокеров исчез со своей страницы"
    assert "blockers.map(" not in overview, "перечисление блокеров вернулось на главную"
    assert "blockers.length" in overview, "счётчик блокеров должен остаться"


def test_free_margin_is_computed_and_shown():
    """(#ui-audit-2026-09-07) `exposure` считался на каждый вызов
    /analytics/summary и не показывался нигде: ни свободной маржи, ни потолка.
    Это единственное число, отвечающее «может ли робот вообще открыть», — а на
    главной вместо него стояли два дубля (Signals и Winrate уже есть в подписи
    книги Trend).
    """
    router = (API / "routers" / "analytics.py").read_text(encoding="utf-8")
    page = _rendered((WEB / "app" / "page.tsx").read_text(encoding="utf-8"))

    assert '"free_margin"' in router and '"max_allowed_margin"' in router
    assert "exposure?.free_margin" in page, "свободная маржа снова не показана"

    minicards = page[page.index("lg:grid-cols-5"):page.index("</section>", page.index("lg:grid-cols-5"))]
    for duplicate in ('title="Signals"', 'title="Winrate"'):
        assert duplicate not in minicards, f"дубль вернулся в ряд мини-карточек: {duplicate}"


# ── разбор журнала сигналов (07.09) ─────────────────────────────────────────

def test_the_journal_cannot_book_an_invented_result():
    """(#manual-result-2026-09-07) Кнопки «+2.1%» и «−1.0%» отдавали в
    `/signals/{id}/close` зашитый процент; бэкенд выводит из него цену выхода и
    проводит сделку полным lifecycle. Выдуманный исход попадал в журнал наравне
    с измеренными: в разбор по причинам, в форензику стопов, в
    trade_outcomes.jsonl, на котором учится ML.

    Ручка не под debug-гейтом — в отличие от инъекции цены, — то есть работала
    в production в один клик рядом с настоящим закрытием.
    """
    page = _rendered((WEB / "app" / "signals" / "page.tsx").read_text(encoding="utf-8"))

    # Ярлыки этих причин живут в общем модуле и обязаны остаться: сделки,
    # закрытые так раньше, лежат в журнале. Запрещено ОТПРАВЛЯТЬ их, а не
    # показывать.
    for invented in ("manual_profit_close", "manual_loss_close"):
        assert invented not in page, f"журнал снова умеет вписывать исход: {invented}"
    assert "close-market" in page, "честное закрытие по рынку исчезло"
    assert "manual_cancel" in page, "отмена неоткрытого сигнала не должна была уйти"


def test_the_journal_shows_the_same_honest_pnl_as_the_dashboard():
    """Сводка журнала показывала сырые winrate и net PnL, тогда как главная и
    аналитика — честные. Ветка tp2_reached книжит полную цену TP2, закрываясь
    на 92% пути, и завышение попадает ТОЛЬКО в выигрышные сделки, так что два
    экрана давали два разных числа за один период. На главной это чинили 25.07
    и до журнала не донесли.
    """
    page = _rendered((WEB / "app" / "signals" / "page.tsx").read_text(encoding="utf-8"))

    assert "winrate_honest" in page, "журнал снова показывает сырой winrate"
    assert "total_net_pnl_honest_usdt" in page, "журнал снова показывает сырой PnL"


def test_reachability_line_colours_by_the_verdict_not_by_the_mode():
    """(#shadow-verdict-2026-09-06) В shadow `allowed` всегда true: гейт считает,
    но не блокирует. Строка красилась по нему, поэтому зелёным выходили сделки с
    частотой вдвое ниже требуемой — у #482 17.4% против нужных 29.8%.
    """
    reach = (API / "services" / "tp_reachability.py").read_text(encoding="utf-8")
    page = _rendered((WEB / "app" / "signals" / "page.tsx").read_text(encoding="utf-8"))

    assert "allowed=True if shadow else within" in reach, "гейт больше не так устроен"

    block = page[page.index("Достижимость TP2"):]
    block = block[:block.index("Оставлено на столе")]
    assert "would_block" in block, "строка не показывает собственный вердикт гейта"
    assert "reach.allowed" not in block, "цвет снова берётся из режима, а не из вердикта"


def test_ml_badge_does_not_paint_a_verdict():
    """(#ml-badge-2026-09-07) Значок красил ≥0.6 зелёным, ≥0.45 жёлтым, ниже
    красным. Разделение оси ML на закрытых сделках не измерялось ни разу, а
    GradeBadge в том же интерфейсе уже обесцвечен — там замер показал, что
    палитра была ПЕРЕВЁРНУТА.

    MLScorer при этом подмешивается в уверенность с весом 0.3 независимо от
    ML_MODE: на вход он влияет, а как именно — неизвестно.
    """
    page = (WEB / "app" / "signals" / "page.tsx").read_text(encoding="utf-8")
    badge = page[page.index("function MlBadge"):]
    badge = _rendered(badge[:badge.index("\n}\n")])

    verdict_colours = [c for c in ("emerald-600", "yellow-600", "red-700") if c in badge]
    assert verdict_colours == [], f"значок ML снова красит вердикт: {verdict_colours}"
    assert "title=" in badge, "измерение обязано быть в подсказке"


# ── разбор ленты решений (07.09) ────────────────────────────────────────────

def _backend_decisions() -> set[str]:
    """Коды решений, которые боевой контур реально пишет в ленту."""
    codes: set[str] = set()
    for name in ("core/decision_codes.py", "services/loop_skip_reporter.py"):
        codes |= set(re.findall(r'^DECISION_[A-Z_0-9]+ = "([a-z0-9_]+)"',
                                _read(API / name), re.M))
    loop = _read(API / "workers/robot_loop.py")
    codes |= set(re.findall(r'decision="([a-z0-9_]+)"', loop))
    codes |= set(re.findall(r'"decision":\s*"([a-z0-9_]+)"', loop))
    # Пропуски цикла: причина уходит в поле decision как есть.
    codes |= set(re.findall(r'reason="(loop_skip_[a-z0-9_]+)"', _read(API / "main.py")))
    return codes


def _intelligence_allowlist() -> set[str]:
    src = _read(WEB / "app/intelligence/page.tsx")
    start = src.index("const IMPORTANT_DECISIONS")
    return set(re.findall(r'"([a-z0-9_]+)"', src[start : src.index("];", start)]))


def test_the_decisions_feed_shows_every_code_the_loop_writes():
    """(#decision-allowlist-2026-09-07) IMPORTANT_DECISIONS — белый список, и
    отсутствующий код не «показывается без ярлыка»: событие отфильтровывается
    ЦЕЛИКОМ, до всякой отрисовки.

    Список отставал уже дважды. Пометка 27.08 в самом файле фиксирует первый
    раз; во второй в него не попали обе оси молчания — при том, что ярлыки для
    них написали ещё 04–05.09, и они лежали мёртвыми, пока список их съедал.
    Ради этих событий лента и заводилась: 04.09 система молчала семь часов, и с
    экрана это было неотличимо от остановки робота.

    Поэтому словарь берётся из бэкенда, а не поддерживается руками.
    """
    missing = sorted(_backend_decisions() - _intelligence_allowlist())

    assert missing == [], f"лента не покажет эти решения вовсе: {missing}"


def test_every_decision_code_reads_as_words_not_as_an_identifier():
    """Фолбэк `decisionLabel` — сам код, поэтому непокрытое решение выводится
    машинным идентификатором. `stop_loss`, `tp2_reached` и `position_opened`
    стояли в белом списке без ярлыка и печатались как есть — на соседней
    странице те же исходы были подписаны по-русски.
    """
    src = _read(WEB / "app/intelligence/page.tsx")
    start = src.index("function decisionLabel")
    own = set(re.findall(r"^\s{2,}([a-z0-9_]+):", src[start : src.index("};", start)], re.M))
    shared = _frontend_labels("lib/closeReasons.ts", "CLOSE_REASON_LABELS")

    unlabelled = sorted(_backend_decisions() - own - shared)

    assert unlabelled == [], f"решения выводятся машинным кодом: {unlabelled}"


def test_the_close_reason_map_is_not_kept_in_two_copies():
    """Карта причин правится каждым раундом работы над выходами. Пока копий две,
    расхождение неизбежно и молчаливо — ровно как у GradeBadge, жившего в трёх
    экземплярах.
    """
    shared = WEB / "lib/closeReasons.ts"
    assert shared.exists(), "общий модуль ярлыков исчез"

    for page in ("app/signals/page.tsx", "app/intelligence/page.tsx"):
        src = _read(WEB / page)
        assert "closeReasons" in src, f"{page} не читает общий модуль"
        assert "const CLOSE_REASON_LABELS" not in src, f"{page} снова держит свою копию"


def test_silence_events_show_how_long_the_silence_has_held():
    """У молчания вся суть в длительности: «нет кандидатов» минуту и семь часов
    — разные новости. `held_sec` пишется с 06.09, и до этого разбора карточка
    события его не показывала.
    """
    reporter = _read(API / "services/loop_skip_reporter.py")
    page = _rendered(_read(WEB / "app/intelligence/page.tsx"))

    assert '"held_sec"' in reporter, "бэкенд больше не пишет длительность"
    assert "payload.held_sec" in page, "лента снова не показывает длительность молчания"


# ── разбор позиций (07.09) ──────────────────────────────────────────────────

def test_no_screen_guesses_which_exchange_a_trade_belongs_to():
    """(#exchange-default-2026-09-07) Значок биржи подставлял «htx», когда поле
    пустое. Торгует OKX, то есть умолчание давало прямо неверный ответ на
    вопрос, ради которого значок и стоит, — и выглядело замером, хотя означало
    «поля нет». Копий было две, и обе несли одно и то же умолчание.
    """
    offenders = []
    for path in sorted((WEB / "app").rglob("page.tsx")) + sorted((WEB / "components").glob("*.tsx")):
        page = _rendered(path.read_text(encoding="utf-8"))
        if re.search(r'\|\|\s*"htx"', page):
            offenders.append(path.relative_to(WEB).as_posix())

    assert offenders == [], f"биржа додумывается вместо «неизвестно»: {offenders}"


def test_the_exchange_badge_is_not_reimplemented_per_page():
    """Тот же довод, что у GradeBadge: копия расходится молча, и правка в одной
    оставляет остальные врать. Здесь это уже случилось — обе копии несли
    неверное умолчание.
    """
    assert (WEB / "components/ExchangeBadge.tsx").exists(), "общий значок исчез"

    for page in ("app/signals/page.tsx", "app/positions/page.tsx"):
        src = _read(WEB / page)
        assert "components/ExchangeBadge" in src, f"{page} не читает общий значок"
        assert "function ExchangeBadge" not in src, f"{page} снова держит свою копию"


def test_the_empty_positions_state_states_a_fact_not_a_cause():
    """(#frozen-prose-2026-09-07) Пустой список объяснялся фразой, вписанной
    однажды: «робот запущен в paper, но за окно наблюдения новые сделки не дошли
    до open». Она срабатывала при ЛЮБОМ пустом фильтре и утверждала причину,
    которой эта страница не измеряет. 07.09 две сделки открылись и закрылись, а
    текст всё равно сказал бы, что до open ничего не дошло.
    """
    page = _rendered(_read(WEB / "app/positions/page.tsx"))

    for invented in ("за окно наблюдения", "соответствует последнему отчет"):
        assert invented not in page, f"страница снова объясняет причину: {invented}"


def test_a_missing_result_is_not_drawn_as_zero():
    """У закрытой позиции без записанного результата `rowPnl` даёт null, а
    рисовался `?? 0` — зелёный ноль, неотличимый от измеренного безубытка.
    """
    page = _rendered(_read(WEB / "app/positions/page.tsx"))

    assert "rowPnl(p) ?? 0" not in page, "отсутствие результата снова рисуется нулём"
    assert "нет записи" in page, "не видно, что результата в записи нет"


# ── разбор ML-страницы (07.09) ──────────────────────────────────────────────

def _ml_blend_source() -> str:
    """Кусок цикла, где MLScorer подмешивается в уверенность."""
    loop = _read(API / "workers/robot_loop.py")
    start = loop.index("Blend: 70% calibrated")
    return loop[start : loop.index("return calibrated", start)]


def test_the_ml_page_does_not_promise_a_contract_the_loop_breaks():
    """(#ml-blend-contract-2026-09-06) Экран обещал «shadow считает и логирует,
    на сделки НЕ влияет» — в подсказке режима, в заголовке панели и в хвостовой
    приписке. Смешивание MLScorer в уверенность идёт с весом 0.3 БЕЗ проверки
    режима, а уверенность гейтит вход и задаёт грейд.

    Насколько это существенно: у #479 уверенность после этого шага составила
    60.02 при пороге 60.0 — вход состоялся с запасом в две сотых.

    Тест парный намеренно: починить можно с любой стороны. Либо цикл начинает
    смотреть на режим, либо экран перестаёт обещать, что он смотрит. Чего нельзя
    — держать обещание без поведения.
    """
    blend = _ml_blend_source()
    gated = bool(re.search(r"if[^\n]*ML_MODE", blend)) or "ml_controller" in blend

    page = _rendered(_read(WEB / "app/ml/page.tsx"))
    promises = [claim for claim in ("НЕ влияет на сделки", "не влияет на сделки",
                                    "без влияния")
                if claim in page]

    assert not (promises and not gated), (
        f"экран обещает {promises}, а смешивание идёт без проверки режима"
    )


def test_the_ml_step_is_named_where_the_mode_is_chosen():
    """(#ml-blend-contract-2026-09-07) Расхождение закрыто в коде, но полномочия
    режима обязаны быть видны там, где режим и выбирают: слово «shadow» само по
    себе читается как «безопасно наблюдаем», и один раз это уже было неправдой.
    """
    page = _rendered(_read(WEB / "app/ml/page.tsx"))

    assert "full_auto" in page and "ml_blend.applied" in page, (
        "не видно, в каком режиме ML трогает сделки и чем это записано"
    )
    assert "#479" in page, "нет замера, показывающего цену вопроса"


def test_the_blend_asks_the_effective_mode_not_the_setting():
    """Контроллер понижает full_auto и advisory до shadow при слабом или
    протухшем AUC. Спросить `settings.ML_MODE` напрямую значило бы обойти это
    понижение и вернуть ту же дыру под видом полномочий.
    """
    blend = _ml_blend_source()

    assert "effective_mode()" in blend, "режим берётся мимо контроллера"
    assert 'getattr(settings, "ML_MODE"' not in blend, "снова читается настройка напрямую"


def test_the_depth_feed_names_its_own_venue():
    """(#depth-venue-2026-09-07) Стакан читается с HTX — `run_htx_orderbook_feed`,
    адреса huobi/hbdm, ветки OKX в фиде нет вовсе, — а ордера с 02.09 уходят на
    OKX. Стакан при этом не наблюдательный: `OB_GATE_ENTRIES` блокирует по нему
    входы, а `entry_depth.*` уходит в план сделки и дальше в форензику стопов
    как признак входа.

    Пока обе биржи не названы в ответе рядом, расхождение не видно ни на экране,
    ни в разборе, и подзаголовок «Живой стакан HTX» читается как деталь
    оформления, а не как несовпадение площадок.
    """
    feed = _read(API / "services/orderbook_feed.py")
    # 07.09 фид переведён на ACTIVE_EXCHANGE: ветка OKX появилась, но обе
    # площадки остались, и вопрос «чья это книга» стал переменной, а не
    # константой — тем более требующей ответа в телеметрии.
    assert "def feed_exchange" in feed, "фид снова не выбирает биржу"
    assert "run_okx_orderbook_feed" in feed and "run_htx_orderbook_feed" in feed

    main = _read(API / "main.py")
    assert "run_orderbook_feed" in main, "стартует не диспетчер, а одна из веток"
    assert '"feed_exchange"' in main, "ответ не говорит, чей это стакан"
    assert '"active_exchange"' in main, "ответ не говорит, где исполняются ордера"

    page = _rendered(_read(WEB / "app/orderbook/page.tsx"))
    assert "venueMismatch" in page, "экран не сравнивает биржу стакана с биржей ордеров"


# ── разбор Venues (07.09) ───────────────────────────────────────────────────

def test_cross_arb_exit_reasons_reach_the_screen_as_words():
    """(#cross-arb-reasons-2026-09-07) Причины выхода печатались машинным кодом:
    `spread_compressed:2.10<3.0`, а в полосе подтверждения ещё и с хвостом
    `|held_until_carry_covers_fees`. Тот же дефект уже сняли с журнала сигналов,
    ленты решений и отчётов — здесь он оставался последним.

    Коды параметризованные, поэтому словарём их не покрыть: тест сверяет, что
    каждая ветка `exit_reason` разобрана на экране.
    """
    engine = _read(API / "services/cross_funding_arb.py")
    page = _rendered(_read(WEB / "app/venues/page.tsx"))

    emitted = set(re.findall(r'return "([a-z_]+)"', engine[engine.index("def exit_reason"):]))
    emitted.add("spread_compressed")  # f-строка с порогами, литералом не ловится
    assert emitted >= {"max_hold_reached", "spread_flipped"}, "ветки выхода изменились"

    for code in emitted:
        assert code in page, f"причина выхода не разобрана на экране: {code}"
    assert "held_until_carry_covers_fees" in page, "хвост отложенного выхода не разобран"


def test_the_venue_table_colours_by_the_gates_it_prints():
    """Таблица красила по зашитым 12 и 80 — второй копии настроек, которые она
    же показывает чипами выше. Сегодня копия совпадает с дефолтами движка, и
    именно поэтому дефект незаметен: разойдутся они молча, при первой подкрутке.
    """
    page = _rendered(_read(WEB / "app/venues/page.tsx"))

    assert "gates.min_ann_pct" in page, "порог входа снова зашит в таблицу"
    assert "gates.min_stability_pct" in page, "порог устойчивости снова зашит"
    assert ">= 12" not in page and ">= 80" not in page, "константы порогов вернулись"


def test_the_funding_total_inherits_the_caveat_of_its_parts():
    """(#funding-total-inherits-2026-09-07) `realized_pnl` завышен, пока carry
    считается по ставке ВХОДА, а не пер-периодно (0.25–0.60 на сделку). Карточка
    «Realized P&L» это говорит и красит янтарём при measured_trades = 0.

    Соседняя «Total P&L est.» складывает ТО ЖЕ число — и красила зелёным. Две
    карточки из одного источника с противоположным цветом: правая отменяла
    предупреждение левой.
    """
    page = _rendered(_read(WEB / "app/funding/page.tsx"))

    block = page[page.index('title="Total P&L est."'):]
    block = block[:block.index("/>")]

    assert "measured_trades" in block, "итог снова не смотрит на наличие измеренных сделок"
    assert "good={(summary?.measured_trades ?? 0) > 0" in block, (
        "итог снова может позеленеть на оценке"
    )


# ── разбор денежных страниц (07.09) ─────────────────────────────────────────

def test_money_actions_do_not_fail_in_silence():
    """(#silent-actions-2026-09-07) `apiPost` бросает на любом не-2xx. Журнал
    сигналов это ловил и объяснял с 09.07, а страницы подписчиков и платежей —
    нет: там стоял голый `await`. Нажал «продлить на 30 дней», запрос упал —
    экран просто не изменился, и это неотличимо от «продлил, но список не
    обновился».

    Именно на этих двух страницах молчание дороже всего: одна выдаёт платный
    доступ, вторая подтверждает оплаты.
    """
    for page in ("app/clients/page.tsx", "app/payments/page.tsx"):
        src = _rendered(_read(WEB / page))
        assert "lib/apiAction" in src, f"{page} не подключает общий обработчик"

        for call in re.findall(r"await apiPost\(", src):
            pass
        # Каждый apiPost обязан быть под assertOk — голых вызовов не остаётся.
        bare = re.findall(r"(?<!assertOk\()await apiPost\(", src)
        assert bare == [], f"{page}: {len(bare)} действий без проверки ответа"


def test_a_failed_action_does_not_wipe_what_was_typed():
    """Форма очищалась в любом случае: введённые данные пропадали вместе с
    ошибкой, о которой никто не узнал. Ранний `return` в ветке отказа — то, что
    отличает «повтори» от «набирай заново».
    """
    for page in ("app/clients/page.tsx", "app/payments/page.tsx"):
        src = _rendered(_read(WEB / page))
        block = src[src.index("catch (e)"):]
        assert "return;" in block[:200], f"{page}: после ошибки выполнение продолжается"


def test_the_action_handler_lives_in_one_place():
    """Тот же довод, что у GradeBadge и ExchangeBadge: копия расходится молча.
    Здесь копии не было вовсе — две страницы просто остались без обработки.
    """
    assert (WEB / "lib/apiAction.ts").exists(), "общий обработчик исчез"

    signals = _read(WEB / "app/signals/page.tsx")
    assert "function assertOk" not in signals, "журнал снова держит свою копию"
    assert "lib/apiAction" in signals


def test_the_latch_line_names_both_series_the_backend_reports():
    """(#impulse-tf-2026-09-08) ADX импульса и ADX условия — разные ряды:
    событие читается с ENTRY_IMPULSE_TF (15m), а снимает отказ, посчитанный на
    TZ_TREND_TF (1h). В ленте 07.09 в ОДНОЙ записи стояли adx 11.31 у условия и
    16.68 у защёлки — по виду неотличимо от рассогласования данных.

    Замысел был записан только в комментарии к коду; снимок теперь называет оба
    ряда, и строка на экране обязана их показывать — иначе объяснение снова
    остаётся там, куда владелец не смотрит.
    """
    from services.entry_impulse_latch import ImpulseLatch

    latch = ImpulseLatch()
    latch.observe("SOL/USDT", "long", {"15m": {
        "adx14": 30.0, "adx14_prev": 29.0,
        "stoch_rsi_k": 40.0, "stoch_rsi_k_prev": 30.0,
        "stoch_rsi_d": 35.0, "stoch_rsi_d_prev": 35.0,
    }}, now=0.0)
    snap = latch.snapshot("SOL/USDT", "long", now=10.0)

    assert snap["tf"] and snap["substitutes_tf"], "снимок снова не называет ряды"
    assert snap["tf"] != snap["substitutes_tf"], "ряды совпали — проверить настройки"

    line = _rendered(_read(WEB / "components/ImpulseLatchLine.tsx"))
    assert "latch?.tf" in line and "substitutes_tf" in line, "строка не читает ряды"
    assert "adx_rise_min" in line, "порог роста не показан рядом с импульсом"


def test_the_decision_feed_lets_every_close_reason_through():
    """(#decision-allowlist-close-reasons-2026-09-12) Белый список ленты решений
    трижды отставал от бэкенда, и событие без кода в списке отбрасывалось
    целиком. Все причины закрытия берутся из общего модуля ярлыков."""
    src = _read(WEB / "app/intelligence/page.tsx")
    start = src.index("const IMPORTANT_DECISIONS")
    body = src[start: src.index("];", start)]
    assert "...Object.keys(CLOSE_REASON_LABELS)" in body
