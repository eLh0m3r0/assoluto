# syntax=docker/dockerfile:1
#
# Multi-stage image for SME Client Portal.
#
#   1. builder  — install deps with uv into /app/.venv, build Tailwind CSS
#   2. runtime  — slim Python image with just the venv and app code
#
# Resulting image runs `uvicorn app.main:app` as an unprivileged user.

ARG PYTHON_VERSION=3.12
ARG TAILWIND_VERSION=v3.4.14

# =============================================================================
# Stage 1: builder
# =============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first so docker layer cache is reused when only source changes
COPY pyproject.toml uv.lock ./
# Touch a placeholder README so `uv sync` doesn't complain about missing files
RUN touch README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project

# Download Tailwind CSS standalone binary
ARG TAILWIND_VERSION
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) tw_arch="x64" ;; \
        arm64) tw_arch="arm64" ;; \
        *) echo "Unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/tailwindcss \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-${tw_arch}"; \
    chmod +x /usr/local/bin/tailwindcss

# Copy sources and build Tailwind bundle
COPY app ./app
COPY tailwind.config.js ./
COPY README.md ./
RUN tailwindcss \
        -c tailwind.config.js \
        -i app/static/css/input.css \
        -o app/static/css/app.css \
        --minify

# Install the project itself into the venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

# =============================================================================
# Stage 2: runtime
# =============================================================================
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    APP_ENV=production \
    APP_DEBUG=false

# Minimal runtime packages (libpq for psycopg, poppler in M3+, tini for PID 1)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --shell /bin/bash --home /app app

WORKDIR /app

# Copy the virtualenv and the application from the builder
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/app /app/app
COPY --chown=app:app migrations /app/migrations
COPY --chown=app:app alembic.ini /app/alembic.ini
COPY --chown=app:app pyproject.toml /app/pyproject.toml
COPY --chown=app:app scripts /app/scripts
COPY --chown=app:app docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
