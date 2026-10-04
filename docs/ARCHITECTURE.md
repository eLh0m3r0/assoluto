# Architecture

Concise map of the codebase for someone reading it for the first time.

## Stack

| Layer       | Choice                                            |
|-------------|---------------------------------------------------|
| Web         | FastAPI 0.115                                     |
| Templating  | Jinja2 + `jinja2-fragments` (HTMX-friendly)       |
| DB          | PostgreSQL 16                                     |
| ORM         | SQLAlchemy 2 async + `asyncpg`                    |
| Migrations  | Alembic (sync via `psycopg`)                      |
| Scheduler   | APScheduler in-process (no Redis)                 |
| Background  | FastAPI `BackgroundTasks` (no Redis)              |
| Storage     | S3-compatible via boto3 (MinIO / B2 / R2)        |
| E-mail      | Plain SMTP (Postmark/Resend/MailHog)              |
| Auth        | Cookie session (itsdangerous) + Argon2 passwords  |

Roadmap item R0 replaces BackgroundTasks with Dramatiq+Redis when the
first SaaS tenant lands.

## Multi-tenancy model

```
Tenant (supplier, e.g. 4MEX)
├── User              (tenant staff — admins and operators)
├── Customer          (supplier's client company)
│   └── CustomerContact  (people on the client side who log in)
├── Product           (catalog item — shared or scoped to a customer)
├── Order
│   ├── OrderItem
│   ├── OrderAttachment
│   ├── OrderStatusHistory (audit trail of state transitions)
│   └── OrderComment       (thread, staff can mark `is_internal`)
└── Asset             (client-owned material/tools held by supplier)
    └── AssetMovement (signed qty, receive / issue / consume / adjust)
```

### Two DB roles

- `portal` is the table owner. Alembic migrations, the
  `scripts/create_tenant.py` CLI, and the periodic auto-close job run
  as this role; Postgres skips RLS for table owners.
- `portal_app` is an unprivileged login role. The FastAPI process runs
  as this role, so every statement is filtered by the
  `tenant_isolation` policy
  (`tenant_id = current_setting('app.tenant_id')::uuid`).

The `app.tenant_id` GUC is set at the start of every request by
`app.deps.get_db` via `set_config('app.tenant_id', tid, true)`.

### Two identities

```
Principal (dataclass)
├── type = "user"     → User (staff)
└── type = "contact"  → CustomerContact  (+ customer_id)
```

`require_login` and `require_tenant_staff` are the two dependencies routes use
to gate access.

## Request flow

```
HTTP request
 │
 ▼
FastAPI router
 ├── get_current_tenant  (resolve by subdomain / X-Tenant-Slug / default)
 ├── get_db              (open session + set_config app.tenant_id)
 ├── get_current_principal (decode signed cookie → User | Contact | None)
 │
 ▼
service layer (app/services/*)   ← pure, HTTP-free
 │
 ▼
SQLAlchemy ORM      ← all queries hit RLS as portal_app
 │
 ▼
PostgreSQL
```

On the way back out:

1. Endpoint builds notification payloads while the request session is
   still open.
2. Endpoint issues `await db.commit()` BEFORE registering background
   tasks — FastAPI runs BackgroundTasks inside `await response(send)`
   which executes before the request-scoped dep cleanup, so without
   an explicit commit the background task's fresh session wouldn't
   see the just-written rows.
3. Response is sent; BackgroundTasks fire e-mails and thumbnail jobs;
   request-scoped dep cleanup runs (now a no-op, since we already
   committed).

## Directory layout

```
app/
├── main.py          # FastAPI factory, lifespan, error handlers
├── config.py        # Pydantic Settings from env vars
├── deps.py          # tenant + principal + DB dependencies
├── scheduler.py     # APScheduler setup (called from lifespan)
├── logging.py       # structlog configuration
├── templating.py    # Jinja2 wrapper with full + block render
├── db/              # base metadata, async session factory
├── models/          # SQLAlchemy ORM models + enums + mixins
├── services/        # business logic (HTTP-free)
├── routers/         # FastAPI routers — thin HTTP glue
├── security/        # passwords, tokens, session cookie, CSRF
├── storage/         # boto3 S3 helper (internal + public client)
├── email/           # SMTP sender + Jinja email templates
├── tasks/           # email + thumbnail + periodic task bodies
├── templates/       # Jinja pages (public, app, error, platform)
└── platform/        # opt-in SaaS layer (see below)
migrations/          # Alembic (0001–0007 core + 1001 platform)
scripts/             # create_tenant, seed_dev, build_tailwind, backup
docker/              # entrypoint.sh, postgres-init.sql, nginx.conf.example
tests/               # 112 tests (pytest + httpx ASGI + moto S3 + freezegun)
docs/                # SELF_HOST, ARCHITECTURE, ENV
```

## CSRF protection

Double-submit cookie pattern split into two pieces:

1. **`CsrfCookieMiddleware`** (pure ASGI): stamps a `csrftoken` cookie on
   every response and stashes the value on `scope["state"]["csrf_token"]`.
2. **`verify_csrf`** (FastAPI dependency): wired on every router via
   `dependencies=[Depends(verify_csrf)]`. Compares the cookie against
   `X-CSRF-Token` header or `csrf_token` form field. Safe methods
   (GET/HEAD/OPTIONS) skip validation.

Templates expose `{{ csrf_input() }}` which renders a hidden `<input>`.

## Platform package (opt-in SaaS layer)

Everything in `app/platform/` is loaded only when `FEATURE_PLATFORM=true`.
Core never imports from it — `app.main.create_app()` calls
`app.platform.install(app)` conditionally.

### Models (not RLS-protected)

- **`Identity`** — globally unique email + password hash. One person
  across all tenants.
- **`TenantMembership`** — links an Identity to a specific tenant via
  either `user_id` (staff) or `contact_id` (customer contact).

### Routes

- `/platform/login` + `/platform/logout` — platform-wide auth
- `/platform/select-tenant` — tenant switcher showing all memberships
- `/platform/switch/{slug}` — mints a tenant-local session and redirects
- `/platform/admin/tenants` — CRUD for platform admins

### Session

A separate `sme_portal_platform` signed cookie scoped to
`PLATFORM_COOKIE_DOMAIN` (parent domain) so every tenant subdomain
shares the session. The tenant-local `sme_portal_session` cookie
co-exists.
