import shutil
import subprocess
from pathlib import Path

import pytest

def _bash_works() -> bool:
    """Не «есть ли файл bash», а «запускается ли он».

    На Windows `shutil.which("bash")` находит C:\\Windows\\System32\\bash.exe —
    лаунчер WSL. Он существует, но при отсутствии установленного дистрибутива
    падает с `execvpe(/bin/bash) failed: No such file or directory`, и тест
    выглядел как провал контракта скрипта, хотя скрипт не запускался вовсе.
    """
    if shutil.which("bash") is None:
        return False
    try:
        probe = subprocess.run(["bash", "-c", "echo ok"], capture_output=True,
                               text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return False
    return probe.returncode == 0 and "ok" in (probe.stdout or "")


# Тест исполняет .sh — без рабочего bash он не про код, а про среду.
# В CI (Linux) bash есть, контракт скрипта проверяется там.
requires_bash = pytest.mark.skipif(
    not _bash_works(),
    reason="нужен РАБОЧИЙ bash: тест проверяет контракт .sh-скрипта, а не код",
)


def test_production_compose_disables_schema_auto_create_and_runs_migrations():
    compose = Path(__file__).resolve().parents[3] / "docker-compose.prod.yml"
    text = compose.read_text(encoding="utf-8")

    assert "api-migrate" in text
    assert "DB_AUTO_CREATE_SCHEMA" in text
    assert '"false"' in text
    assert "alembic" in text
    assert "upgrade" in text
    assert "service_completed_successfully" in text


def test_production_runbook_documents_migration_and_readiness_flow():
    runbook = Path(__file__).resolve().parents[3] / "docs" / "PRODUCTION_RUNBOOK_RU.md"
    text = runbook.read_text(encoding="utf-8")

    assert "DB_AUTO_CREATE_SCHEMA=false" in text
    assert "api-migrate" in text
    assert "/system/readiness" in text
    assert "Base.metadata.create_all" in text


@requires_bash
def test_backup_restore_smoke_script_has_dry_run_contract():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "db_backup_restore_smoke.sh"

    # check=False намеренно: при check=True падение выглядит как
    # CalledProcessError без текста, и причина (нет pg_dump в PATH, CRLF в
    # скрипте, отсутствующая переменная) остаётся невидимой. Здесь она
    # печатается прямо в сообщении теста.
    result = subprocess.run(
        ["bash", str(script), "--dry-run"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"скрипт вернул {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )

    assert "pg_dump" in result.stdout
    assert "createdb" in result.stdout
    assert "pg_restore" in result.stdout
    assert "dropdb" in result.stdout
    assert "backup_restore_smoke_status=ok" in result.stdout


def test_production_runbook_documents_backup_restore_smoke():
    runbook = Path(__file__).resolve().parents[3] / "docs" / "PRODUCTION_RUNBOOK_RU.md"
    text = runbook.read_text(encoding="utf-8")

    assert "db_backup_restore_smoke.sh" in text
    assert "--dry-run" in text
    assert "backup_restore_smoke_status=ok" in text
