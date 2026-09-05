from core.config import Settings
from pathlib import Path
import pytest
import yaml


def test_schema_auto_create_enabled_for_development_defaults():
    cfg = Settings(APP_ENV="development", DB_AUTO_CREATE_SCHEMA=True)

    assert cfg.should_auto_create_schema is True


def test_schema_auto_create_disabled_in_production_even_if_env_left_true():
    cfg = Settings(APP_ENV="production", DB_AUTO_CREATE_SCHEMA=True)

    assert cfg.should_auto_create_schema is False
    assert "DB_AUTO_CREATE_SCHEMA must be disabled in production; run Alembic migrations" in cfg.production_blockers()


def test_schema_auto_create_can_be_explicitly_disabled():
    cfg = Settings(APP_ENV="development", DB_AUTO_CREATE_SCHEMA=False)

    assert cfg.should_auto_create_schema is False


def test_capital_leak_entry_gates_enforced_by_default():
    cfg = Settings()

    assert cfg.TREND_TRIGGER_MODE == "enforce"
    assert cfg.TZ_MODE == "enforce"
    assert cfg.TP_REACH_MODE == "enforce"


def test_dynamic_sizing_uses_capital_by_default():
    cfg = Settings()

    assert cfg.REGIME_EXP_SIZING_ENABLED is False
    assert cfg.DYNAMIC_MARGIN_FAIR_SHARE is False
    assert cfg.DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE == 1.0


def _api_env() -> dict:
    repo_root = Path(__file__).resolve().parents[3]
    with (repo_root / "render.yaml").open(encoding="utf-8") as fh:
        blueprint = yaml.safe_load(fh)
    api = next(s for s in blueprint["services"] if s["name"] == "robot-api")
    return {row["key"]: row.get("value") for row in api["envVars"] if "key" in row}


def _settings_field_names() -> set[str]:
    fields = getattr(Settings, "model_fields", None) or getattr(Settings, "__fields__", {})
    return set(fields.keys())


def test_every_blueprint_key_is_a_real_setting():
    """Каждый ключ blueprint обязан существовать в Settings.

    Pydantic сконфигурирован с extra="ignore": несуществующий ключ НЕ вызывает
    ошибку — он молча игнорируется. Так в render.yaml однажды жил
    MAX_OPEN_POSITIONS, которого в Settings нет: переменная создавала видимость
    лимита позиций, которого в системе не существовало, и никто этого не видел.

    Опечатка в имени даёт ровно тот же эффект — «настроил», а поведение прежнее.
    Тест переводит этот класс ошибок из молчаливых в громкие.
    """
    known_infra = {
        # Ключи, которые задаёт платформа/докер, а не наш Settings.
        "PORT", "PYTHON_VERSION", "NODE_VERSION",
    }
    unknown = sorted(set(_api_env()) - _settings_field_names() - known_infra)

    assert not unknown, (
        "В render.yaml есть ключи, которых нет в Settings — pydantic их молча "
        f"проигнорирует, настройка будет фикцией: {unknown}"
    )


def test_trend_tp2_geometry_constants_are_declared():
    """(#audit-2026-08-27) TREND_TP1_R_MULT/TREND_TP2_R_MULT/TREND_TP1_FLOOR_PCT/
    TREND_TP2_FLOOR_PCT читались только через strategy_profiles._f(name, default)
    — getattr с дефолтом — и НЕ были объявлены в Settings. Из-за extra="ignore"
    любой render.yaml/.env override этих имён молча отбрасывался: настройка
    была фикцией. Регрессия: эти четыре поля обязаны существовать на Settings."""
    names = _settings_field_names()
    for name in (
        "TREND_TP1_R_MULT", "TREND_TP2_R_MULT",
        "TREND_TP1_FLOOR_PCT", "TREND_TP2_FLOOR_PCT",
    ):
        assert name in names, f"{name} must be declared on Settings"


def test_trend_tp2_geometry_recalibrated_to_reachable_distance():
    """(#audit-2026-08-27, часть 2) TREND_TP2_R_MULT/FLOOR_PCT снижены с
    3.2/2.4 до 2.0/2.0. Живые данные 27.08: гейт tp_reachability (enforce,
    переписан 24.08) требовал ~25% hit rate, а фактический tp2_hit_rate для
    trend_up_candidate был 0.0 на выборке 96 сделок — TP2=risk×3.2 давал
    6-12% дистанции при median_mfe_pct~0.55%, практически недостижимую цель,
    что и держало почти все входы. Регрессия: держим геометрию в разумных
    пределах и не даём ей случайно уехать обратно к недостижимой.
    """
    cfg = Settings()
    assert cfg.TREND_TP2_R_MULT == pytest.approx(2.0)
    assert cfg.TREND_TP2_FLOOR_PCT == pytest.approx(2.0)
    # Инвариант TP1<TP2 (см. комментарий у TP1_MAX_PCT) не должен ломаться.
    assert cfg.TP1_MAX_PCT < cfg.TREND_TP2_FLOOR_PCT


def test_crt_tp2_dynamic_enabled_after_live_evidence():
    """(#audit-2026-08-28) Живые данные 28.08: CRT-сетапы (score 61-92,
    mss+fvg подтверждены после возврата CRT_LTF_CONFIRM=either) валились на
    net_rr_blended_too_low с blended RR 0.90-0.99 против порога 1.10 —
    не хватало 0.1-0.2, почти целиком из-за консервативного CRT_TP2_RR=1.5
    (render.yaml). Включено осознанно: механизм только расширяет RR вверх
    от базового, никогда не сужает — не может ухудшить уже отклонённые
    сетапы, может протолкнуть часть из них выше порога. Регрессия: не даём
    флагу случайно откатиться назад на False."""
    cfg = Settings()
    assert cfg.CRT_TP2_DYNAMIC_ENABLED is True


def test_dynamic_tp2_settings_are_declared_and_trend_range_off_by_default():
    """(#trend-tp2-dynamic-2026-08-27 / #crt-tp2-dynamic / #range-tp2-dynamic)
    Новые флаги динамического TP2 должны существовать на Settings (иначе любой
    override — фикция, тот же класс бага, что и в тесте выше). TREND и RANGE
    остаются выключены по умолчанию — раскатка по одному, осознанно, после
    живых данных (CRT включён отдельным тестом выше по той же логике)."""
    cfg = Settings()
    names = _settings_field_names()
    for name in (
        "TREND_TP2_DYNAMIC_ENABLED", "TREND_TP2_DYNAMIC_TF",
        "TREND_TP2_DYNAMIC_MAX_R_MULT", "TREND_TP2_DYNAMIC_ADX_BASE",
        "TREND_TP2_DYNAMIC_ADX_SPAN", "TREND_TP2_DYNAMIC_ATR_EXP_SPAN",
        "TREND_TP2_DYNAMIC_KAMA_SPAN_ATR", "TREND_TP2_DYNAMIC_W_ADX",
        "TREND_TP2_DYNAMIC_W_ATR", "TREND_TP2_DYNAMIC_W_KAMA",
        "CRT_TP2_DYNAMIC_ENABLED", "CRT_TP2_DYNAMIC_MAX_RR",
        "CRT_TP2_DYNAMIC_ADX_BASE", "CRT_TP2_DYNAMIC_ADX_SPAN",
        "CRT_TP2_DYNAMIC_ATR_EXP_SPAN",
        "RANGE_TP2_DYNAMIC_ENABLED", "RANGE_TP2_DYNAMIC_MIN_BUFFER",
        "RANGE_TP2_DYNAMIC_ADX_BASE", "RANGE_TP2_DYNAMIC_ADX_SPAN",
    ):
        assert name in names, f"{name} must be declared on Settings"

    assert cfg.TREND_TP2_DYNAMIC_ENABLED is False
    assert cfg.RANGE_TP2_DYNAMIC_ENABLED is False
    # CRT_TP2_DYNAMIC_ENABLED — see test_crt_tp2_dynamic_enabled_after_live_evidence above.


def test_range_and_crt_undeclared_field_bug_is_fixed():
    """(#audit-2026-08-27) RANGE_CONFIRMED_ONLY / CRT_REQUIRE_TREND_ALIGN были
    читаны через strategy_profiles._b(name, True) без объявления в Settings —
    тот же класс бага, что уже однажды случился с ENABLE_SCALP_STRATEGY."""
    names = _settings_field_names()
    assert "RANGE_CONFIRMED_ONLY" in names
    assert "CRT_REQUIRE_TREND_ALIGN" in names
    cfg = Settings()
    assert cfg.RANGE_CONFIRMED_ONLY is True
    assert cfg.CRT_REQUIRE_TREND_ALIGN is True


def test_render_blueprint_enforces_capital_leak_entry_gates():
    repo_root = Path(__file__).resolve().parents[3]
    with (repo_root / "render.yaml").open(encoding="utf-8") as fh:
        blueprint = yaml.safe_load(fh)

    api = next(s for s in blueprint["services"] if s["name"] == "robot-api")
    env = {row["key"]: row.get("value") for row in api["envVars"] if "key" in row}

    assert env["TREND_TRIGGER_MODE"] == "enforce"
    assert env["TZ_MODE"] == "enforce"
    # (#tp-reach-shadow-2026-09-06) Здесь стоял `enforce`, и тест был прав: гейт
    # достижимости стережёт утечку капитала, и молча разоружить его нельзя.
    # 06.09 он отпущен НАМЕРЕННО и на время.
    #
    # Гейт судит по достижению TP2, а сделка платится через TP1 и трейл: замер
    # 04.09 дал +0.98R у дошедшей до TP1 против −0.83R у недошедшей. Он держал
    # 500+ блокировок в сутки при нуле сделок, и проверить его правоту можно
    # только на исходах тех сделок, которые он останавливал. Вердикт при этом
    # продолжает писаться в план (`tp_reach.would_block`), так что сравнение
    # делается по нему, а не по памяти.
    #
    # Возврат в `enforce` — после разреза stop-forensics по would_block. Строка
    # остаётся утверждением, а не дырой: сменить режим без правки теста и этого
    # объяснения по-прежнему нельзя.
    assert env["TP_REACH_MODE"] == "shadow"
    # 24.08.2026: TP_REACH_MAX_RATIO снят — он и создавал безусловный замок
    # (0.6 / 1.5 = 0.4%: пара с медианой ниже блокировалась навсегда).
    assert "TP_REACH_MAX_RATIO" not in env
    assert env["TP_REACH_EV_MARGIN"] == "1.0"
    assert env["REGIME_EXP_SIZING_ENABLED"] == "false"
    assert env["DYNAMIC_MARGIN_FAIR_SHARE"] == "false"
    assert env["DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE"] == "1.0"
