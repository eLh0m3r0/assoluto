# Self-host guide

The SME Client Portal ships as a single Docker image and a
`docker-compose.yml` that brings up every dependency you need to run
the portal against a private Postgres.

## Prerequisites

- Docker 24+ with Compose v2
- A public hostname that resolves to the host (or use `*.localhost` for
  local testing)
- An SMTP relay (Postmark, Resend, Amazon SES, or a plain MTA) — MailHog
  is bundled for dev but is not suitable for production

## First run (local demo)

```bash
git clone https://github.com/eLh0m3r0/sme-client-portal.git
cd sme-client-portal
cp .env.example .env
docker compose up --build
```

On first start the Postgres container runs
`docker/postgres-init.sql`, which creates the unprivileged `portal_app`
role used by the running app, and Alembic applies all migrations before
uvicorn starts. When the stack is up, open:

| URL                              | What                                  |
|----------------------------------|---------------------------------------|
| http://4mex.localhost:8000/      | Portal (subdomain = tenant slug)      |
| http://localhost:8025/           | MailHog inbox (all outbound email)    |
| http://localhost:9001/           | MinIO console (bucket browser)        |

Create your first tenant and owner user:

```bash
docker compose exec web \
    python -m scripts.create_tenant 4mex owner@4mex.cz --password demo1234
```

Then log in at `http://4mex.localhost:8000/auth/login`.

For a demo dataset (one customer, a few products, one priced order, one
asset) run:

```bash
docker compose exec web python -m scripts.seed_dev
```

## Environment variables

See `.env.example` for the full list. The values you almost certainly
need to change for production:

```
APP_ENV=production
APP_DEBUG=false
APP_SECRET_KEY=<generate a 64-char random string>
APP_BASE_URL=https://portal.example.com

DATABASE_URL=postgresql+asyncpg://portal_app:<strong-pass>@db:5432/portal
DATABASE_SYNC_URL=postgresql+psycopg://portal:<owner-pass>@db:5432/portal
DATABASE_OWNER_URL=postgresql+asyncpg://portal:<owner-pass>@db:5432/portal

S3_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=portal-prod
S3_REGION=eu-central-003

SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_STARTTLS=true
SMTP_FROM="SME Portal <noreply@portal.example.com>"

MAX_UPLOAD_SIZE_MB=50
LOG_LEVEL=INFO
LOG_JSON=true
```

## Architecture at a glance

- **Two Postgres roles**: `portal` owns all tables (migrations + bootstrap
  scripts run as this role and bypass RLS). `portal_app` is a non-owner
  used by the running application — subject to Row-Level Security on
  every tenant-owned table via a `tenant_isolation` policy comparing
  `tenant_id = current_setting('app.tenant_id')::uuid`.
- **Tenant resolution**: the leftmost label of the `Host` header selects
  the tenant, e.g. `4mex.portal.example.com`. A fallback
  `DEFAULT_TENANT_SLUG` is honoured for single-tenant self-hosts.
- **Attachments** are stored in S3-compatible object storage (MinIO /
  Backblaze B2 / Cloudflare R2 / AWS S3). File size is capped by
  `MAX_UPLOAD_SIZE_MB`. Thumbnails are rendered synchronously in the
  background after the upload response has been sent.
- **No Redis in MVP**. E-mails and thumbnails run via FastAPI
  `BackgroundTasks`; periodic work (auto-closing delivered orders) runs
  via an in-process `APScheduler`. Roadmap item R0 covers the switch to
  Dramatiq + Redis once the portal has more than one pilot tenant.

## Upgrading

```bash
docker compose pull
docker compose up -d
```

The entrypoint always runs `alembic upgrade head` before starting
uvicorn, so migrations are applied in place.

## Backups

The only stateful services are Postgres and MinIO. A minimal backup
strategy:

```bash
# Database
docker compose exec db pg_dump -U portal portal | gzip > portal-$(date +%F).sql.gz

# Object storage (requires rclone configured against the MinIO endpoint)
rclone sync minio:portal ./backups/minio/
```

For production you'll want point-in-time recovery for Postgres (e.g.
`wal-g` against an off-site bucket) and versioned S3 buckets.
