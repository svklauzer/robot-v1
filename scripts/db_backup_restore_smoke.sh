#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

COMPOSE_FILES=${COMPOSE_FILES:-"-f docker-compose.yml -f docker-compose.prod.yml"}
DB_SERVICE=${DB_SERVICE:-db}
POSTGRES_DB=${POSTGRES_DB:-robot}
POSTGRES_USER=${POSTGRES_USER:-robot}
BACKUP_DIR=${BACKUP_DIR:-backups}
RESTORE_DB=${RESTORE_DB:-"${POSTGRES_DB}_restore_smoke"}
TIMESTAMP=${TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}
BACKUP_FILE=${BACKUP_FILE:-"${BACKUP_DIR}/${POSTGRES_DB}_${TIMESTAMP}.dump"}

compose_cmd() {
  # shellcheck disable=SC2086
  docker compose ${COMPOSE_FILES} "$@"
}

print_cmd() {
  printf '+ %s\n' "$*"
}

run_cmd() {
  print_cmd "$*"
  if [[ "${DRY_RUN}" != "true" ]]; then
    eval "$@"
  fi
}

run_compose_exec() {
  local cmd="$1"
  print_cmd "docker compose ${COMPOSE_FILES} exec -T ${DB_SERVICE} sh -lc '${cmd}'"
  if [[ "${DRY_RUN}" != "true" ]]; then
    compose_cmd exec -T "${DB_SERVICE}" sh -lc "${cmd}"
  fi
}

backup_database() {
  run_cmd "mkdir -p '${BACKUP_DIR}'"
  print_cmd "docker compose ${COMPOSE_FILES} exec -T ${DB_SERVICE} sh -lc 'pg_dump -U ${POSTGRES_USER} -Fc -d ${POSTGRES_DB}' > '${BACKUP_FILE}'"
  if [[ "${DRY_RUN}" != "true" ]]; then
    compose_cmd exec -T "${DB_SERVICE}" sh -lc "pg_dump -U '${POSTGRES_USER}' -Fc -d '${POSTGRES_DB}'" > "${BACKUP_FILE}"
  fi
}

restore_smoke() {
  run_compose_exec "dropdb -U '${POSTGRES_USER}' --if-exists '${RESTORE_DB}'"
  run_compose_exec "createdb -U '${POSTGRES_USER}' '${RESTORE_DB}'"
  print_cmd "cat '${BACKUP_FILE}' | docker compose ${COMPOSE_FILES} exec -T ${DB_SERVICE} sh -lc 'pg_restore -U ${POSTGRES_USER} -d ${RESTORE_DB}'"
  if [[ "${DRY_RUN}" != "true" ]]; then
    compose_cmd exec -T "${DB_SERVICE}" sh -lc "pg_restore -U '${POSTGRES_USER}' -d '${RESTORE_DB}'" < "${BACKUP_FILE}"
  fi
  run_compose_exec "psql -U '${POSTGRES_USER}' -d '${RESTORE_DB}' -c 'select count(*) as restored_tables from information_schema.tables where table_schema = '\''public'\'';'"
  run_compose_exec "dropdb -U '${POSTGRES_USER}' --if-exists '${RESTORE_DB}'"
}

backup_database
restore_smoke

printf 'backup_restore_smoke_status=ok backup_file=%s restore_db=%s dry_run=%s\n' "${BACKUP_FILE}" "${RESTORE_DB}" "${DRY_RUN}"
