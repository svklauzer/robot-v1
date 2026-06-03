import importlib.util
from pathlib import Path


def _load_migration():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "20260530_0001_operational_domains.py"
    spec = importlib.util.spec_from_file_location("operational_domains_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return path, module


def test_operational_domains_migration_is_registered_and_importable():
    path, module = _load_migration()

    assert path.exists()
    assert module.revision == "20260530_0001"
    assert module.down_revision is None
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_operational_domains_migration_covers_new_runtime_tables():
    path, _module = _load_migration()
    text = path.read_text()

    for table_name in [
        "users",
        "bots",
        "subscribers",
        "signals",
        "orders",
        "positions",
        "intelligence_events",
        "audit_events",
        "billing_plans",
        "payments",
        "payment_events",
        "telegram_deliveries",
        "telegram_profiles",
    ]:
        assert f'"{table_name}"' in text


def test_operational_domains_migration_upgrades_fresh_sqlite_database():
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    _path, module = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        module.op = Operations(context)
        module.upgrade()

        inspector = sa.inspect(connection)
        for table_name in [
            "users",
            "bots",
            "subscribers",
            "signals",
            "orders",
            "positions",
            "intelligence_events",
            "audit_events",
            "billing_plans",
            "payments",
            "payment_events",
            "telegram_deliveries",
            "telegram_profiles",
        ]:
            assert inspector.has_table(table_name)
