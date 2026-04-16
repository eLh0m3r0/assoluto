# Changelog

All notable changes to the SME Client Portal are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- AGPL-3.0 license, `SECURITY.md`, `CODE_OF_CONDUCT.md`, GitHub issue/PR
  templates — repo is now ready for open-source release.

### Planned

- Internationalization (Czech + English) via Babel + gettext
- Self-signup, email verification, and onboarding wizard
- Marketing website (landing, features, pricing, self-hosted guide)
- Stripe billing integration (plans, subscriptions, invoices)
- Usage metering and plan limit enforcement
- Platform admin dashboard with KPIs and MRR tracking

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
