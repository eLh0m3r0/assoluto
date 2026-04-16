# Changelog

All notable changes to the SME Client Portal are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Phase 0 — License & community files:** AGPL-3.0 license,
  `SECURITY.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1),
  GitHub issue/PR templates.
- **Phase 1 — Internationalization:** Babel + gettext infrastructure
  with Czech + English locales, `jinja2.ext.i18n` integration,
  cookie-based `GET /set-lang` switcher, `LocaleMiddleware` for
  per-request locale negotiation (cookie → Accept-Language → default).
- **Phase 2 — Self-signup + email verification:** `POST /platform/signup`
  creates a tenant + owner User + Identity in one step, sends a
  verification email, auto-logs the user in to the platform session.
  Verification via `GET /platform/verify-email?token=` (24-hour TTL).
  Reserved-slug blocklist protects against phishing-friendly subdomains.
- **Phase 3 — Marketing website:** landing page, features, pricing,
  self-hosted guide, contact form, terms/privacy placeholders.
  Apex-domain routing: shows marketing pages when no tenant resolves
  and `FEATURE_PLATFORM=true`, otherwise falls back to tenant landing.
- **Phase 4 — Billing (Stripe-optional):** Plans, Subscriptions,
  Invoices models; `platform_plans` seeded with Community / Starter /
  Pro / Enterprise. Demo mode (no `STRIPE_SECRET_KEY`) keeps all
  bookkeeping local; live mode talks to Stripe for Checkout + webhooks.
  Signup auto-attaches a 14-day Starter trial.
- **Phase 5 — Usage metering + limits:** `UsageSnapshot` dataclass +
  `snapshot_tenant_usage()` + `ensure_within_limit()` that rejects
  creations that would push a tenant past its plan cap. Unlimited
  plans (community / enterprise) are always allowed.
- **Phase 6 — Platform admin dashboard:** `/platform/admin/dashboard`
  with KPI cards (total tenants, signups 7/30d, active subscriptions,
  MRR) + recent-signups table.
- **Phase 7 — Hosted deployment guide:** `docs/DEPLOY_SAAS.md` covers
  Hetzner + Coolify + Cloudflare (DNS/R2/SSL) + Resend + Stripe +
  Sentry end-to-end.

### Dependencies

- `babel>=2.16`
- `stripe>=10.0` (only used in live mode; demo mode avoids the import)

## [0.1.0] — Pilot MVP

First feature-complete release. Ready for pilot deployment.

### Core features

- **Orders** — full lifecycle state machine (DRAFT → SUBMITTED → QUOTED →
  CONFIRMED → IN_PRODUCTION → READY → DELIVERED → CLOSED), with comments,
  audit trail, and per-customer permissions.
- **Product catalog** — per-tenant or per-customer products, autocomplete
  picker on order creation.
- **Asset tracking** — customer-owned material/tools stored at the
  supplier; receive/issue/consume/adjust movements.
- **Attachments** — direct-to-S3 upload with presigned URLs, thumbnails
  for PDF and images, item-level or order-level attachments.
- **Email notifications** — new orders, status changes, and comments via
  `BackgroundTasks` (no Redis required). APScheduler for periodic jobs
  (auto-close delivered orders, cleanup expired tokens).
- **Multi-tenant** — shared PostgreSQL with Row-Level Security. Two DB
  roles: `portal` (owner, bypasses RLS) and `portal_app` (non-owner,
  subject to RLS). Tenant resolved from subdomain or `X-Tenant-Slug`
  header.
- **Platform (opt-in SaaS)** — `app/platform/` package gated by
  `FEATURE_PLATFORM=true`. Global `Identity`, `TenantMembership`,
  cross-tenant login, tenant switcher, platform admin CRUD.
- **CSRF protection** — double-submit cookie pattern via
  `CsrfCookieMiddleware` + `verify_csrf` dependency.
- **Staff admin** — invite, deactivate, password self-service, password
  reset flow.

### Infrastructure

- Docker multi-stage build (amd64 + arm64), published to
  `ghcr.io/elh0m3r0/sme-client-portal`.
- `docker-compose.yml` (dev with Postgres + MinIO + MailHog) and
  `docker-compose.prod.yml` (prod overlay with Nginx, external S3 + SMTP).
- GitHub Actions CI: ruff lint + format, pytest (112 tests), Docker build.
- GitHub Actions release: semantic versioning, multi-arch push to GHCR.
- Tailwind CSS standalone binary build (no Node required).

### Documentation

- `README.md`, `docs/ARCHITECTURE.md`, `docs/SELF_HOST.md`, `docs/ENV.md`,
  `CONTRIBUTING.md`, `CLAUDE.md`.

[Unreleased]: https://github.com/elh0m3r0/sme-client-portal/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/elh0m3r0/sme-client-portal/releases/tag/v0.1.0
