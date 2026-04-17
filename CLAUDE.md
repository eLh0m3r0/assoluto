# CLAUDE.md — AI-assisted development guide

This file captures conventions and gotchas discovered during the
development of this codebase. It serves as context for AI coding
assistants (Claude Code, Copilot, Cursor) and for human developers
coming to the project fresh.

## Repo layout at a glance

```
app/           Python package — the FastAPI application
  main.py      App factory, lifespan (scheduler), error handlers, middleware
  config.py    Pydantic Settings — every ENV var the app reads
  deps.py      FastAPI dependencies: tenant resolver, RLS-scoped DB session,
               principal (User | CustomerContact) resolver
  models/      SQLAlchemy ORM; every tenant-scoped model inherits TenantMixin
  services/    Pure business logic; no HTTP, no Request
  routers/     Thin HTTP handlers; call services, render templates
  security/    passwords.py, tokens.py, csrf.py, session.py
  storage/     S3 (boto3) helper
  email/       SMTP sender + Jinja email templates
  tasks/       Background task bodies (email, thumbnails, periodic cleanup)
  templates/   Jinja2 HTML (base, auth, orders, customers, products, assets, admin, errors, platform)
  platform/    Opt-in SaaS package (Identity, TenantMembership, platform admin)
migrations/    Alembic (0001-0008 core, 1001-1005 platform/billing)
scripts/       CLI tools: create_tenant, seed_dev, build_tailwind
tests/         207+ tests (pytest-asyncio, httpx ASGITransport, moto for S3)
```

## Critical patterns to understand

### 1. Two DB roles

- `portal` = table owner. Used by Alembic, CLI scripts, periodic tasks.
  **Bypasses RLS** (Postgres doesn't apply RLS to table owners).
- `portal_app` = non-owner. Used by the running app.
  **Subject to RLS** on every tenant-scoped table.

The `get_db` dependency in `deps.py` opens a session as `portal_app`
and calls `set_config('app.tenant_id', tid, true)` so every query in
the request is automatically filtered to the current tenant.

### 2. BackgroundTasks + explicit commit

FastAPI runs BackgroundTasks inside `await response(send)`, which
executes **before** the request-scoped dependency cleanup (and therefore
before `get_db`'s session commit). Without an explicit `await db.commit()`
the background task's fresh session won't see the just-written rows.

**Every endpoint that schedules a background task reading from DB must:**
1. Build notification payloads while the session is still open.
2. `await db.commit()` explicitly.
3. Then `background_tasks.add_task(...)`.

See `app/routers/attachments.py:upload_attachment` and
`app/routers/orders.py:orders_transition` for the canonical pattern.

### 3. CSRF double-submit

- `CsrfCookieMiddleware` (pure ASGI) stamps the `csrftoken` cookie.
- `verify_csrf` (FastAPI dependency) validates on POST. Reads
  `request.form()` safely (Starlette caches parsed FormData).
- Every router uses `dependencies=[Depends(verify_csrf)]`.
- Every template form includes `{{ csrf_input() }}`.
- Tests use `CsrfAwareClient` (in `conftest.py`) which auto-injects
  the token into POST requests.

### 4. set_config vs SET LOCAL

asyncpg does not support bind parameters in `SET LOCAL`. Use
`set_config('app.tenant_id', :tid, true)` instead (the third arg
`true` = `is_local`). Wrapped in `deps.set_tenant_context()`.

### 5. Tenant-scoped unique constraints + NULL

Postgres UNIQUE treats two NULLs as distinct. The product catalog's
`UNIQUE(tenant_id, customer_id, sku)` doesn't prevent duplicate SKUs
when `customer_id IS NULL`. App-level check in
`product_service.create_product()` handles this.

### 6. Platform package isolation

`app/platform/` is opt-in via `FEATURE_PLATFORM=true`. Core (`app/`)
**never** imports from `app.platform`. The `install(app)` hook in
`app/platform/__init__.py` registers routes only when the flag is on.
The test `test_platform_routes_not_mounted_when_flag_off` enforces this.

## Test fixtures quick reference

| Fixture | Needs PG | What |
|---|---|---|
| `client` | no | ASGI client, no tenant |
| `tenant_client` | yes | ASGI client, `X-Tenant-Slug: 4mex`, with seeded demo tenant |
| `owner_engine` | yes | Async engine as `portal` owner (bypasses RLS) |
| `demo_tenant` | yes | Pre-created `4mex` TenantRef |
| `wipe_db` | yes | Deletes all data before/after test |
| `mock_s3` | no | In-process moto S3 with pre-created bucket |
| `platform_client` | yes | ASGI client with `FEATURE_PLATFORM=true` |

All clients are `CsrfAwareClient` instances — POST requests automatically
include the CSRF token.

## Common commands

```bash
uv run pytest -q                          # full suite
uv run pytest tests/test_orders_flow.py   # one file
uv run pytest -m "not postgres"           # unit only
uv run ruff check . --fix                 # lint + fix
uv run ruff format .                      # format
uv run alembic upgrade head               # apply migrations
uv run alembic history                    # show migration chain
python -m scripts.create_tenant <slug> <email>
python -m scripts.seed_dev
```
