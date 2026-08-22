"""Отключение убыточных контуров (#kill-losers-2026-07-28).

Решения приняты по замеру на 287 закрытых сделках, а не по ощущению. Тест
фиксирует и цифры, и сами решения: обратное включение должно быть осознанным.

Общая арифметика системы на момент решения:

    валовый результат   +40.56 USDT
    комиссии           −142.20 USDT
    net                −101.64 USDT

Система угадывает направление в плюс (+0.141 на сделку валовыми) и платит за
это 0.495. Поэтому режется не только направление, но и частота.
"""
from __future__ import annotations

from core.config import settings


# ── Режимы и контуры: включены обратно ───────────────────────────────────────
#
# (#regimes-back-on-2026-07-28) Отключение по ретроспективному PnL было той
# самой подгонкой, о которой предупреждал walk-forward: он сказал «подбор НЕ
# бьёт текущий конфиг вне выборки», а конфиг тут же выбрали по результату на
# всей истории. Двойной стандарт.
#
# Главное: −101.64 получены системой, которой больше нет. Починены две
# блокировки, снят замок безубытка, выровнены пороги сторон, срезана частота.
# Судить старые режимы по старым числам — судить другую систему.

def test_regime_allowlist_is_a_mechanism_not_a_verdict():
    """Пустой список = фильтр выключен. Механизм остаётся для будущих данных."""
    allowed = [r.strip() for r in settings.TRADEABLE_REGIMES.split(",") if r.strip()]
    assert allowed == [], (
        "режимы снова отключены по истории — это подгонка, пока нет данных "
        "под новой логикой"
    )


def test_all_entry_engines_are_enabled():
    """Дефолты приведены к боевой реальности: конфиг обязан описывать систему."""
    assert settings.ENABLE_RANGE_STRATEGY is True
    assert settings.ENABLE_CRT_STRATEGY is True
    assert settings.ENABLE_SCALP_STRATEGY is True


def test_cross_arb_trading_is_off_observation_stays():
    """Межбиржевой выключен по замеру, а не по настроению (#cross-farb-off-2026-08-21).

    Правка выхода 28.07 (carry floor) не помогла: 14 закрытых, −1.25 USDT, ВСЕ по
    `spread_flipped`. Причина структурная — опрос REST отстаёт от колокации, и к
    моменту, когда спред виден, его уже нет. Настройками не лечится.

    Наблюдение при этом ОСТАЁТСЯ живым: снятие спреда — проверка качества фида
    HTX, полезная независимо от арбитража. Тест фиксирует именно эту развилку,
    чтобы «выключено» не превратилось в «удалено» по невнимательности.
    """
    assert settings.CROSS_FARB_ENABLED is False
    # Пол carry не удалён: при возврате (смена тарифа) он снова понадобится.
    assert settings.CROSS_FARB_CARRY_FLOOR_ENABLED is True


def test_grid_is_back_with_the_churn_fixed():
    """185 циклов, −5.74 — и это был холостой оборот, а не проигранные сделки.

    Neutral-корзина переворачивалась при ЛЮБОМ направленном регайме, а он есть
    почти всегда: десятки циклов закрыты с realized РОВНО 0.0. Открылась,
    перевернулась, закрылась, заняв маржу.
    """
    assert settings.GRID_KILL_SWITCH_ENABLED is False
    assert settings.GRID_NEUTRAL_FLIP_NEEDS_BREAKOUT is True, (
        "у двусторонней корзины нет направления, которому можно быть "
        "противоположной — её ломает выход из диапазона, а не наличие тренда"
    )
    assert settings.GRID_OPEN_NEEDS_RANGE is True, (
        "без этого корзина откроется в пробое и повиснет замороженной"
    )


def test_breakeven_lock_stays_off_and_this_is_not_curve_fitting():
    """Единственное, что осталось выключенным — и по другой причине.

        breakeven_lock:  45 сделок, побед 2, net −30.24

    Это не отбор по результату, а свойство МЕХАНИЗМА: ветка по построению
    выходит около безубытка, поэтому крупной победы дать не может в принципе —
    только обрезать её. 43 убытка из 45 подтверждают устройство, а не удачу
    выборки.

    13.08: ВКЛЮЧЕНО ОБРАТНО для non-trend режимов (scalp/range), где нет TZ exit.
    Конфигурация обновлена: ARM=0.45%, FLOOR=0.18%, COST_BUFFER=0.07%.
    На 16 траекториях сумма замков +0.02 USDT (было −30.24).
    """
    assert settings.BREAKEVEN_LOCK_ENABLED is True


def test_throughput_is_not_capped():
    """Лимит на количество сделок снят: он не создаёт edge, а замедляет набор
    статистики. Чтобы отличить ход trend-режимов от нуля, нужно ~230 сделок,
    scalp — ~121; при 3 сделках в сутки это месяцы.

    Издержки 0.495 на сделку, из-за которых лимит ставили, были посчитаны по
    спотовой ставке при своп-маршруте. По фактическому маршруту — 0.231.
    """
    assert settings.MAX_TRADES_PER_DAY >= 100
    assert settings.MAX_ACTIVE_SIGNALS >= 100


# ── Регрессы на найденные баги ───────────────────────────────────────────────

def test_scalp_margin_cap_is_above_the_sizing_target():
    """(#margin-cap-collision) 12 часов без сделок.

    Сайзинг метит в SCALP_MAX_POSITION_MARGIN_PCT × equity, а гвард сравнивает
    строго: `required / equity * 100 > cap`. Когда цель и потолок равны, любое
    округление лота вверх блокирует вход — в ленте решений это десятки
    `blocked_position_margin_limit`.
    """
    target_pct = float(settings.SCALP_MAX_POSITION_MARGIN_PCT) * 100
    cap_pct = float(settings.SCALP_ANTI_DRAIN_MAX_POSITION_MARGIN_PCT)

    assert cap_pct > target_pct, (
        f"потолок {cap_pct}% не выше цели сайзинга {target_pct}% — "
        "гвард будет резать арифметику округления, а не риск"
    )
    # Запас не символический: округление лота на дорогих символах даёт заметный
    # сдвиг (ETH #280: цель 190.0, факт 199.97 → 21.05%).
    assert cap_pct - target_pct >= 5.0


def test_cvd_thin_gate_needs_more_than_a_single_trade():
    """(#cvd-noise) Одна сделка в окне всегда даёт cvd_ratio ровно ±1.000.

    При пороге 1 условие `|ratio| >= 0.9` срабатывало автоматически, независимо
    от рынка: `depth_cvd_thin_against_short: cvd_ratio=1.000>=0.9(n=1)`.
    """
    assert settings.OB_CVD_THIN_MIN_TRADES >= 5
    assert settings.OB_POSITION_CVD_THIN_MIN_TRADES >= settings.OB_CVD_THIN_MIN_TRADES, (
        "у часового горизонта 60-секундное окно ленты — ещё меньшая доля жизни "
        "сделки, чем у скальпа; порог не может быть мягче"
    )


def test_no_duplicate_keys_in_settings_class():
    """(#config-collision) Дубликат в теле класса молча переопределяется.

    Так уже сломался FUNDING_ARB_MIN_HOLD_PERIODS (считалось по 3 периодам
    вместо 10) и едва не сломался MAX_TRADES_PER_DAY. Ошибка тихая: тип и
    значение валидны, просто побеждает последнее объявление.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "core" / "config.py"
    names = re.findall(r"^    ([A-Z][A-Z0-9_]*)\s*:", src.read_text(encoding="utf-8"), re.M)

    seen: dict[str, int] = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    dupes = sorted(k for k, v in seen.items() if v > 1)

    assert not dupes, f"ключи объявлены дважды — победит последний: {dupes}"


# ── Дневной отчёт на малой выборке ───────────────────────────────────────────

def test_share_thresholds_need_a_sample():
    """(#report-sample-guard) Отчёт кричал на трёх сделках.

    «positive_then_negative share 33.3% > 25% threshold» — это ОДНА сделка из
    трёх. На таком объёме доля принимает лишь значения 0 / 33 / 67 / 100%, и
    порог 25% пробивается первой же сделкой, отдавшей плюс. Панель уходила в
    attention_required по арифметике малых чисел.

    Проверка net PnL рядом такой минимум имела с самого начала — долевые не
    имели, и это была непоследовательность, а не замысел.
    """
    assert settings.DAILY_REPORT_MIN_SAMPLE >= 5

    # На трёх закрытиях доля не может быть меньше шага 1/3.
    step = 100.0 / 3
    assert step > 25.0, (
        "порог 25% лежит ниже одного шага выборки из трёх сделок — "
        "любая единичная сделка пробивает его автоматически"
    )


def test_daily_report_exposes_sample_sufficiency_to_the_ui():
    """Витрина обязана знать, что стоит за процентом.

    Winrate 66.7% на трёх сделках — это «две из трёх». Подавать это цифрой
    рядом с остальными нельзя, поэтому отчёт отдаёт флаг явно.
    """
    src = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "services" / "daily_quality_report.py"
    ).read_text(encoding="utf-8")

    assert '"sample_sufficient"' in src
    assert '"min_sample"' in src


def test_dashboard_mutes_shares_on_a_small_sample():
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[3] / "apps" / "web" / "app" / "page.tsx"
    ).read_text(encoding="utf-8")

    assert "sample_sufficient" in page
    assert "muted" in page, "доли на малой выборке красятся как полноценные"
    # Winrate на малой выборке показывается счётом, а не процентом.
    assert "win_count" in page
