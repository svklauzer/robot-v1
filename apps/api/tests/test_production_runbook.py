import subprocess
from pathlib import Path


def test_production_compose_disables_schema_auto_create_and_runs_migrations():
    compose = Path(__file__).resolve().parents[3] / "docker-compose.prod.yml"
    text = compose.read_text()

    assert "api-migrate" in text
    assert "DB_AUTO_CREATE_SCHEMA" in text
    assert '"false"' in text
    assert "alembic" in text
    assert "upgrade" in text
    assert "service_completed_successfully" in text


def test_production_runbook_documents_migration_and_readiness_flow():
    runbook = Path(__file__).resolve().parents[3] / "docs" / "PRODUCTION_RUNBOOK_RU.md"
    text = runbook.read_text()

    assert "DB_AUTO_CREATE_SCHEMA=false" in text
    assert "api-migrate" in text
    assert "/system/readiness" in text
    assert "Base.metadata.create_all" in text


def test_backup_restore_smoke_script_has_dry_run_contract():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "db_backup_restore_smoke.sh"

    result = subprocess.run(
        ["bash", str(script), "--dry-run"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "pg_dump" in result.stdout
    assert "createdb" in result.stdout
    assert "pg_restore" in result.stdout
    assert "dropdb" in result.stdout
    assert "backup_restore_smoke_status=ok" in result.stdout


def test_production_runbook_documents_backup_restore_smoke():
    runbook = Path(__file__).resolve().parents[3] / "docs" / "PRODUCTION_RUNBOOK_RU.md"
    text = runbook.read_text()

    assert "db_backup_restore_smoke.sh" in text
    assert "--dry-run" in text
    assert "backup_restore_smoke_status=ok" in text
