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
    frontend = _frontend_labels("app/signals/page.tsx", "CLOSE_REASON_LABELS")

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
    page = (WEB / "app" / "signals" / "page.tsx").read_text(encoding="utf-8")

    missing = sorted(k for k in kinds if f"{k}:" not in page)
    assert missing == [], f"виды импульса без подписи в UI: {missing}"


def test_shadow_mode_is_visible_to_the_owner():
    """Пока режим shadow, защёлка ничего не решает. Показывать её без этой
    пометки значило бы дать владельцу думать, что механизм уже работает.
    """
    page = (WEB / "app" / "signals" / "page.tsx").read_text(encoding="utf-8")
    assert 'impulse_latch.mode === "shadow"' in page


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
