# Changelog

All notable changes to Assoluto are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — Second-round audit hardening (PR #9.1 – #9.4)

A second independent three-reviewer audit (Stripe, backend security,
UX) was run after the first hardening series. 5 new P0 issues + 11 P1
were found; this series closes them all.

- **PR #9.1 Security hotfix round 2** (+6 tests):
  - ``_resolve_tenant_id`` now prioritises the authoritative
    ``Tenant.stripe_customer_id`` lookup over customer-writeable
    metadata, and cross-checks candidate tenant_ids against it to
    block tenant-spoofing via ``metadata.tenant_id``
    (Stripe-N0 + Backend-N4 — cross-confirmed).
  - Webhook handlers raise ``WebhookNotYetReady`` on unresolvable
    tenant / missing subscription; router rolls back the dedup row
    + returns 503 so Stripe retries. Previous silent returns
    committed dedup permanently losing events (Backend-N1).
  - ``slowapi`` limiter key honours ``X-Forwarded-For`` only from
    trusted peers via a new ``TRUSTED_PROXIES`` CIDR allowlist
    (Backend-N2). Per-IP counters are now meaningful behind
    Cloudflare / nginx; header spoofing rejected.
  - ``tenants.stripe_customer_id`` partial-UNIQUE index
    (migration 1005) — two tenants can never share a Stripe
    Customer (Stripe-N5, Backend-P2).
  - Narrow exception types in ``verify_webhook`` and
    ``start_checkout`` (Backend-N3, Stripe-N3).
  - Password validator rejects leading/trailing whitespace and
    control chars before zxcvbn (Backend-N6).
  - ``RateLimitExceeded`` handler returns ``PlainTextResponse``
    on HTML clients.

- **PR #9.2 UX flow repairs round 2** (+1 test):
  - ``verify_email`` success screen renders a
    ``Finish setting up {plan}`` CTA that POSTs to
    ``/platform/switch/{slug}`` with a ``next`` field pointing at
    ``/platform/billing/checkout/{plan}`` — closes the pricing →
    signup → verify → checkout conversion loop (UX-C-1).
  - ``switch_to_tenant`` accepts an optional same-origin ``next``
    form field (validated via ``_safe_next_path``).
  - HTML 401 / 403 handlers are platform-aware: ``/platform/*``
    paths bounce to ``/platform/login`` rather than
    ``/auth/login``; 403s that carry a ``Location`` header are
    honoured as 303 redirects (UX-C-2, UX-C-3/C-11).
  - Billing is gated on ``UserRole.TENANT_ADMIN`` in
    ``_resolve_current_tenant`` — plain ``tenant_staff`` can no
    longer cancel a subscription (Stripe-N10).

- **PR #9.3 Stripe polish round 2** (+1 test):
  - Idempotency key now keys on ``trial_ends_at.isoformat()``
    instead of a per-minute bucket so legitimate retries collapse
    (Stripe-N1).
  - ``handle_subscription_upserted`` scans ALL line items for a
    recurring plan match — setup fees / add-ons at ``items[0]``
    no longer mask the real plan swap (Stripe-N4).
  - ``handle_checkout_completed`` refuses to reset
    ``status='demo' → 'trialing'`` once
    ``stripe_subscription_id`` is already populated
    (order-of-arrival race) (Stripe-N6).
  - ``handle_invoice_paid`` logs
    ``stripe.webhook.currency_mismatch`` when invoice currency
    differs from the plan's currency (Backend-P2).
  - Lifespan on boot flips any lingering ``status='demo'``
    subscriptions to ``trialing`` when Stripe becomes enabled
    (Backend-P2-N11).
  - New periodic task ``cleanup_old_stripe_events`` prunes
    ``platform_stripe_events`` older than 30 days (Stripe-N8).
  - ``tax_behavior=exclusive`` documentation added to the
    STRIPE_PRICE_* config docstrings (Stripe-N7).

- **PR #9.4 A11y + i18n extension**:
  - ``_flash.html`` banners carry ``role="alert"`` / ``role="status"``
    and their SVG icons are ``aria-hidden="true"``.
  - Billing usage bars gain ``role="progressbar"`` +
    ``aria-valuenow`` / ``aria-valuemin`` / ``aria-valuemax``.
  - ``select_tenant.html`` wrapped with gettext; new catalog
    entries shipped in cs + en .mo files. Remaining Czech strings
    on admin / billing dashboards are tracked as follow-up.
  - ``privacy.html`` ``[doplnit]`` ("to be filled") placeholder
    replaced with a concrete Czech-language description ("transakční
    e-mailový poskytovatel (EU regiony)" — "transactional email
    provider, EU regions").

### Schema

- Migration ``1005_tenant_customer_uq``: replaces the non-unique
  ``ix_tenants_stripe_customer_id`` with a partial UNIQUE index
  ``WHERE stripe_customer_id IS NOT NULL``.

Test suite: **206 passing** (round-2 baseline was 198).
Ruff lint + format: clean.

### Added — Post-audit hardening (8 PRs)

After the Phase 0–7 SaaS launch plan landed, an independent three-way
review (Stripe API conformance, backend security, UX/frontend) flagged
76 findings across severity levels. This series of PRs addresses all of
the P0s and most of the P1s:

- **PR #1 Security hotfix** (+18 tests): per-locale Jinja Environment
  cache (closes locale-leak race), HTML escape on contact form email
  (closes HTML injection), ``/set-lang`` open-redirect guard including
  backslash / protocol-relative vectors, ``slowapi`` rate limiting on
  contact / signup / verify-resend / login / password-reset,
  ``IntegrityError`` race mapping to ``DuplicateIdentityEmail`` /
  ``DuplicateTenantSlug``, ``zxcvbn`` password strength floor.
- **PR #2 Legal blockers** (+4 tests): ``PLATFORM_OPERATOR_{NAME,ICO,
  ADDRESS,EMAIL}`` env vars templated into Terms / Privacy with 404
  when unset, ``{{ |money(currency) }}`` Jinja filter for consistent
  "490 Kč" formatting, production + ``FEATURE_PLATFORM`` +
  no-Stripe boot assertion.
- **PR #3 Stripe webhook subscription sync** (+7 tests): new
  ``platform_stripe_events`` dedup table, metadata propagation via
  ``client_reference_id`` + ``metadata`` + ``subscription_data.metadata``,
  handlers for ``checkout.session.completed``,
  ``customer.subscription.{created,updated,deleted}``,
  ``invoice.payment_failed``, ``customer.subscription.trial_will_end``,
  plus idempotency and signature verification tests.
- **PR #4 Stripe polish** (+4 tests): idempotency keys on every
  mutating Stripe call, absolute ``trial_end`` timestamp instead of
  always-fresh ``trial_period_days``, Customer Portal route
  ``POST /platform/billing/portal`` + dashboard button, ``tenants.
  stripe_customer_id`` reuse.
- **PR #5 UX flow repairs** (+5 tests): ``?plan=`` query param
  survives signup → ``tenant.settings["selected_plan"]``,
  ``require_verified_identity`` dependency gates billing + admin
  routes until ``email_verified_at`` is stamped.
- **PR #6 Dashboard polish** (+1 test): usage progress bars on the
  billing dashboard, distinct "Přejít nahoru" / "Přejít dolů"
  ("Upgrade" / "Downgrade") button styling, ``cancel_at_period_end``
  warning banner, real MRR query
  from active plan prices (replaces the flawed 30-day paid sum),
  empty-state onboarding nudge on a fresh tenant dashboard.
- **PR #7 CZ compliance**: Stripe Tax (``automatic_tax`` +
  ``tax_id_collection`` + ``billing_address_collection``),
  ``locale="cs"`` on the hosted Checkout, documentation of §29 ZDPH
  invoicing paths (Fakturoid/iDoklad integration or custom
  ``number_prefix``).
- **PR #8 i18n coverage** (+1 test): signup / verify / login /
  base.html wrapped with ``gettext`` markers, Czech + English
  catalogues for the new msgids, accessibility improvements
  (``aria-hidden`` on decorative SVGs, ``aria-describedby`` on form
  helpers, ``sr-only`` required-field labels).

### Dependencies

- ``slowapi>=0.1.9`` — per-IP rate limiting
- ``zxcvbn>=4.4.28`` — password strength scoring

### Schema (forward-only)

- Migration ``1002_identity_verification``: add ``email_verified_at``
  and ``terms_accepted_at`` on ``platform_identities``.
- Migration ``1003_billing``: add ``platform_plans``,
  ``platform_subscriptions``, ``platform_invoices`` with seed rows
  for community/starter/pro/enterprise plans.
- Migration ``1004_stripe_events``: add ``platform_stripe_events``
  dedup table + ``tenants.stripe_customer_id`` column (indexed).

Test suite: **198 passing** (baseline 159 before audit-fix series).
Ruff lint + format: clean.

### Added — Original Phase 0-7 plan

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
