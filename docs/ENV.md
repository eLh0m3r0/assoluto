# Environment variables reference

Complete reference of all settings read by `app/config.py`. Copy
`.env.example` to `.env` for local development; in production set
these on the container environment or in a secrets manager.

Variables marked **required in prod** must be explicitly set for a
production deployment — the defaults are insecure or point at
localhost services that won't exist.

## Application

| Variable | Type | Default | Prod required | Description |
|---|---|---|---|---|
| `APP_ENV` | `development` / `test` / `production` | `development` | yes | Controls scheduler start, error verbosity, CaptureSender vs SMTP |
| `APP_DEBUG` | bool | `true` | yes (set `false`) | FastAPI debug mode; exposes tracebacks |
| `APP_SECRET_KEY` | string | `dev-insecure-…` | **yes** | Signs session cookies, CSRF tokens, invite/reset tokens. Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `APP_BASE_URL` | string | `http://localhost:8000` | yes | Used in email links (invite URLs, password reset) |

## Database

The portal uses **two Postgres roles** — see [ARCHITECTURE.md](ARCHITECTURE.md)
for the rationale.

| Variable | Type | Default | Prod required | Description |
|---|---|---|---|---|
| `DATABASE_URL` | DSN | `postgresql+asyncpg://portal_app:portal_app@localhost:5432/portal` | yes | Async DSN for the **runtime** app process (`portal_app` role, subject to RLS) |
| `DATABASE_SYNC_URL` | DSN | `postgresql+psycopg://portal:portal@localhost:5432/portal` | yes | Sync DSN for **Alembic migrations** and bootstrap scripts (`portal` owner role, bypasses RLS) |
| `DATABASE_OWNER_URL` | DSN | `postgresql+asyncpg://portal:portal@localhost:5432/portal` | yes | Async owner DSN for **background jobs and CLI scripts** that legitimately work across tenants |

## Tenancy

| Variable | Type | Default | Prod required | Description |
|---|---|---|---|---|
| `DEFAULT_TENANT_SLUG` | string | *(empty)* | no | Fallback tenant slug for single-tenant / self-host. When empty, tenant is resolved from subdomain or `X-Tenant-Slug` header |

## Platform (hosted SaaS layer)

These only matter when `FEATURE_PLATFORM=true`. Self-hosted open-source
deployments can ignore them.

| Variable | Type | Default | Prod required | Description |
|---|---|---|---|---|
| `FEATURE_PLATFORM` | bool | `false` | no | Enable the `app/platform/` package: global Identity, tenant switcher, platform admin |
| `PLATFORM_COOKIE_DOMAIN` | string | *(empty)* | if platform on | Parent domain for the cross-subdomain platform session cookie, e.g. `.portal.example.com`. Leave empty for single-host dev |

## S3 / Object storage

| Variable | Type | Default | Prod required | Description |
|---|---|---|---|---|
| `S3_ENDPOINT_URL` | URL | `http://localhost:9000` | yes | Internal endpoint the app uses for S3 API calls (MinIO in dev, managed S3 in prod) |
| `S3_PUBLIC_ENDPOINT_URL` | URL | *(empty)* | recommended | Browser-facing endpoint baked into presigned download URLs. Leave empty to reuse `S3_ENDPOINT_URL`. In docker-compose set to `http://localhost:9000` so the browser can reach MinIO |
| `S3_ACCESS_KEY` | string | `portal` | yes | S3 access key |
| `S3_SECRET_KEY` | string | `portalportal` | yes | S3 secret key |
| `S3_BUCKET` | string | `portal` | yes | Bucket name (auto-created at app startup) |
| `S3_REGION` | string | `eu-central-1` | yes | S3 region for signature |
| `S3_USE_SSL` | bool | `false` | yes (set `true`) | Use HTTPS for S3 connections |

## SMTP

| Variable | Type | Default | Prod required | Description |
|---|---|---|---|---|
| `SMTP_HOST` | string | `localhost` | yes | SMTP server hostname |
| `SMTP_PORT` | int | `1025` | yes | SMTP port (1025 = MailHog dev, 587 = STARTTLS prod) |
| `SMTP_USER` | string | *(empty)* | if SMTP requires auth | SMTP login username |
| `SMTP_PASSWORD` | string | *(empty)* | if SMTP requires auth | SMTP login password |
| `SMTP_FROM` | string | `SME Portal <noreply@localhost>` | yes | Sender address in outbound emails |
| `SMTP_STARTTLS` | bool | `false` | yes (set `true`) | Enable STARTTLS for SMTP connection |

## File uploads

| Variable | Type | Default | Prod required | Description |
|---|---|---|---|---|
| `MAX_UPLOAD_SIZE_MB` | int | `50` | no | Maximum file size per attachment in megabytes |

## Logging

| Variable | Type | Default | Prod required | Description |
|---|---|---|---|---|
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | `INFO` | no | structlog minimum level |
| `LOG_JSON` | bool | `false` | recommended (`true`) | `true` = JSON lines on stdout (for log aggregators); `false` = coloured dev console |
