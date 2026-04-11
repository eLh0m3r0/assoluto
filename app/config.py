"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global configuration for the SME Client Portal.

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
    app_debug: bool = Field(default=True, alias="APP_DEBUG")
    app_secret_key: str = Field(default="dev-insecure-secret-change-me", alias="APP_SECRET_KEY")
    app_base_url: str = Field(default="http://localhost:8000", alias="APP_BASE_URL")

    # --- Database ----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://portal:portal@localhost:5432/portal",
        alias="DATABASE_URL",
    )
    database_sync_url: str = Field(
        default="postgresql+psycopg://portal:portal@localhost:5432/portal",
        alias="DATABASE_SYNC_URL",
    )

    # --- Tenancy -----------------------------------------------------------
    default_tenant_slug: str | None = Field(default=None, alias="DEFAULT_TENANT_SLUG")

    # --- S3 / MinIO --------------------------------------------------------
    s3_endpoint_url: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT_URL")
    s3_access_key: str = Field(default="portal", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="portalportal", alias="S3_SECRET_KEY")
    s3_bucket: str = Field(default="portal", alias="S3_BUCKET")
    s3_region: str = Field(default="eu-central-1", alias="S3_REGION")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")
    s3_public_url: str = Field(default="http://localhost:9000/portal", alias="S3_PUBLIC_URL")

    # --- SMTP --------------------------------------------------------------
    smtp_host: str = Field(default="localhost", alias="SMTP_HOST")
    smtp_port: int = Field(default=1025, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="SME Portal <noreply@localhost>", alias="SMTP_FROM")
    smtp_starttls: bool = Field(default=False, alias="SMTP_STARTTLS")

    # --- Uploads -----------------------------------------------------------
    max_upload_size_mb: int = Field(default=50, alias="MAX_UPLOAD_SIZE_MB")

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
    def is_test(self) -> bool:
        return self.app_env == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance.

    Cached so the env file is read only once and repeated access in request
    handlers is essentially free.
    """
    return Settings()
