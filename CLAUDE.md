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

Platform admin is **an operator role, not a super-user**. A platform
admin has tenant CRUD + aggregate metrics, but accessing a tenant's
business data still requires an explicit `TenantMembership`. The
opt-in path is `/platform/admin/tenants/{id}/support-access` —
creates a User row in the target tenant, a `TenantMembership` with
`access_type='support'`, and an audit event
`platform.support_access_granted` in the target tenant's log. The
revoke flow is symmetric. Never add a code path that silently lets a
platform admin read tenant data without going through this grant.

### 7. i18n — `_t` vs `gettext`, `%%` escaping

User-visible strings flow through **two different helpers**:

- Jinja templates: `{{ _("...") }}` — standard gettext alias.
- Python code: `_t(request, "...")` from `app.i18n` (the `request`
  argument picks the locale). Using `_()` in Python is an error —
  `app.i18n` does not define it.

Because Babel's default keywords don't include `_t`, **`pybabel
extract` blindly will mark every `_t`-only msgid as obsolete** and
you'll ship the app in English even after a "full translation pass".
This bit us twice. The fix is already in `babel.cfg`:

```ini
python_keyword = _t:2 _ gettext ngettext:1,2
```

Safe extract + compile workflow:

```bash
uv run pybabel extract -F babel.cfg -k _t:2 -k _ -k gettext \
  -k 'ngettext:1,2' -o app/locale/messages.pot .
uv run pybabel update -i app/locale/messages.pot -d app/locale
uv run pybabel compile -d app/locale
```

**Never hand-write a different keyword list** — the command above
is the one that matches the source. Verify by grepping the PO
catalog afterwards for strings you know exist in Python (e.g.
`Invalid email or password.`) — they must NOT be under `#~ msgid`
(obsolete).

**`%%` trap.** Jinja's i18n extension runs `rv % variables` on the
gettext-returned string. A translated message containing a bare `%`
(e.g. `'≥90 % včas'`) will try to interpret `%v` as a format spec
and raise `ValueError: unsupported format character`. Rules:

- If the msgid has `%%`, **the msgstr must also have `%%`** for any
  literal `%`.
- Prefer the split-form pattern
  `{{ _('On time') }}: {{ value }}` over
  `{{ _('On time %(v)s') % {'v': value} }}` — simpler to translate
  and immune to the `%%` trap.

### 8. Authenticated-state redirects

Every GET page that presents an *unauthenticated* action (log in,
sign up, reset password) **MUST redirect the already-authenticated
visitor somewhere useful** rather than re-rendering the form. The
canonical destinations:

| Page | Target when logged in |
|---|---|
| `/auth/login` (tenant) | `/app` |
| `/auth/password-reset` | `/app/admin/profile` (password change) |
| `/platform/login` | `/platform/select-tenant` |
| `/platform/signup` | `/platform/select-tenant` |
| `/platform/password-reset` | `/platform/select-tenant` |
| tenant `/` (index) | `/app` |

`app/routers/public.py:login_form` does this for the tenant login
already (via `require_login(optional=True)`). Match the pattern for
any new auth-shaped route.

### 9. Flash messages

POST-redirect-GET uses **query-param flashes**, not cookies:

```python
return RedirectResponse(
    url=f"/app/orders?notice={quote('Order submitted')}",
    status_code=303,
)
```

And in templates:

```jinja
{% include "_flash.html" %}   {# renders `notice` / `error` query params #}
```

**Every POST route that mutates data should flash on redirect**, both
on success and recoverable-error paths. Silent redirects hide whether
an action succeeded. This is especially important in platform admin
routes, which historically were silent — audit trails help the
operator verify, but a one-shot UI flash is the immediate feedback.

### 10. CSP form-action for cross-subdomain handoffs

`form-action 'self'` blocks *redirect chains* following a form POST
that cross origins. When the platform `/switch/{slug}` endpoint
redirects from the apex to `{slug}.apex/platform/complete-switch`,
`form-action 'self'` cancels the redirect and the user silently
stays on the previous page.

`SecurityHeadersMiddleware` takes a `subdomain_apex` argument (wired
from `settings.platform_cookie_domain`) that extends `form-action`
with `https://*.{apex}`. Single-host dev keeps the tighter `'self'`
policy. Don't hand-edit the CSP string in `headers.py` without
running the /switch flow end-to-end afterwards.

### 11. Deploy & env vars — single source of truth

Production secrets live in `/etc/assoluto/env` on the VPS (mode 600,
deploy-owned). The production compose file uses
`env_file: - /etc/assoluto/env` so **every variable flows through
automatically** — no per-var mapping in
`docker-compose.prod.yml`. Compose-level `${VAR:?}` assertions
remain for hard operator requirements (APP_SECRET_KEY,
DATABASE_URL passwords, S3/SMTP).

`deploy-production.yml` does `git reset --hard origin/production`
before `docker compose up` so **compose-file changes in the repo
propagate on every deploy** — there is no manual copy step.

When you add a new setting:
1. Add the Pydantic field in `app/config.py` with a safe default.
2. Document in `.env.example`.
3. Operator edits `/etc/assoluto/env`. Done — no compose edit.

Deployment-fixed overrides (e.g. `APP_ENV: production`) stay in
`docker-compose.prod.yml` because they must not be operator-tunable.

### 12. When to write a plan doc vs just do it

For polish passes that span 5+ pages or 3+ templates, **audit first,
write a plan document, then implement**. Examples of past audits in
commit history: `feat(ux): round-2 polish`, `UX audit — independent
app walkthrough`. Two benefits:

1. The user can reorder / trim scope before you spend hours on the
   wrong priority.
2. You notice connections between fixes (e.g. "auth redirects + nav
   polish both need a new flash-on-redirect helper" — saves
   duplicate work).

One-line bug reports (`X broken, please fix`) can skip the plan.

**Reusable audit pipeline.** Three Claude Code skills in
`.claude/skills/` give the founder repeatable multi-perspective
audits:

* `/audit` — fans out four parallel sub-agents (UX with Chrome, Backend,
  Security, Business) and consolidates findings into
  `docs/audit-runs/<date>/findings.md`.
* `/audit-fix` — applies every `Auto-fixable: yes` finding from the
  latest run; one logical batch per commit; pushes to production;
  updates statuses in place.
* `/audit-verify` — re-runs the audit and diffs against the previous
  run, marking `resolved` / `persisted` / `regressed` / `new`.

Skill files live at `.claude/skills/<name>/SKILL.md` (directory-
based — Claude Code's modern format; the deprecated
`.claude/commands/<name>.md` flat-file path doesn't reliably load).
Sub-agent definitions live in `.claude/agents/{ux,backend,security,
business}-auditor.md` (flat files). Each agent carries the per-
perspective rules so agents stay focused (e.g. UX auditor is
forbidden from submitting any mutating POST against prod). Audit-
trail docs live in `docs/audit-runs/` (committed — see the
`README.md` there).

Full portable recipe — including the migration story from `.claude/
commands/` to `.claude/skills/<name>/SKILL.md` — is in
`docs/AUDIT_PIPELINE.md`. Hand that file to any other Claude session
to recreate the same pipeline in another repo.

### 13. Session cookie is tenant-scoped — verify on every read

``read_session(...)`` only checks the signature, not the ``tenant_id``
embedded in the payload. Cookies are set without a ``Domain`` attribute
(host-only), so in a correctly configured browser they can't cross
subdomains. But callers on public pages (``/``, ``/auth/login``,
``/auth/password-reset``) MUST additionally verify
``session.tenant_id == tenant.id`` or they produce
``ERR_TOO_MANY_REDIRECTS``: public route sees a decodable cookie →
redirects to ``/app`` → ``get_current_principal`` rejects the tenant
mismatch → 401 → bounces to ``/auth/login`` → sees the cookie again.

Use :func:`app.security.session.read_session_for_tenant` for the decode
+ match in one call. When a mismatch is detected, stamp a Set-Cookie
deletion via :func:`cookie_mismatches_tenant` + :func:`clear_session`
so the zombie doesn't re-trigger on the next request.

### 14. Recovery tokens must be single-use

Password-reset, invitation-accept and staff-invitation tokens MUST
become invalid after their first successful consumption. Otherwise an
attacker who briefly has the URL (email intercepted, shared computer,
screenshot) can replay it within the TTL to overwrite the victim's
password on their behalf.

Two patterns are in use:

* **Session-version embedding** (password reset): include the
  principal's current ``session_version`` in the token payload; on
  consume, reject if ``token.sv != row.session_version`` and bump
  after the mutation so the second attempt fails. Any unrelated
  ``session_version`` bump (manual password change, admin reset,
  contact accepting a fresh invite) also invalidates in-flight tokens.
* **State flag check** (invitations): refuse acceptance when the row
  already has ``password_hash IS NOT NULL`` (staff) or
  ``accepted_at IS NOT NULL`` (customer contact).

Both patterns are at the service layer so bypassing the UI by posting
directly to the endpoint still hits them.

### 15. Docker Compose env-var precedence: ``environment:`` wins over ``env_file:``

When a service declares both, values from the ``environment:`` block
override anything with the same key in an ``env_file:``. Our base
``docker-compose.yml`` hard-codes a few dev defaults (``SMTP_PORT``,
``SMTP_HOST``) in its ``environment:`` block. Production can't rely on
merely listing an override in ``/etc/assoluto/env`` — we hit this
once when prod emails silently timed out because the Brevo port in
env_file was shadowed by ``SMTP_PORT: "1025"`` in base.

The pattern in ``docker-compose.prod.yml`` is: anything the operator
needs to tune MUST be explicitly re-listed in the prod overlay's
``environment:`` as ``${VAR:?}``. That does two things — (a) the
``${VAR:?}`` expansion reads from the env file that the compose CLI
is already consuming via ``--env-file /etc/assoluto/env``, so the
operator value wins; (b) a missing value loudly fails compose at
``up`` time instead of silently shipping a broken container. See the
``SMTP_PORT`` comment in the overlay for the standing example.

### 16. TRUSTED_PROXIES must list the reverse-proxy container network

slowapi defaults to ``request.client.host`` which in a docker-
compose deployment is the Caddy container IP — i.e. one global
bucket for every external client. ``TRUSTED_PROXIES`` tells
``app.security.rate_limit._client_ip`` when to believe the
``X-Forwarded-For`` header. Empty = don't trust anything = every
request looks like it's from the proxy.

For the docker-compose + Caddy deploy, trust the RFC1918 container
bridge ranges:

```
TRUSTED_PROXIES=172.16.0.0/12,10.0.0.0/8
```

An attacker can't get a source IP in those ranges into our front-
facing Caddy, so honouring XFF from there is safe. Add Cloudflare's
published ranges if you stand up a CDN in front.

### 17. Plans: DB owns structure, env owns Stripe price IDs

Subscription plans (Starter, Pro, …) are rows in ``platform_plans``
seeded by migration ``1003_billing``. ``platform_subscriptions.plan_id``
is a foreign key to that row — so the plan row is stable long-term,
even if prices or limits change later (new revision = new row).

What lives **where**:

* **DB** (``platform_plans``): code, name, ``monthly_price_cents``,
  currency, ``max_users`` / ``max_contacts`` / ``max_orders_per_month``
  / ``max_storage_mb``, ``is_active``. Seeded by the migration;
  edited via ``psql`` today, admin UI later.
* **ENV** (``STRIPE_PRICE_STARTER`` / ``STRIPE_PRICE_PRO``): Stripe
  price IDs. They rotate per environment (test vs. live) and the
  operator should be able to flip them without a migration.
  ``_sync_stripe_prices_from_env`` (in ``app.main``) UPSERTs these
  into ``platform_plans.stripe_price_id`` at boot; empty env leaves
  the existing value alone, so staging can stay unconfigured.
* **Templates** (``pricing.html``): marketing copy + headline prices.
  Treat as editorial content; commit changes via git, not DB edits.

Plan limits are enforced by ``ensure_within_limit`` (see
``app.platform.usage``) called from the four creation services
(``invite_tenant_staff``, ``invite_customer_contact``,
``create_order``, ``create_attachment_row``). Throws
``PlanLimitExceeded`` when ``current + delta > limit``; caught by a
global exception handler in ``app.main`` and rendered as a friendly
402 page with an Upgrade CTA. Tenants without a subscription (self-
hosted, pre-billing signup) skip the check and stay unlimited.

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
