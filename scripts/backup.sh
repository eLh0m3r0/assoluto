#!/usr/bin/env bash
# Nightly backup: Postgres dump (optionally GPG-encrypted) + off-site
# sync via rclone.
#
# Usage: run as a cron job or from docker-compose-restart. Exits 0
# on success, non-zero if any step fails. Designed to be idempotent
# and safe to re-run.
#
# Environment:
#   PORTAL_BACKUP_DIR      — where to drop pg_dump files (default /backups)
#   PORTAL_KEEP_DAYS       — rotate dumps older than this (default 30)
#   PORTAL_PG_CONTAINER    — docker-compose service name (default postgres)
#   PORTAL_DB_NAME         — database name (default portal)
#   PORTAL_DB_USER         — superuser (default portal)
#   BACKUP_GPG_RECIPIENT   — gpg key id / email to encrypt to. When set,
#                            output is .sql.gz.gpg; pubkey must be in the
#                            local gpg keyring. Empty = unencrypted.
#   RCLONE_REMOTE          — e.g. b2:portal-backups (must be pre-configured
#                            in `rclone config`); leave empty to skip sync.
#
# See docs/BACKUP_RESTORE.md for the operator runbook.

set -euo pipefail

BACKUP_DIR="${PORTAL_BACKUP_DIR:-/backups}"
KEEP_DAYS="${PORTAL_KEEP_DAYS:-30}"
PG_CONTAINER="${PORTAL_PG_CONTAINER:-postgres}"
DB_NAME="${PORTAL_DB_NAME:-portal}"
DB_USER="${PORTAL_DB_USER:-portal}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "${BACKUP_DIR}"

# ---------- Postgres ----------
# If BACKUP_GPG_RECIPIENT is set, pipe through gpg before writing. The
# pipeline is dump | gzip | gpg so the plaintext dump never lands on
# disk — important on a server where /backups might be readable by a
# wider set of operator accounts than the gpg secret-key holder.
if [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
    DUMP_FILE="${BACKUP_DIR}/portal-${STAMP}.sql.gz.gpg"
    echo "[backup] Dumping ${DB_NAME} → ${DUMP_FILE} (encrypted to ${BACKUP_GPG_RECIPIENT})"
    docker compose exec -T "${PG_CONTAINER}" \
        pg_dump --no-owner --no-acl --format=plain -U "${DB_USER}" "${DB_NAME}" \
      | gzip \
      | gpg --batch --yes --trust-model always \
            --recipient "${BACKUP_GPG_RECIPIENT}" \
            --output "${DUMP_FILE}" --encrypt
else
    DUMP_FILE="${BACKUP_DIR}/portal-${STAMP}.sql.gz"
    echo "[backup] Dumping ${DB_NAME} → ${DUMP_FILE} (UNENCRYPTED — set BACKUP_GPG_RECIPIENT)"
    docker compose exec -T "${PG_CONTAINER}" \
        pg_dump --no-owner --no-acl --format=plain -U "${DB_USER}" "${DB_NAME}" \
      | gzip > "${DUMP_FILE}"
fi

# Sanity check — fail if the dump is suspiciously small.
if [ ! -s "${DUMP_FILE}" ] || [ "$(stat -c%s "${DUMP_FILE}")" -lt 1024 ]; then
    echo "[backup] ERROR: dump is empty or tiny" >&2
    exit 1
fi

echo "[backup] Dump size: $(du -h "${DUMP_FILE}" | cut -f1)"

# ---------- Rotation ----------
find "${BACKUP_DIR}" -name 'portal-*.sql.gz' -type f -mtime "+${KEEP_DAYS}" -delete || true
find "${BACKUP_DIR}" -name 'portal-*.sql.gz.gpg' -type f -mtime "+${KEEP_DAYS}" -delete || true

# ---------- Off-site sync ----------
if [ -n "${RCLONE_REMOTE:-}" ]; then
    echo "[backup] Syncing ${BACKUP_DIR} -> ${RCLONE_REMOTE}"
    rclone sync "${BACKUP_DIR}" "${RCLONE_REMOTE}/pg" --progress --retries=3
fi

echo "[backup] Done."
