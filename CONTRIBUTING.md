# Contributing

Thanks for considering a contribution! This guide covers everything you
need to get the codebase running locally and submit a clean PR.

## Prerequisites

- Python 3.11+ (3.12 recommended)
- [uv](https://docs.astral.sh/uv/) package manager
- PostgreSQL 16
- Docker + Docker Compose (optional, for full-stack dev)

## Setup

```bash
# Clone and install
git clone https://github.com/eLh0m3r0/sme-client-portal.git
cd sme-client-portal
uv sync --all-extras
cp .env.example .env
```

### Database

The app needs **two Postgres roles**: `portal` (owner, runs migrations)
and `portal_app` (non-owner, runs the app, subject to RLS).

```bash
# Option A: use docker compose (recommended)
docker compose up postgres -d

# Option B: use a local Postgres
sudo -u postgres psql -f docker/postgres-init.sql
```

Then apply migrations:

```bash
uv run alembic upgrade head
```

### Run the dev server

```bash
uv run uvicorn app.main:app --reload
```

## Running tests

```bash
# All tests (requires a running Postgres at DATABASE_URL)
uv run pytest -v

# Only unit tests (no Postgres needed)
uv run pytest -m "not postgres"

# Single test file
uv run pytest tests/test_orders_flow.py -v
```

Tests marked `@pytest.mark.postgres` are automatically skipped when
Postgres is not reachable at `DATABASE_URL`. CI always runs them (it
spins up a postgres service container).

### Key test fixtures

| Fixture | What it provides |
|---|---|
| `client` | ASGI httpx client (no tenant context) |
| `tenant_client` | ASGI client with `X-Tenant-Slug: 4mex` + a seeded demo tenant |
| `owner_engine` | Async engine as the `portal` owner (bypasses RLS) for seeding |
| `demo_tenant` | A `TenantRef(id, slug, name)` for the pre-created `4mex` tenant |
| `mock_s3` | In-process moto S3 mock with the bucket pre-created |

All fixtures include CSRF handling via `CsrfAwareClient` — POST requests
automatically inject the `csrf_token` form field.

## Linting

```bash
uv run ruff check .           # lint
uv run ruff format --check .  # format check
uv run ruff check . --fix     # auto-fix
uv run ruff format .          # auto-format
```

CI runs both; PRs must be clean.

## Code conventions

### Architecture layers

```
routers/  → thin HTTP glue (parse form, call service, render template)
services/ → business logic (testable without HTTP, no request/response)
models/   → SQLAlchemy ORM (data definition only)
tasks/    → background task bodies (called from BackgroundTasks or APScheduler)
```

Routers **never** contain business logic. Services **never** import
FastAPI or touch `Request`. Models **never** contain queries.

### Tenant isolation

Every tenant-scoped table inherits `TenantMixin` (adds `tenant_id` FK).
Postgres RLS policies filter rows via `current_setting('app.tenant_id')`.
The `get_db` dependency in `app/deps.py` sets this variable on every
request. **Never bypass this** unless you're writing a background job
that explicitly needs cross-tenant access (use the owner engine then).

### BackgroundTasks + explicit commit

FastAPI runs BackgroundTasks **before** request-scoped dependency cleanup.
This means the request's DB transaction is still open when the task
starts. If your task opens its own DB session (which it should), it
won't see the just-written rows unless the request endpoint explicitly
calls `await db.commit()` before scheduling the task. This pattern is
used in `attachments.py`, `orders.py` (transitions), and `orders.py`
(comments). Follow it for any new background task that reads from DB.

### CSRF

Every router that accepts POST/PUT/PATCH/DELETE must include
`dependencies=[Depends(verify_csrf)]`. Every `<form method="post">` in
templates must include `{{ csrf_input() }}`.

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(orders): add status filter to orders list
fix(docker): copy uv.lock into builder stage
docs: add ENV.md reference
```

## PR checklist

- [ ] `uv run ruff check .` clean
- [ ] `uv run ruff format --check .` clean
- [ ] `uv run pytest` passes (all 112+ tests)
- [ ] New features have tests
- [ ] Templates include `{{ csrf_input() }}` in every form
- [ ] New ENV vars documented in `docs/ENV.md` and `.env.example`
- [ ] Migrations are numbered sequentially and have a downgrade path
