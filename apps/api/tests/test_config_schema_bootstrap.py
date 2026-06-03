from core.config import Settings


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
