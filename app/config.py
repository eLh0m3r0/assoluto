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
    # one of the supported locales. The portal ships Czech (``cs``) and
    # English (``en``) out of the box.
    default_locale: str = Field(default="cs", alias="DEFAULT_LOCALE")
    # Comma-separated list of supported locale codes.
    supported_locales: str = Field(default="cs,en", alias="SUPPORTED_LOCALES")

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

    # --- Platform operator (legal entity behind the hosted service) -------
    # Filled on every hosted deployment; templated into the Terms of
    # Service + Privacy Policy pages. Leaving *any* of these empty hides
    # the legal pages (404) so we never publish a half-filled template
    # that a user could legally accept against an unnamed party.
    platform_operator_name: str = Field(default="", alias="PLATFORM_OPERATOR_NAME")
    platform_operator_ico: str = Field(default="", alias="PLATFORM_OPERATOR_ICO")
    platform_operator_address: str = Field(default="", alias="PLATFORM_OPERATOR_ADDRESS")
    platform_operator_email: str = Field(
        default="opensource@assoluto.eu", alias="PLATFORM_OPERATOR_EMAIL"
    )

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

    # --- SMTP --------------------------------------------------------------
    smtp_host: str = Field(default="localhost", alias="SMTP_HOST")
    smtp_port: int = Field(default=1025, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="Assoluto <noreply@localhost>", alias="SMTP_FROM")
    smtp_starttls: bool = Field(default=False, alias="SMTP_STARTTLS")

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
