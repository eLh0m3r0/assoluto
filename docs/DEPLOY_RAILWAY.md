# Deploying Assoluto on Railway

This guide walks through deploying the portal on [Railway](https://railway.app) —
a single-service Docker deployment backed by Railway's managed Postgres, an
external S3-compatible store (Backblaze B2 or Cloudflare R2), and an external
SMTP relay (Postmark or Resend).

Railway provides HTTPS termination and routing — nginx is **not** needed on this path.

---

## Prerequisites

1. Railway account — https://railway.app
2. Backblaze B2 bucket (or Cloudflare R2) with credentials
3. Postmark or Resend account for transactional email
4. A domain you control (for wildcard DNS if multi-tenant; a single hostname for single-tenant)
5. A tagged Docker image on GHCR — trigger by pushing a `v*.*.*` tag to GitHub

---

## Step 1 — Push the release image

```bash
git tag v0.1.0
git push origin v0.1.0
```

The GitHub Actions `release.yml` workflow builds a multi-arch image and pushes:

- `ghcr.io/elh0m3r0/sme-client-portal:0.1.0`
- `ghcr.io/elh0m3r0/sme-client-portal:0.1`
- `ghcr.io/elh0m3r0/sme-client-portal:0`
- `ghcr.io/elh0m3r0/sme-client-portal:latest`

Wait for the Actions run to complete before deploying.

---

## Step 2 — Create Railway project

1. Open https://railway.app/new → **Deploy from GitHub repo** → select `eLh0m3r0/sme-client-portal`
   - Railway detects `railway.toml` and `Dockerfile` automatically.
   - Alternatively: **Empty project** → **Add service** → **GitHub repo**.

2. Add the **Postgres** plugin: **New** → **Database** → **PostgreSQL**.

3. Copy the Postgres connection string from the plugin's **Connect** tab.
   You need it for the env vars below.

---

## Step 3 — Set environment variables

In the web service **Variables** tab, set every variable marked with `?` below.
Variables with a default can be left at the shown value.

```bash
# --- App ---
APP_ENV=production
APP_DEBUG=false
APP_SECRET_KEY=<generate: python -c "import secrets; print(secrets.token_urlsafe(64))">
APP_BASE_URL=https://<your-domain>        # e.g. https://portal.example.com

# --- Database ---
# Use values from Railway Postgres plugin:
DATABASE_URL=postgresql+asyncpg://postgres:<password>@<host>.railway.internal:5432/<db>
DATABASE_SYNC_URL=postgresql+psycopg://postgres:<password>@<host>.railway.internal:5432/<db>
DATABASE_OWNER_URL=postgresql+asyncpg://postgres:<password>@<host>.railway.internal:5432/<db>

# --- Tenancy (single-tenant self-host) ---
DEFAULT_TENANT_SLUG=<your-slug>           # e.g. 4mex

# --- S3 (Backblaze B2) ---
S3_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com
S3_PUBLIC_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com
S3_ACCESS_KEY=<B2 key ID>
S3_SECRET_KEY=<B2 application key>
S3_BUCKET=portal-prod
S3_REGION=eu-central-003
S3_USE_SSL=true

# --- SMTP (Postmark) ---
SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USER=<postmark server API key>
SMTP_PASSWORD=<postmark server API key>
SMTP_FROM="Assoluto <noreply@your-domain>"
SMTP_STARTTLS=true

# --- Feature flags ---
FEATURE_PLATFORM=false

# --- Logging ---
LOG_LEVEL=INFO
LOG_JSON=true
MAX_UPLOAD_SIZE_MB=50
```

> **Railway Postgres note:** The `postgres` user on Railway Postgres IS the table owner.
> Use the same credentials for all three `DATABASE_*` vars — the portal's `portal_app`
> role is bootstrapped via `docker/postgres-init.sql` which the entrypoint runs at start.
> However, Railway Postgres does not execute `initdb` scripts. You MUST run the init SQL
> manually once (see Step 4).

---

## Step 4 — Bootstrap the database roles

Railway Postgres starts as a clean Postgres instance. Run the init SQL once
to create the `portal_app` role before starting the app:

```bash
# Install psql locally if needed: brew install libpq
psql "<railway-postgres-url>" -f docker/postgres-init.sql
```

Or use the Railway CLI:

```bash
railway run --service postgres psql -f docker/postgres-init.sql
```

After this, the entrypoint will run `alembic upgrade head` automatically on
every container start.

---

## Step 5 — Set custom domain

1. In Railway web service → **Settings** → **Networking** → **Custom Domain**.
2. Add your domain and follow the DNS instructions.
3. For single-tenant: one domain is sufficient — set `DEFAULT_TENANT_SLUG`.
4. For multi-tenant: configure a wildcard `*.portal.example.com` CNAME → Railway's provided hostname.

---

## Step 6 — First tenant and user

Once the app is live, create the first tenant via the Railway console or a
`railway run` command:

```bash
railway run python -m scripts.create_tenant <slug> <owner-email> --password <initial-password>
```

Example:

```bash
railway run python -m scripts.create_tenant 4mex owner@4mex.cz --password "change-me-now"
```

The owner must change the password on first login.

---

## Step 7 — Smoke test

| Check | URL |
|-------|-----|
| Health | `https://<domain>/healthz` → `{"status":"ok"}` |
| Login | `https://<domain>/auth/login` (or `https://<slug>.<domain>/auth/login`) |
| Dashboard | After login: `/orders`, `/assets`, `/products` |
| Email | Create a test order → confirm notification email arrives |
| S3 | Upload an attachment → confirm file appears in B2 bucket |

---

## Updating

Railway auto-deploys when you push a new tag (if connected to GitHub). To deploy
a specific image version, update `APP_IMAGE_TAG` in Railway variables or trigger
a new deploy via the Railway dashboard.
