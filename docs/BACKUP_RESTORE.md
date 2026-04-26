# Backup & restore

Operator runbook for the Postgres backup script
(`scripts/backup.sh`) and the GPG-encrypted off-site copies.

## Overview

`scripts/backup.sh` runs nightly via cron on the production VPS.
It dumps the Postgres database, optionally GPG-encrypts the result,
keeps the last 30 days locally, and (optionally) syncs the directory
to an off-site `rclone` remote.

S3 attachment backup is **not** in scope for this script — see the
operator decision in `PRELAUNCH_REVIEW_2026-04-25.md` §"S3
attachment backup".

## Set up encryption

1. **On a personal machine** (NOT the production VPS) generate a
   long-lived backup keypair:
   ```bash
   gpg --quick-generate-key 'Assoluto Backups <backups@assoluto.eu>' \
       rsa4096 cert,encrypt 5y
   ```
   Pick a strong passphrase and write it down somewhere safe
   (1Password, paper, etc.). The **secret key never leaves this
   machine.**

2. Export the **public** key:
   ```bash
   gpg --armor --export backups@assoluto.eu > backups-pubkey.asc
   ```

3. Copy the public key to the production VPS and import:
   ```bash
   scp backups-pubkey.asc deploy@assoluto.eu:/tmp/
   ssh deploy@assoluto.eu
   gpg --import /tmp/backups-pubkey.asc
   rm /tmp/backups-pubkey.asc
   ```

4. Set the env var so the backup script encrypts to that key:
   ```bash
   sudo vim /etc/assoluto/env
   # add:
   BACKUP_GPG_RECIPIENT=backups@assoluto.eu
   ```

5. Verify the next backup produces `*.sql.gz.gpg`:
   ```bash
   sudo /opt/assoluto/scripts/backup.sh
   ls -la /backups
   ```
   The file extension should be `.sql.gz.gpg`, not `.sql.gz`.

## Verify a restore (do this once after setup, then quarterly)

The most expensive incident is "we have backups, they don't
restore". Test the loop end-to-end on a clean machine — never on
production.

1. Pick a recent encrypted backup, copy it to the personal machine
   that holds the secret key:
   ```bash
   scp deploy@assoluto.eu:/backups/portal-20260426-030000.sql.gz.gpg .
   ```

2. Decrypt:
   ```bash
   gpg --decrypt portal-20260426-030000.sql.gz.gpg \
       | gunzip > portal-restore-test.sql
   ```
   You'll be prompted for the passphrase from step 1 of setup.

3. Spin up a throwaway Postgres + load the dump:
   ```bash
   docker run -d --name pg-restore-test \
       -e POSTGRES_PASSWORD=test \
       -p 55432:5432 postgres:16
   sleep 5
   docker exec -i pg-restore-test psql -U postgres < portal-restore-test.sql
   ```

4. Sanity check — count tenants, users, orders:
   ```bash
   docker exec pg-restore-test psql -U postgres -d portal -c \
       "SELECT (SELECT count(*) FROM tenants) AS tenants,
               (SELECT count(*) FROM users)   AS users,
               (SELECT count(*) FROM orders)  AS orders;"
   ```
   Numbers should match what you see in production.

5. Tear down:
   ```bash
   docker rm -f pg-restore-test
   rm portal-restore-test.sql portal-20260426-030000.sql.gz.gpg
   ```

If any step fails, the backup pipeline is broken — fix it before
shipping anything else.

## Restore on production after a disaster

This is the destructive path — only for the case where the live
database is gone or unrecoverably corrupted.

1. Stop the application:
   ```bash
   ssh deploy@assoluto.eu
   cd /opt/assoluto
   docker compose --env-file /etc/assoluto/env \
       -f docker-compose.yml -f docker-compose.prod.yml stop web
   ```

2. Decrypt the backup off-VPS (you need the secret key — never
   import it on the production server):
   ```bash
   # on personal machine
   scp deploy@assoluto.eu:/backups/portal-LATEST.sql.gz.gpg .
   gpg --decrypt portal-LATEST.sql.gz.gpg \
       | gunzip > portal-restore.sql
   scp portal-restore.sql deploy@assoluto.eu:/tmp/
   rm portal-restore.sql portal-LATEST.sql.gz.gpg
   ```

3. Drop and recreate the database. **This destroys current data —
   make sure step 2 succeeded.**
   ```bash
   ssh deploy@assoluto.eu
   docker compose --env-file /etc/assoluto/env exec postgres \
       psql -U postgres -c "DROP DATABASE portal; CREATE DATABASE portal OWNER portal;"
   docker compose --env-file /etc/assoluto/env exec -T postgres \
       psql -U portal -d portal < /tmp/portal-restore.sql
   rm /tmp/portal-restore.sql
   ```

4. Restart the application:
   ```bash
   docker compose --env-file /etc/assoluto/env \
       -f docker-compose.yml -f docker-compose.prod.yml start web
   ```

5. Smoke check:
   ```bash
   curl -s https://assoluto.eu/healthz
   curl -s https://assoluto.eu/readyz
   ```
   Both should return 200 OK.

## Off-site sync

If `RCLONE_REMOTE` is set in `/etc/assoluto/env`, the script
mirrors the entire `/backups` directory (including the encrypted
files) to the configured remote after every nightly run. Set up
your remote with `rclone config` once on the VPS as `deploy`; the
remote name in `RCLONE_REMOTE` (e.g. `b2:portal-backups/pg`) must
match.

For a worked walk-through of `rclone config` for Backblaze B2 see
`OPERATOR_PLAYBOOK.md` (TODO — not yet documented; B2 is set up the
same way as the rclone docs describe, no Assoluto-specific steps).

## Operational reminders

* **Don't keep the secret key on the production VPS.** That defeats
  encryption.
* **Test restores quarterly.** Untested backups are roughly equal
  to no backups.
* **Rotate the GPG key every 5 years** (or sooner if compromised).
  When you rotate: import the new pubkey, change
  `BACKUP_GPG_RECIPIENT`, and keep the old secret key archived so
  pre-rotation backups remain decryptable.
