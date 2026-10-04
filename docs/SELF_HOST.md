# Self-host guide

The Assoluto ships as a single Docker image and a
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
git clone https://github.com/eLh0m3r0/assoluto.git
cd assoluto
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

For a demo dataset instead (tenant `4mex`, one customer, a few products,
one priced order, one asset) run this on a fresh database:

```bash
docker compose exec web python -m scripts.seed_dev
```

and sign in at `http://4mex.localhost:8000/auth/login` as
`vlastnik@dilna.example.com` / `demo1234` (staff) or
`jan@klient.example.com` / `demo1234` (client contact).

## Production (Caddy + automatic HTTPS)

`docker-compose.prod.yml` is an **overlay** — always combine it with the
base file:

```bash
docker compose --env-file /etc/assoluto/env \
    -f docker-compose.yml -f docker-compose.prod.yml up -d
```

What the overlay does and what it needs:

- **Web image**: it runs `ghcr.io/elh0m3r0/assoluto:${APP_IMAGE_TAG:-latest}`
  instead of building. To run your own build, tag it locally first:
  `docker build -t ghcr.io/elh0m3r0/assoluto:latest .`
- **Required variables** (compose refuses to start without them):
  `APP_SECRET_KEY`, `APP_BASE_URL`, `PORTAL_OWNER_PASSWORD`,
  `PORTAL_APP_PASSWORD`, `S3_*`, `SMTP_*`, `TRUSTED_PROXIES`,
  `PORTAL_DOMAIN`, `ACME_EMAIL` and — for the bundled Caddy —
  `PORKBUN_API_KEY` / `PORKBUN_SECRET_KEY`.
- **Database role password**: `docker/postgres-init.sql` creates the
  runtime role `portal_app` with the password `portal_app`. Change it to
  your `PORTAL_APP_PASSWORD` right after the first start:
  `docker compose exec postgres psql -U portal -d portal -c "ALTER ROLE portal_app WITH PASSWORD '<PORTAL_APP_PASSWORD>';"`
- **TLS**: `docker/Caddyfile` issues a wildcard certificate (needed for
  tenant subdomains) through the Porkbun DNS API. With another DNS
  provider, build Caddy with that provider's `caddy-dns` plugin in
  `docker/Dockerfile.caddy` and change the `dns` block in the Caddyfile,
  or put your own reverse proxy in front (see `docker/nginx.conf.example`).
- **Object storage / mail**: MinIO and MailHog are removed in production;
  point `S3_*` at any S3-compatible service and `SMTP_*` at a real relay.

The full walkthrough for a single VPS (the setup assoluto.eu runs on) is in
[`DEPLOY_HETZNER.md`](DEPLOY_HETZNER.md).

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
S3_PUBLIC_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=portal-prod
S3_REGION=eu-central-003

# For self-hosted single-tenant: keep platform OFF
FEATURE_PLATFORM=false

SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_STARTTLS=true
SMTP_FROM="Assoluto <noreply@portal.example.com>"

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
docker compose exec postgres pg_dump -U portal portal | gzip > portal-$(date +%F).sql.gz

# Object storage (requires rclone configured against the MinIO endpoint)
rclone sync minio:portal ./backups/minio/
```

For production you'll want point-in-time recovery for Postgres (e.g.
`wal-g` against an off-site bucket) and versioned S3 buckets.
