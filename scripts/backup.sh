#!/usr/bin/env bash
# Nightly backup: Postgres dump + S3 bucket mirror via rclone.
#
# Usage: run as a cron job or from docker-compose-restart. Exits 0
# on success, non-zero if any step fails. Designed to be idempotent
# and safe to re-run.
#
# Environment:
#   PORTAL_BACKUP_DIR    — where to drop pg_dump files (default /backups)
#   PORTAL_KEEP_DAYS     — rotate dumps older than this (default 30)
#   PORTAL_PG_CONTAINER  — docker-compose service name (default postgres)
#   PORTAL_DB_NAME       — database name (default portal)
#   PORTAL_DB_USER       — superuser (default portal)
#   RCLONE_REMOTE        — e.g. b2:portal-backups (must be pre-configured
#                          in `rclone config`); leave empty to skip S3 sync

set -euo pipefail

BACKUP_DIR="${PORTAL_BACKUP_DIR:-/backups}"
KEEP_DAYS="${PORTAL_KEEP_DAYS:-30}"
PG_CONTAINER="${PORTAL_PG_CONTAINER:-postgres}"
DB_NAME="${PORTAL_DB_NAME:-portal}"
DB_USER="${PORTAL_DB_USER:-portal}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "${BACKUP_DIR}"

# ---------- Postgres ----------
DUMP_FILE="${BACKUP_DIR}/portal-${STAMP}.sql.gz"
echo "[backup] Dumping ${DB_NAME} to ${DUMP_FILE}..."
docker compose exec -T "${PG_CONTAINER}" \
    pg_dump --no-owner --no-acl --format=plain -U "${DB_USER}" "${DB_NAME}" \
  | gzip > "${DUMP_FILE}"

# Sanity check — fail if the dump is suspiciously small.
if [ ! -s "${DUMP_FILE}" ] || [ "$(stat -c%s "${DUMP_FILE}")" -lt 1024 ]; then
    echo "[backup] ERROR: dump is empty or tiny" >&2
    exit 1
fi

echo "[backup] Dump size: $(du -h "${DUMP_FILE}" | cut -f1)"

# ---------- Rotation ----------
find "${BACKUP_DIR}" -name 'portal-*.sql.gz' -type f -mtime "+${KEEP_DAYS}" -delete || true

# ---------- Off-site sync ----------
if [ -n "${RCLONE_REMOTE:-}" ]; then
    echo "[backup] Syncing ${BACKUP_DIR} -> ${RCLONE_REMOTE}"
    rclone sync "${BACKUP_DIR}" "${RCLONE_REMOTE}/pg" --progress --retries=3
fi

echo "[backup] Done."
