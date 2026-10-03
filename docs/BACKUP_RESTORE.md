# Backup & restore

Operator runbook for the Postgres backup script
(`scripts/backup.sh`) and the GPG-encrypted off-site copies.

## Overview

`scripts/backup.sh` runs nightly via cron on the production VPS
(03:00, user `deploy`). It dumps Postgres, GPG-encrypts the dump to the
operator's public key, keeps 14 days locally, and — when
`BACKUP_S3_BUCKET` is set — ships the encrypted dump **and every
attachment** (customers' drawings) to a versioned bucket in a different
Hetzner location from the VPS and the primary bucket.

The off-site copy runs `python -m app.ops.offsite_backup` inside the web
container (boto3 + S3 keys live there; the host has no rclone). It never
deletes: a wiped primary bucket or `/backups` directory cannot propagate.
Retention off-site = bucket lifecycle/versioning, not the script.
`/healthz/backups` answers 503 when the newest off-site dump is older
than `BACKUP_MAX_AGE_HOURS` (30 h); the uptime workflow probes it.

Each production deploy additionally takes a plain pre-migration dump in
`~deploy/predeploy-backups/` (newest 10, mode 600) — see
`DEPLOY_HETZNER.md` §8.3.

## Production setup (as configured 2026-10-04)

| what | value |
|---|---|
| Backup bucket | `assoluto-backups` @ `https://fsn1.your-objectstorage.com` (region `fsn1`), versioning on, private |
| Primary bucket | `assoluto` @ `nbg1` |
| GPG key | `Assoluto Backups <backups@assoluto.eu>`, fpr `84CFB7EB56DC2BB92D9450FDAA1026BE816A9384` — public key in `deploy`'s keyring on the VPS; **private key only with the operator** |
| Env (`/etc/assoluto/env`) | `BACKUP_GPG_RECIPIENT`, `BACKUP_S3_BUCKET`, `BACKUP_S3_ENDPOINT_URL`, `BACKUP_S3_REGION` |
| Cron | `0 3 * * * cd /opt/assoluto && PORTAL_BACKUP_DIR=/home/deploy/backups PORTAL_KEEP_DAYS=14 ./scripts/backup.sh >> /home/deploy/assoluto-backup.log 2>&1` |

`backup.sh` reads the `BACKUP_*` keys from `/etc/assoluto/env` itself
(cron's environment is empty). Hetzner rejects requests whose region
differs from the bucket location — `BACKUP_S3_REGION` must be `fsn1`.

## Restore from the off-site copy

```bash
# 1. On the VPS (or any host with the app image + S3 env): stream the
#    newest encrypted dump out of the backup bucket.
ssh deploy@assoluto.eu 'cd /opt/assoluto && docker compose exec -T web \
    python -m app.ops.offsite_backup fetch --latest' > latest.sql.gz.gpg

# 2. On the operator machine that holds the private key:
gpg --decrypt latest.sql.gz.gpg | gunzip > latest.sql

# 3. Restore into an EMPTY database (owner role `portal`), then re-run
#    docker/postgres-init.sql grants for `portal_app`:
createdb -U portal portal_restore
psql -U portal -d portal_restore -f latest.sql
```

Attachments live under `attachments/<original key>` in the backup bucket;
copy them back with the same keys into the primary bucket.

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
