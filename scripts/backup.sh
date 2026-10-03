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
#   PORTAL_KEEP_DAYS       — rotate dumps older than this (default 14
#                            to match the marketing + Terms commitment
#                            of "daily backups, 14 days retention" and
#                            the GDPR Art. 5(1)(e) storage limitation).
#   PORTAL_PG_CONTAINER    — docker-compose service name (default postgres)
#   PORTAL_DB_NAME         — database name (default portal)
#   PORTAL_DB_USER         — superuser (default portal)
#   BACKUP_GPG_RECIPIENT   — gpg key id / email to encrypt to. When set,
#                            output is .sql.gz.gpg; pubkey must be in the
#                            local gpg keyring. Empty = unencrypted.
#   BACKUP_S3_BUCKET       — off-site bucket (in a different location from
#                            the VPS). When set, the encrypted dump and every
#                            attachment are copied there by running
#                            `python -m app.ops.offsite_backup` inside the web
#                            container (which has boto3 + the S3 keys). Requires
#                            BACKUP_GPG_RECIPIENT — plaintext never leaves the box.
#   RCLONE_REMOTE          — legacy alternative: e.g. b2:portal-backups (must be
#                            pre-configured in `rclone config`); empty = skip.
#   PORTAL_ENV_FILE        — default /etc/assoluto/env. BACKUP_GPG_RECIPIENT,
#                            BACKUP_S3_BUCKET and RCLONE_REMOTE are read from it
#                            when not already set, because cron does not load it.
#
# See docs/BACKUP_RESTORE.md for the operator runbook.

set -euo pipefail

# Dumps hold every tenant's data. Without this they landed as 0664 —
# readable by any local account on the VPS.
umask 077

# cron runs with an empty environment, so the operator settings in the app's
# env file were never seen here — the off-site step silently never ran.
# Read only the keys this script needs (the file is not shell-safe to source).
ENV_FILE="${PORTAL_ENV_FILE:-/etc/assoluto/env}"
read_env_key() {
    [ -r "${ENV_FILE}" ] || return 0
    sed -n -E "s/^$1=//p" "${ENV_FILE}" | tail -n1 | sed -E 's/^"(.*)"$/\1/'
}
: "${BACKUP_GPG_RECIPIENT:=$(read_env_key BACKUP_GPG_RECIPIENT)}"
: "${BACKUP_S3_BUCKET:=$(read_env_key BACKUP_S3_BUCKET)}"
: "${RCLONE_REMOTE:=$(read_env_key RCLONE_REMOTE)}"

BACKUP_DIR="${PORTAL_BACKUP_DIR:-/backups}"
KEEP_DAYS="${PORTAL_KEEP_DAYS:-14}"
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
    echo "[backup] Copying ${BACKUP_DIR} -> ${RCLONE_REMOTE}"
    # `copy`, NOT `sync`. `sync` is a destructive mirror: it deletes
    # remote objects that are absent locally, so an emptied or
    # unmounted ${BACKUP_DIR} (rm -rf, failed mount, wrong volume)
    # propagated the deletion off-site on the next nightly run and took
    # the entire dump history with it. `copy` only ever adds.
    #
    # Retention off-site is therefore the REMOTE's job — set a bucket
    # lifecycle rule (and object lock / versioning) at the provider.
    # PORTAL_KEEP_DAYS below only rotates the LOCAL copies.
    rclone copy "${BACKUP_DIR}" "${RCLONE_REMOTE}/pg" --progress --retries=3
fi

# ---------- Off-site (S3, different location) ----------
if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
    if [ -z "${BACKUP_GPG_RECIPIENT:-}" ]; then
        echo "[backup] ERROR: BACKUP_S3_BUCKET is set but BACKUP_GPG_RECIPIENT is not — refusing to ship plaintext" >&2
        exit 1
    fi
    echo "[backup] Uploading $(basename "${DUMP_FILE}") off-site"
    # shellcheck disable=SC2094  # basename only reads the name
    docker compose exec -T web python -m app.ops.offsite_backup upload \
        --name "$(basename "${DUMP_FILE}")" < "${DUMP_FILE}"
    echo "[backup] Copying attachments off-site"
    docker compose exec -T web python -m app.ops.offsite_backup sync-attachments
fi

echo "[backup] Done."
