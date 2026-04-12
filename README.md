# SME Client Portal

Zákaznický portál pro malé a střední výrobní firmy. Transparentní
objednávkový proces mezi dodavatelem (výrobcem) a jeho klienty.

> **English:** A multi-tenant customer portal for manufacturing SMEs.
> Customers submit orders (with attachments), the supplier quotes,
> confirms, tracks production status, and manages client-owned
> materials stored on-site. Built with Python / FastAPI / PostgreSQL.

---

## Features

| Module | Description |
|---|---|
| **Objednávky** | Klient zadá položky a přílohy, dodavatel nacení, navrhne termín, aktualizuje stav (DRAFT → SUBMITTED → QUOTED → CONFIRMED → IN_PRODUCTION → READY → DELIVERED → CLOSED). Komentáře, audit trail. |
| **Katalog produktů** | Per-tenant nebo per-customer katalog; výběr do objednávky místo free-textu s předvyplněním ceny a jednotky. |
| **Správa majetku klienta** | Evidence materiálu/nástrojů klienta u dodavatele: příjem, výdej, spotřeba, korekce. Klient má read-only přehled. |
| **E-mail notifikace** | Nové objednávky, změny stavů, komentáře — přes BackgroundTasks (bez Redisu). |
| **Multi-tenant** | Shared DB + Postgres Row-Level Security. Dva DB uživatelé (`portal` owner + `portal_app` non-owner). |
| **Platform (SaaS)** | Opt-in `app/platform/` package: globální Identity, cross-tenant login, tenant switcher, platform admin CRUD. `FEATURE_PLATFORM=true` aktivuje. |
| **CSRF ochrana** | Double-submit cookie pattern na všech mutating routes. |
| **Staff admin** | Správa staff uživatelů (invite, deaktivace), self-service změna hesla, password reset flow. |

## Tech stack

| Layer | Choice |
|---|---|
| Web | FastAPI 0.115+ |
| Templating | Jinja2 + jinja2-fragments |
| DB | PostgreSQL 16 (RLS) |
| ORM | SQLAlchemy 2 async + asyncpg |
| Migrations | Alembic (sync psycopg) |
| Background | FastAPI BackgroundTasks + APScheduler |
| Storage | S3-compatible (MinIO / B2 / R2) via boto3 |
| Email | SMTP (Postmark / Resend / MailHog) |
| Auth | Signed cookie session (itsdangerous) + Argon2 |
| CSS | Tailwind (standalone CLI, no Node) |

## Quick start

```bash
git clone https://github.com/eLh0m3r0/sme-client-portal.git
cd sme-client-portal
cp .env.example .env
docker compose up --build
```

Then seed demo data:

```bash
docker compose exec web python -m scripts.seed_dev
```

Open the portal:

| URL | What |
|---|---|
| http://localhost:8000 | Portal (set `DEFAULT_TENANT_SLUG=4mex` in `.env`, or use `http://4mex.localhost:8000`) |
| http://localhost:8025 | MailHog — captured emails |
| http://localhost:9001 | MinIO console (portal / portalportal) |

Demo credentials: `owner@4mex.cz` / `demo1234` (staff), `jan@acme.cz` / `demo1234` (customer contact).

## Documentation

| Doc | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, multi-tenancy, RLS, request flow, directory layout |
| [docs/SELF_HOST.md](docs/SELF_HOST.md) | Production deployment guide, first-run walkthrough, backups |
| [docs/ENV.md](docs/ENV.md) | Complete reference of all environment variables |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Developer setup, testing, code conventions, PR checklist |
| [CLAUDE.md](CLAUDE.md) | AI-assisted development conventions and codebase gotchas |

## Development

```bash
# Install deps (requires Python 3.11+ and uv)
uv sync --all-extras

# Start Postgres (if not using docker compose)
service postgresql start

# Apply migrations
uv run alembic upgrade head

# Run dev server
uv run uvicorn app.main:app --reload

# Run tests
uv run pytest                     # all tests (needs Postgres)
uv run pytest -m "not postgres"   # unit tests only

# Lint
uv run ruff check .
uv run ruff format --check .
```

## Project structure

```
app/
├── main.py              # FastAPI factory, lifespan, error handlers
├── config.py            # Pydantic Settings (all ENV vars)
├── deps.py              # Tenant + principal + DB dependencies
├── scheduler.py         # APScheduler periodic jobs
├── db/                  # SQLAlchemy base + async session
├── models/              # ORM models + enums
├── services/            # Business logic (HTTP-free)
├── routers/             # FastAPI route handlers
├── security/            # Passwords, tokens, CSRF, session
├── storage/             # S3 helper
├── email/               # SMTP sender + Jinja templates
├── tasks/               # Background + periodic tasks
├── templates/           # Jinja2 HTML pages
├── static/              # CSS, JS
└── platform/            # Opt-in SaaS layer (Identity, tenant CRUD)
migrations/              # Alembic (0001-0007 + 1001)
scripts/                 # CLI: create_tenant, seed_dev, build_tailwind
tests/                   # 112 tests (pytest + httpx + moto)
docs/                    # ARCHITECTURE, SELF_HOST, ENV
```

## Licence

Licence TBD before first public release — see [LICENSE.placeholder](LICENSE.placeholder).
Options under consideration: AGPL-3.0 (full) or MIT core + commercial platform.

## Status

Active development. MVP functional for pilot deployment.
112 pytest tests, CI via GitHub Actions (lint + test + Docker build).
