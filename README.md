# Assoluto

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![CI](https://github.com/elh0m3r0/sme-client-portal/actions/workflows/ci.yml/badge.svg)](https://github.com/elh0m3r0/sme-client-portal/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://docs.astral.sh/ruff/)

**Assoluto** — a multi-tenant customer portal for manufacturing SMEs.
Customers submit orders (with attachments), the supplier quotes,
confirms, tracks production status, and manages client-owned materials
stored on-site. Built with Python / FastAPI / PostgreSQL. Hosted at
[assoluto.eu](https://assoluto.eu), free and open-source under AGPL-3.0.

## Features

| Module | Description |
|---|---|
| **Orders** | Customer submits items and attachments; supplier prices them, proposes a delivery date, and moves the order through DRAFT → SUBMITTED → QUOTED → CONFIRMED → IN_PRODUCTION → READY → DELIVERED → CLOSED. Comments and full audit trail. |
| **Product catalog** | Per-tenant or per-customer catalog; pick items into an order instead of free text, with pre-filled price and unit. |
| **Customer-owned asset tracking** | Track materials and tools that belong to the customer but are stored at the supplier: receive, issue, consume, adjust. Customer gets a read-only view. |
| **Email notifications** | New orders, status transitions, and comments — via `BackgroundTasks` (no Redis required). |
| **Multi-tenant** | Shared database with Postgres Row-Level Security. Two DB roles (`portal` owner + `portal_app` non-owner). |
| **Platform (SaaS)** | Opt-in `app/platform/` package: global Identity, cross-tenant login, tenant switcher, platform admin CRUD. Activated by `FEATURE_PLATFORM=true`. |
| **CSRF protection** | Double-submit cookie pattern on every mutating route. |
| **Staff admin** | Staff user management (invite, deactivate), self-service password change, password reset flow. |

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
| [docs/SELF_HOST.md](docs/SELF_HOST.md) | Self-hosted production deployment guide |
| [docs/DEPLOY_SAAS.md](docs/DEPLOY_SAAS.md) | Hosted SaaS deployment (Hetzner + Coolify + Cloudflare R2 + Resend + Stripe) |
| [docs/DEPLOY_HETZNER.md](docs/DEPLOY_HETZNER.md) | Hetzner VPS single-box production setup with auto-deploy from the `production` branch |
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

## License

**GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)** — see [LICENSE](LICENSE).

You are free to use, modify, and redistribute this software under the
terms of the AGPL-3.0. If you run a modified version over a network
(e.g., as a hosted SaaS), the AGPL requires that you make the source
code of your modified version available to users of that network service.

For commercial licensing inquiries (proprietary forks, OEM integration,
etc.) contact: `team@assoluto.eu`

## Community

- **Bug reports & feature requests:** [GitHub Issues](https://github.com/elh0m3r0/sme-client-portal/issues)
- **Security disclosures:** see [SECURITY.md](SECURITY.md)
- **Code of Conduct:** [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — we follow the Contributor Covenant v2.1
- **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md)
- **Changelog:** [CHANGELOG.md](CHANGELOG.md)

## Status

Active development. MVP functional for pilot deployment.
112 pytest tests, CI via GitHub Actions (lint + test + Docker build).

---

## Česká verze (Czech version)

### Assoluto

**Assoluto** — multi-tenantní zákaznický portál pro malé a střední
výrobní firmy. Zákazníci zadávají objednávky (včetně příloh),
dodavatel nacení, potvrzuje, sleduje stav výroby a eviduje materiál
zákazníka skladovaný u dodavatele. Postaveno na Pythonu / FastAPI /
PostgreSQL. Hostováno na [assoluto.eu](https://assoluto.eu), zdarma
a open-source pod licencí AGPL-3.0.

### Funkce

| Modul | Popis |
|---|---|
| **Objednávky** | Klient zadá položky a přílohy, dodavatel nacení, navrhne termín, aktualizuje stav (DRAFT → SUBMITTED → QUOTED → CONFIRMED → IN_PRODUCTION → READY → DELIVERED → CLOSED). Komentáře, audit trail. |
| **Katalog produktů** | Per-tenant nebo per-customer katalog; výběr do objednávky místo free-textu s předvyplněním ceny a jednotky. |
| **Správa majetku klienta** | Evidence materiálu/nástrojů klienta u dodavatele: příjem, výdej, spotřeba, korekce. Klient má read-only přehled. |
| **E-mail notifikace** | Nové objednávky, změny stavů, komentáře — přes BackgroundTasks (bez Redisu). |
| **Multi-tenant** | Sdílená DB + Postgres Row-Level Security. Dva DB uživatelé (`portal` owner + `portal_app` non-owner). |
| **Platform (SaaS)** | Opt-in `app/platform/` balíček: globální Identity, cross-tenant login, přepínač tenantů, platform admin CRUD. Aktivuje se přes `FEATURE_PLATFORM=true`. |
| **CSRF ochrana** | Double-submit cookie pattern na všech mutating routes. |
| **Staff admin** | Správa staff uživatelů (invite, deaktivace), self-service změna hesla, password reset flow. |

### Rychlý start

```bash
git clone https://github.com/eLh0m3r0/sme-client-portal.git
cd sme-client-portal
cp .env.example .env
docker compose up --build
```

Naplnit demo data:

```bash
docker compose exec web python -m scripts.seed_dev
```

Demo přihlašovací údaje: `owner@4mex.cz` / `demo1234` (staff),
`jan@acme.cz` / `demo1234` (zákaznický kontakt).

### Licence

**GNU Affero General Public License v3.0 nebo novější (AGPL-3.0-or-later)**
— viz [LICENSE](LICENSE). Pokud provozujete upravenou verzi po síti
(např. jako hostovanou SaaS), AGPL vyžaduje, abyste zdrojový kód své
upravené verze zpřístupnili uživatelům dané síťové služby.

Pro komerční licencování (proprietární forky, OEM integrace apod.):
`team@assoluto.eu`.

### Komunita

- **Bug reporty a návrhy funkcí:** [GitHub Issues](https://github.com/elh0m3r0/sme-client-portal/issues)
- **Bezpečnostní oznámení:** viz [SECURITY.md](SECURITY.md)
- **Kodex chování:** [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — řídíme se Contributor Covenant v2.1
- **Přispívání:** [CONTRIBUTING.md](CONTRIBUTING.md)
- **Changelog:** [CHANGELOG.md](CHANGELOG.md)
