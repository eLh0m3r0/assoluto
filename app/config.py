"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global configuration for Assoluto.

    Values are populated from environment variables (and optionally a `.env`
    file in development). Keep this model flat and explicit — it documents
    every knob the deployment needs.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = Field(
        default="development", alias="APP_ENV"
    )
    # Safe default: ``false``. FastAPI with debug=True lets Starlette's
    # ServerErrorMiddleware render a full traceback on unhandled
    # exceptions, which leaks source paths + line numbers. Operators
    # who want debug in dev must set APP_DEBUG=true explicitly.
    app_debug: bool = Field(default=False, alias="APP_DEBUG")
    app_secret_key: str = Field(default="dev-insecure-secret-change-me", alias="APP_SECRET_KEY")
    app_base_url: str = Field(default="http://localhost:8000", alias="APP_BASE_URL")

    # --- Database ----------------------------------------------------------
    # Runtime app user — non-owner, fully subject to Row-Level Security.
    database_url: str = Field(
        default="postgresql+asyncpg://portal_app:portal_app@localhost:5432/portal",
        alias="DATABASE_URL",
    )
    # Sync DSN used by Alembic and bootstrap scripts (owner, bypasses RLS).
    database_sync_url: str = Field(
        default="postgresql+psycopg://portal:portal@localhost:5432/portal",
        alias="DATABASE_SYNC_URL",
    )
    # Async DSN for scripts/CLI tasks that need to act across tenants.
    database_owner_url: str = Field(
        default="postgresql+asyncpg://portal:portal@localhost:5432/portal",
        alias="DATABASE_OWNER_URL",
    )

    # --- Tenancy -----------------------------------------------------------
    default_tenant_slug: str | None = Field(default=None, alias="DEFAULT_TENANT_SLUG")

    # --- Localization (i18n) -----------------------------------------------
    # Default UI language served when no cookie / header preference matches
    # one of the supported locales. The portal ships Czech (``cs``),
    # English (``en``) and German (``de``) out of the box.
    default_locale: str = Field(default="cs", alias="DEFAULT_LOCALE")
    # Comma-separated list of supported locale codes.
    supported_locales: str = Field(default="cs,en,de", alias="SUPPORTED_LOCALES")

    # --- Platform (hosted SaaS layer) ---------------------------------------
    # When enabled, the `app.platform` package registers extra routes for
    # platform-level identity, tenant switching, and tenant CRUD. Keep it
    # OFF for self-hosted / open-source deployments.
    feature_platform: bool = Field(default=False, alias="FEATURE_PLATFORM")
    # Parent domain cookies for the cross-subdomain session, e.g.
    # `.portal.example.com`. Leave empty for single-host deployments.
    platform_cookie_domain: str = Field(default="", alias="PLATFORM_COOKIE_DOMAIN")
    # Escape hatch for hosted *staging / demo* environments that want the
    # platform UI without configuring Stripe yet. Normally the app refuses
    # to start when FEATURE_PLATFORM=true + APP_ENV=production + no Stripe
    # (because paid checkouts would silently succeed). Setting this to
    # True acknowledges the risk and lets checkout stay in ``demo`` mode.
    # Never set this on a real paying-customer deployment.
    feature_platform_allow_demo: bool = Field(default=False, alias="FEATURE_PLATFORM_ALLOW_DEMO")
    # Whether the Stripe-less "demo" checkout may switch a tenant's plan
    # locally without charging. Unset = allowed everywhere EXCEPT
    # production (dev/test/staging keep the click-through demo). In
    # production without Stripe the checkout instead tells the customer
    # that online payment is being set up and to write to
    # PLATFORM_OPERATOR_EMAIL for a bank-transfer invoice — it never
    # grants a plan for free (audit 2026-10-03, BIZ-01 / D2). Set to
    # true only on a hosted staging/demo box that has no real customers.
    billing_demo_mode_allowed: bool | None = Field(default=None, alias="BILLING_DEMO_MODE_ALLOWED")
    # Trial-nurture email cadence (day-1 onboarding, day-7 check-in,
    # trial-ending reminder 5 days before trial_ends_at). Off by default
    # so the copy can be reviewed before any tenant receives it; flip to
    # true in /etc/assoluto/env once approved. Requires FEATURE_PLATFORM.
    trial_nurture_enabled: bool = Field(default=False, alias="TRIAL_NURTURE_ENABLED")
    # Behaviour-based activation nudges to trial admins (BIZ-16): day 2
    # "invite your first customer" (only if nobody was invited) and day 5
    # "your customer hasn't signed in yet — resend the invitation" (only
    # if invited contacts never signed in). Same copy-approval rule as
    # TRIAL_NURTURE_ENABLED: off by default. Requires FEATURE_PLATFORM.
    activation_nudges_enabled: bool = Field(default=False, alias="ACTIVATION_NUDGES_ENABLED")
    # "Powered by Assoluto" footer in customer-contact emails and in the
    # customer portal (MKT-9). Base URL of the marketing site the footer
    # links to; ``?ref=portal&t=<tenant slug>`` is appended. Empty = derive
    # ``https://<apex>`` from PLATFORM_COOKIE_DOMAIN; empty and no cookie
    # domain (self-hosted) = no footer at all. Set to ``off`` to disable
    # it on a hosted deployment. A single tenant can opt out with
    # ``tenants.settings["hide_powered_by"] = true`` (white-label).
    powered_by_url: str = Field(default="", alias="POWERED_BY_URL")

    # --- Public demo (E3) --------------------------------------------------
    # Slug of the tenant that anyone may enter without signing up, via
    # ``https://<slug>.<apex>/demo`` (staff or customer view). Empty = off.
    # Only a tenant created by ``app.demo.seed`` (marker
    # ``tenants.settings.demo_seed``) can be public; any other tenant with
    # this slug is refused. While on, that tenant sends no e-mail at all,
    # account / invitation / settings changes are blocked, uploads are
    # capped, and the data is re-seeded every night (02:30 Europe/Prague).
    # See app/demo/ and docs/OPERATOR_PLAYBOOK.md §9.
    public_demo_tenant: str = Field(default="", alias="PUBLIC_DEMO_TENANT")

    # --- Orders -----------------------------------------------------------
    # Days an order may sit in QUOTED before the customer contacts get one
    # follow-up reminder (respecting their notification consent) and the
    # staff dashboard lists it as "waiting for the client". 0 disables
    # the reminder mail; the dashboard then falls back to 3 days.
    quote_reminder_days: int = Field(default=3, alias="QUOTE_REMINDER_DAYS", ge=0)

    # --- Platform operator (legal entity behind the hosted service) -------
    # Filled on every hosted deployment; templated into the Terms of
    # Service + Privacy Policy pages. Leaving *any* of these empty hides
    # the legal pages (404) so we never publish a half-filled template
    # that a user could legally accept against an unnamed party.
    platform_operator_name: str = Field(default="", alias="PLATFORM_OPERATOR_NAME")
    platform_operator_ico: str = Field(default="", alias="PLATFORM_OPERATOR_ICO")
    # DIČ — optional; empty string means "not VAT-registered" and the
    # CZ-tax invoice PDF falls back to the non-VAT path. Supplier
    # IČO alone is legally sufficient for non-DPH invoices per
    # §11 of Act 235/2004 (VAT Act).
    #
    # This is the SINGLE switch for the operator's VAT status. It also
    # drives Stripe Checkout: ``automatic_tax`` and ``tax_id_collection``
    # are enabled only when it is set (see
    # ``app.platform.billing.service.create_checkout_session``). Setting
    # it flips the checkout, the invoice layout and the document label
    # ("Faktura" -> "Daňový doklad") together; leaving it empty keeps the
    # listed price final, with no DPH charged anywhere.
    platform_operator_dic: str = Field(default="", alias="PLATFORM_OPERATOR_DIC")
    platform_operator_address: str = Field(default="", alias="PLATFORM_OPERATOR_ADDRESS")
    # Version tag stamped on every accepted Terms + Privacy consent so we
    # can tell, per Identity, which revision they accepted. Bump when
    # you publish a new version of ``terms.html`` / ``privacy.html``.
    # Format is free but convention is ``YYYY.MM`` or semver.
    legal_doc_version: str = Field(default="2026.05", alias="LEGAL_DOC_VERSION")
    platform_operator_email: str = Field(
        default="team@assoluto.eu", alias="PLATFORM_OPERATOR_EMAIL"
    )
    # Public uptime status page (e.g. an UptimeRobot Public Status Page).
    # When set, marketing pages render a hyperlink instead of "on request".
    # Empty string keeps the legacy ``status page on request`` copy.
    status_page_url: str = Field(default="", alias="STATUS_PAGE_URL")

    # --- Billing (Stripe) --------------------------------------------------
    # Leave all empty to run in "demo mode" — billing flows still work but
    # never call the Stripe API. Set STRIPE_SECRET_KEY (and the price IDs)
    # to enable real checkout.
    stripe_secret_key: str = Field(default="", alias="STRIPE_SECRET_KEY")
    stripe_publishable_key: str = Field(default="", alias="STRIPE_PUBLISHABLE_KEY")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")
    # Stripe Price IDs for the two paid plans (create in Stripe dashboard).
    # REQUIRED: each Price must have ``tax_behavior`` set to either
    # ``inclusive`` or ``exclusive`` at creation time in the Stripe
    # dashboard — otherwise checkout sessions with ``automatic_tax=true``
    # fail with ``InvalidRequestError: The price … doesn't have
    # tax_behavior set``. For Czech DPH 21 % registration, pick
    # ``exclusive`` (list prices shown "bez DPH") — it matches how our
    # pricing page labels the amounts.
    stripe_price_starter: str = Field(default="", alias="STRIPE_PRICE_STARTER")
    stripe_price_pro: str = Field(default="", alias="STRIPE_PRICE_PRO")

    # --- S3 / MinIO --------------------------------------------------------
    s3_endpoint_url: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT_URL")
    s3_access_key: str = Field(default="portal", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="portalportal", alias="S3_SECRET_KEY")
    s3_bucket: str = Field(default="portal", alias="S3_BUCKET")
    s3_region: str = Field(default="eu-central-1", alias="S3_REGION")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")
    # Optional: endpoint exposed to end-users' browsers for presigned URLs.
    # Leave empty to reuse `s3_endpoint_url`. In docker-compose this should
    # point at the host-exposed port (e.g. http://localhost:9000) so the
    # browser can actually reach the MinIO container.
    s3_public_endpoint_url: str = Field(default="", alias="S3_PUBLIC_ENDPOINT_URL")

    # Off-site backup target (app.ops.offsite_backup). A bucket in a
    # different location from S3_BUCKET and the VPS. Credentials default to
    # the primary S3 keys (Hetzner keys are project-wide). Empty = disabled.
    backup_s3_endpoint_url: str = Field(default="", alias="BACKUP_S3_ENDPOINT_URL")
    backup_s3_region: str = Field(default="", alias="BACKUP_S3_REGION")
    backup_s3_bucket: str = Field(default="", alias="BACKUP_S3_BUCKET")
    backup_s3_access_key: str = Field(default="", alias="BACKUP_S3_ACCESS_KEY")
    backup_s3_secret_key: str = Field(default="", alias="BACKUP_S3_SECRET_KEY")
    # /healthz/backups answers 503 when the newest off-site dump is older.
    backup_max_age_hours: float = Field(default=30.0, alias="BACKUP_MAX_AGE_HOURS")

    # --- SMTP --------------------------------------------------------------
    smtp_host: str = Field(default="localhost", alias="SMTP_HOST")
    smtp_port: int = Field(default=1025, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="Assoluto <team@localhost>", alias="SMTP_FROM")
    smtp_starttls: bool = Field(default=False, alias="SMTP_STARTTLS")
    # Operator kill-switch: set to false in /etc/assoluto/env to halt every
    # outbound mail without a redeploy. Use it the moment a sending-platform
    # block notification arrives (Brevo, Postmark, etc.) so we stop adding
    # to the bounce/complaint counter while the spam vector is investigated.
    enable_outbound_emails: bool = Field(default=True, alias="ENABLE_OUTBOUND_EMAILS")

    # Durable e-mail outbox (audit BE-09). Every templated mail is written
    # to ``email_outbox`` before the first send attempt; failures are
    # retried by the ``deliver_email_outbox`` job with exponential backoff.
    # ``false`` restores the old fire-and-forget path (three quick retries,
    # then the mail is gone) — an escape hatch, not a recommended setting.
    email_outbox_enabled: bool = Field(default=True, alias="EMAIL_OUTBOX_ENABLED")

    # --- Operations / observability ---------------------------------------
    # Operator alert address. When set, every unhandled 500 and every
    # crashed scheduler job mails this address (path, error type, request
    # id, traceback frames — never request bodies or cookies), at most once
    # per error signature per 15 minutes. Empty = disabled.
    ops_alert_email: str = Field(default="", alias="OPS_ALERT_EMAIL")
    # ``/readyz`` also probes the S3 bucket (short timeout, result cached
    # for 30 s). Turn off for deployments without object storage.
    readyz_check_s3: bool = Field(default=True, alias="READYZ_CHECK_S3")
    # Build identifier used as the static-asset cache-buster
    # (``/static/css/app.css?v=<build id>``). CI may pass the commit SHA as
    # a Docker build arg; when empty the app derives a content hash of the
    # static directory at boot, so a deploy with changed CSS/JS always
    # gets a new URL.
    app_build_id: str = Field(default="", alias="APP_BUILD_ID")

    # --- Data retention (D6) ----------------------------------------------
    # The retention job (daily) purges tenants deactivated more than 30 days
    # ago (DB rows + S3 objects), audit events older than 3 years, and S3
    # objects no DB row references that are older than 7 days. Dry-run by
    # default: it only logs what it WOULD delete. Set to true to delete.
    retention_enforce: bool = Field(default=False, alias="RETENTION_ENFORCE")

    # --- Tenancy hardening -------------------------------------------------
    # Honour a client-supplied ``X-Tenant-Slug`` header in production.
    # Off by default: in production the tenant comes from the Host only, so
    # nobody can address another tenant's app through the apex domain
    # (audit SEC-7). Development and test always honour the header.
    trust_tenant_header: bool = Field(default=False, alias="TRUST_TENANT_HEADER")

    # --- Uploads -----------------------------------------------------------
    max_upload_size_mb: int = Field(default=50, alias="MAX_UPLOAD_SIZE_MB")

    # --- Proxy / rate limiting --------------------------------------------
    # Comma-separated CIDR blocks or IP literals we trust to forward a
    # real client IP in ``X-Forwarded-For``. Empty = don't trust any
    # header — use ``request.client.host`` directly (safe default for
    # local dev). In production behind Cloudflare / nginx, set this to
    # the proxy's egress IPs (and Cloudflare's published ranges) so per-
    # client rate limits actually track the real client.
    trusted_proxies: str = Field(default="", alias="TRUSTED_PROXIES")

    # --- Logging -----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )
    log_json: bool = Field(default=False, alias="LOG_JSON")

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def stripe_enabled(self) -> bool:
        """Demo mode = billing UI without Stripe API calls."""
        return bool(self.stripe_secret_key)

    @property
    def billing_demo_checkout_allowed(self) -> bool:
        """May a Stripe-less checkout switch the plan locally for free?

        Never relevant when Stripe is configured. Defaults to "not in
        production" — see ``billing_demo_mode_allowed``.
        """
        if self.billing_demo_mode_allowed is not None:
            return self.billing_demo_mode_allowed
        return not self.is_production

    @property
    def operator_identity_complete(self) -> bool:
        """All legal identity fields filled in — safe to serve ToS / Privacy."""
        return bool(
            self.platform_operator_name
            and self.platform_operator_ico
            and self.platform_operator_address
        )

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance.

    Cached so the env file is read only once and repeated access in request
    handlers is essentially free.
    """
    return Settings()
