# Hosted SaaS Deployment Guide

This guide walks through a production deployment of Assoluto
as a hosted SaaS on the recommended stack:

| Component | Provider | Purpose |
|---|---|---|
| Compute | Hetzner Cloud (CX31+) | Docker host |
| Orchestration | Coolify | Docker-first PaaS UI |
| DNS + CDN + SSL | Cloudflare | Wildcard DNS and TLS |
| Database | PostgreSQL 16 on the VPS | Primary store |
| File storage | Cloudflare R2 | Attachments (no egress fees) |
| Email | Resend | Transactional mail |
| Payments | Stripe | Subscriptions + invoices |
| Error tracking | Sentry (free tier) | Exception reporting |
| Uptime | BetterUptime or UptimeRobot | External monitoring |

Total infra cost for a pilot: **~15 EUR/month** (8.49 EUR Hetzner + free
tiers on everything else).

## 1. Provision the server

1. Create a **Hetzner Cloud CX31** instance in **Nuremberg** or
   **Falkenstein** (both are in the EU, inside Germany). Pick Ubuntu 24.04.
2. Add your SSH key; disable root password login.
3. Enable the Hetzner **Cloud Firewall** and open:
   - 22 (SSH) — restrict to your IPs
   - 80, 443 (HTTPS traffic)

## 2. Install Coolify

Coolify is an open-source PaaS with a Railway-like UI. It manages
Docker, Let's Encrypt wildcard certs (via Cloudflare DNS-01), and
automated Postgres backups.

```bash
# On the fresh server:
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
```

Open `https://<server_ip>:8000`, create the admin user, then:

1. **Sources**: add GitHub (OAuth app) with read access to
   `elh0m3r0/sme-client-portal`.
2. **Destinations**: localhost (the VPS itself) is added automatically.
3. **Settings → Certificates**: paste your Cloudflare API token
   (create one scoped to DNS:Edit for your zone). Coolify uses this
   for DNS-01 wildcard challenges.

## 3. Configure DNS

In Cloudflare:

- `A  assoluto.eu → <server_ip>`  — proxied (orange cloud) ON
- `A  *.assoluto.eu → <server_ip>` — proxied ON

SSL mode: **Full (strict)**. Under `Network`, enable HTTP/3.

## 4. Set up Cloudflare R2

1. Cloudflare dashboard → R2 → **Create bucket** `assoluto`.
2. **S3 API tokens → Create token** with object read/write on that bucket.
3. Grab:
   - `Access Key ID`
   - `Secret Access Key`
   - Endpoint: `https://<accountid>.r2.cloudflarestorage.com`
4. **Public endpoint** for presigned URLs: enable public R2 URL or
   use the endpoint above.

Map these to env vars (see section 7).

## 5. Set up Resend

1. Sign up at [resend.com](https://resend.com) (free tier: 100 emails/day).
2. Add your domain → add the DNS records Cloudflare suggests
   (SPF, DKIM).
3. Create an SMTP username/password; use host `smtp.resend.com:587`.

## 6. Set up Stripe

1. Dashboard → **Test mode** for initial integration, then Live.
2. **Products**: create "Assoluto Starter" @ 490 CZK/month and
   "Assoluto Pro" @ 1 490 CZK/month, both monthly, tax-inclusive as
   per your Czech tax setup.
3. Note the `price_xxx` IDs — they go into `STRIPE_PRICE_STARTER` /
   `STRIPE_PRICE_PRO`.
4. **Webhooks** → add endpoint `https://assoluto.eu/platform/webhooks/stripe`
   subscribed to: `checkout.session.completed`, `invoice.paid`,
   `invoice.payment_failed`, `customer.subscription.updated`,
   `customer.subscription.deleted`. Copy the signing secret into
   `STRIPE_WEBHOOK_SECRET`.

## 7. Environment variables

In Coolify, create an application from the GitHub source and set:

```
APP_ENV=production
APP_DEBUG=false
APP_SECRET_KEY=<32 random bytes — e.g. `openssl rand -hex 32`>
APP_BASE_URL=https://assoluto.eu

DATABASE_URL=postgresql+asyncpg://portal_app:<strong>@postgres:5432/portal
DATABASE_SYNC_URL=postgresql+psycopg://portal:<strong>@postgres:5432/portal
DATABASE_OWNER_URL=postgresql+asyncpg://portal:<strong>@postgres:5432/portal

DEFAULT_LOCALE=cs
SUPPORTED_LOCALES=cs,en

FEATURE_PLATFORM=true
PLATFORM_COOKIE_DOMAIN=.assoluto.eu

S3_ENDPOINT_URL=https://<accountid>.r2.cloudflarestorage.com
S3_ACCESS_KEY=<r2 access key>
S3_SECRET_KEY=<r2 secret key>
S3_BUCKET=assoluto
S3_REGION=auto
S3_USE_SSL=true
S3_PUBLIC_ENDPOINT_URL=https://<accountid>.r2.cloudflarestorage.com

SMTP_HOST=smtp.resend.com
SMTP_PORT=587
SMTP_USER=<resend smtp user>
SMTP_PASSWORD=<resend smtp pass>
SMTP_FROM=Assoluto <team@assoluto.eu>
SMTP_STARTTLS=true

STRIPE_SECRET_KEY=sk_live_xxx
STRIPE_PUBLISHABLE_KEY=pk_live_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
STRIPE_PRICE_STARTER=price_xxx
STRIPE_PRICE_PRO=price_xxx

PLATFORM_OPERATOR_NAME=ACME Provider s.r.o.
PLATFORM_OPERATOR_ICO=12345678
PLATFORM_OPERATOR_ADDRESS=Masarykova 1, 110 00 Praha
PLATFORM_OPERATOR_EMAIL=legal@assoluto.eu

MAX_UPLOAD_SIZE_MB=50
LOG_LEVEL=INFO
LOG_JSON=true
```

Never commit the real values — Coolify stores them encrypted per
project.

## 8. First deploy

1. Coolify → **Applications → New → Docker Image** from GHCR
   (`ghcr.io/elh0m3r0/sme-client-portal:latest`).
2. Link to the env vars created above.
3. Deploy. The Docker entrypoint auto-applies Alembic migrations.
4. Create the platform-admin identity via:
   ```bash
   docker exec -it <container> python -m scripts.create_tenant platform admin@yourdomain.tld
   ```
   *(We'll add a dedicated `create_platform_admin` script as a
   follow-up; for now `create_tenant` is enough to seed the first row.)*

## 8a. Stripe Tax + Czech invoicing (§29 ZDPH — Section 29 of the Czech VAT Act)

> Czech tax-law glossary used in this section: **DPH** (daň z přidané
> hodnoty — Czech VAT, standard rate 21 %), **DIČ** (daňové
> identifikační číslo — Czech VAT ID), **IČO** (identifikační číslo
> osoby — Czech company registration number), **§29 ZDPH**
> (Section 29 of Act No. 235/2004 Coll. on VAT — mandatory invoice
> content rules).

The checkout session code already sends
``automatic_tax={"enabled": True}`` + ``tax_id_collection={"enabled":
True}`` + ``billing_address_collection="required"`` +
``locale="cs"``, but **Stripe Tax must be turned on and registered for
Czechia in the dashboard** before the first live invoice.

Steps on the Stripe side:

1. **Dashboard → Tax → Register** — pick **Czechia (DPH / VAT 21 %)** as
   the registration. Stripe now charges 21 % DPH on all B2C and B2B
   Czech-domestic invoices.
2. For EU B2B with a valid DIČ (VAT ID), Stripe automatically applies
   **reverse-charge (0 %)** as long as ``tax_id_collection`` is on.
3. Non-EU / foreign customers — no DPH, no special handling.

### Czech-compliant invoice numbering (§29 ZDPH)

Stripe's default invoice numbers are NOT a continuous Czech-compliant
series. Two supported paths:

**A. Use Stripe + external Czech invoicer (recommended).**
Handle ``invoice.paid`` webhook in ``app/platform/billing/webhooks.py``
by calling Fakturoid / iDoklad / Pohoda API with:
  - supplier IČO/DIČ (company ID / VAT ID — operator from ENV)
  - customer IČO/DIČ (from Stripe ``customer.tax_ids``)
  - invoice date = ``invoice.status_transitions.paid_at``
  - taxable-supply date (datum zdanitelného plnění) same as above
  - our own continuous number prefix (e.g. ``2026-B-0001``)
The Stripe hosted invoice becomes the informal receipt; the external
invoicer produces the legal one and emails it to the customer.

**B. Stripe-only with custom ``number_prefix``.**
``stripe.InvoiceSetting.update(number_prefix="SMEP-2026-")`` forces a
continuous per-year series. Combined with operator name/IČO/DIČ on
the Stripe tenant this can meet §29 — but Stripe PDFs don't carry a
"datum zdanitelného plnění" (date of taxable supply) field by default,
so an accountant should sign off before relying on this alone.

Either path can be wired later without a schema change — the
``platform_invoices`` table already tracks local invoice metadata.

## 9. Smoke test

- `https://assoluto.eu` → marketing landing
- `https://assoluto.eu/platform/signup` → registration form
- Sign up a test tenant; check MailHog / Resend for the verification
  email; click the link; watch `email_verified_at` get stamped.
- `https://testco.assoluto.eu` → tenant portal login
- `https://assoluto.eu/platform/billing` → subscription dashboard
  (Demo badge gone, Stripe prices visible)

## 10. Monitoring

Sentry (free tier):

```
SENTRY_DSN=https://<key>@o<org>.ingest.sentry.io/<project>
```

Add to your Coolify env vars. The app reads the env var through the
logging configuration; failing requests surface as Sentry issues with
full tracebacks.

Uptime (BetterUptime):

- Monitor URL: `https://assoluto.eu/healthz`
- Expected status 200, JSON body `{"status":"ok"}`.
- Alert channel: email + optional Slack.

## 11. Backups

Postgres:

```bash
# /etc/cron.daily/sme-backup.sh
docker exec assoluto-postgres pg_dump -U portal portal \
  | gzip > /var/backups/assoluto-$(date +%Y%m%d).sql.gz
# Upload to R2 with rclone — see scripts/backup.sh in the repo
rclone copy /var/backups/ r2:assoluto-backups/
find /var/backups -name "assoluto-*.sql.gz" -mtime +30 -delete
```

R2 files: durable by default (11 nines). Enable **Object Lock /
versioning** on the bucket for defence against accidental deletes.

## 12. Hardening checklist

- [ ] `APP_SECRET_KEY` is 32+ random bytes; rotate after any ops-side compromise.
- [ ] Postgres `portal_app` role has no CREATE; RLS is enabled.
- [ ] Cloudflare WAF enabled with Bot Fight Mode: ON.
- [ ] Rate-limit `/auth/*` at the nginx / Coolify Traefik layer.
- [ ] Automatic pg_dump daily → R2; test restore quarterly.
- [ ] Subscribe to repo Releases to know when to `docker compose pull`.
- [ ] See also: [SECURITY.md](../SECURITY.md).

## 13. Scaling notes

When you reach the first limits (roughly ~50 active tenants):

- Move Postgres to a managed provider (Neon has a generous free tier
  and regions in Frankfurt).
- Put S3 behind a Cloudflare Worker cache for public attachment URLs.
- Move transactional email to Postmark once you exceed 100 sends/day.
- Introduce Dramatiq + Redis for background tasks (roadmap R0) — the
  current APScheduler + FastAPI BackgroundTasks combo is fine up to
  a few hundred sends/hour.
