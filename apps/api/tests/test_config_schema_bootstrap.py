from core.config import Settings
from pathlib import Path
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


def test_render_blueprint_enforces_capital_leak_entry_gates():
    repo_root = Path(__file__).resolve().parents[3]
    with (repo_root / "render.yaml").open(encoding="utf-8") as fh:
        blueprint = yaml.safe_load(fh)

    api = next(s for s in blueprint["services"] if s["name"] == "robot-api")
    env = {row["key"]: row.get("value") for row in api["envVars"] if "key" in row}

    assert env["TREND_TRIGGER_MODE"] == "enforce"
    assert env["TZ_MODE"] == "enforce"
    assert env["TP_REACH_MODE"] == "enforce"
    assert env["TP_REACH_MAX_RATIO"] == "1.5"
    assert env["REGIME_EXP_SIZING_ENABLED"] == "false"
    assert env["DYNAMIC_MARGIN_FAIR_SHARE"] == "false"
    assert env["DYNAMIC_MARGIN_B_CAP_PCT_OF_FREE"] == "1.0"
