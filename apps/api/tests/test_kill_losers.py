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


# ── Режимы входа ─────────────────────────────────────────────────────────────

def test_losing_regimes_are_not_tradeable():
    """Весь убыток системы сидел в двух режимах.

        trend_up_candidate    n=63  net −55.72  capture −26.4%
        trend_down_candidate  n=91  net −49.54  capture  13.5%
        ───────────────────────────────────────────────────────
        итого                       net −105.26   при общем −101.64
    """
    allowed = {r.strip() for r in settings.TRADEABLE_REGIMES.split(",") if r.strip()}

    assert "trend_up_candidate" not in allowed
    assert "trend_down_candidate" not in allowed
    assert "range" not in allowed, "range: n=25, net −7.05, capture −46.1%"


def test_profitable_regimes_survive():
    """Отключаем убыточное, а не всё подряд."""
    allowed = {r.strip() for r in settings.TRADEABLE_REGIMES.split(",") if r.strip()}

    assert "reversal_long_candidate" in allowed, "n=13, net +9.02, payoff 5.63"
    assert "crt" in allowed, "n=40, net +3.54"
    # Скальп около нуля (−1.89 на 55), но у него лучший capture в системе (37%):
    # его проблема — издержки оборота, а не направление.
    assert "scalp" in allowed


def test_allowlist_is_not_empty():
    """Пустой список означал бы «торговать всё» — ровно наоборот замыслу."""
    allowed = [r.strip() for r in settings.TRADEABLE_REGIMES.split(",") if r.strip()]
    assert allowed, "пустой TRADEABLE_REGIMES отключает фильтр целиком"


# ── Контуры ──────────────────────────────────────────────────────────────────

def test_cross_arb_is_off_after_ten_losses_out_of_ten():
    """10 закрытых позиций — 10 убытков, суммарно −1.50.

    Round-trip 0.20 USDT на позицию 100, carry за срок удержания 0.006–0.18.
    Спред разворачивается раньше, чем окупаются комиссии.
    """
    assert settings.CROSS_FARB_ENABLED is False


def test_grid_kill_switch_overrides_runtime_flag():
    """Сетку нельзя было выключить конфигом: состояние живёт в grid_store.

    185 закрытых циклов, realized −5.74, ни одного периода устойчивого плюса.
    """
    assert settings.GRID_KILL_SWITCH_ENABLED is True


def test_turnover_is_actually_capped_now():
    """Лимит 12 не связывал никогда — фактически 6.2 сделки в сутки.

    43 сделки в неделю против 12.9 у эталонного копи-трейдера с тем же win-rate.
    """
    assert settings.MAX_TRADES_PER_DAY == 3
    assert settings.MAX_TRADES_PER_DAY > 0, "0 отключает предохранитель"


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
